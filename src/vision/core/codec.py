"""Strict public JSON round-trips with no float conversion or dynamic imports."""

import re
from dataclasses import fields
from datetime import datetime
from decimal import Decimal, InvalidOperation

from vision.core.contracts import (
    AssetClass,
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    InstrumentSpec,
    QuotePayload,
    TimestampBasis,
    TradePayload,
)


def decimal_string(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Expected a bounded exact decimal string")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", value):
        raise ValueError("Invalid unsigned decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid decimal string") from None
    if abs(result.as_tuple().exponent) > 1000:
        raise ValueError("Decimal exponent exceeds the wire limit")
    return result


def utc_string(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Expected an ISO UTC timestamp ending in Z")
    return datetime.fromisoformat(value)


def _shape(value: object, cls: type, required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    if not required <= value.keys() or value.keys() - {field.name for field in fields(cls)}:
        raise ValueError("Missing or unknown contract fields")
    return dict(value)


def instrument_from_dict(value: object) -> InstrumentSpec:
    data = _shape(value, InstrumentSpec, {field.name for field in fields(InstrumentSpec)})
    data["asset_class"] = AssetClass(data["asset_class"])
    for key in ("tick_size", "lot_size", "minimum_quantity", "contract_size"):
        data[key] = decimal_string(data[key])
    return InstrumentSpec(**data)


def event_from_dict(value: object) -> CanonicalMarketEvent:
    data = _shape(
        value,
        CanonicalMarketEvent,
        {
            "schema_version",
            "event_id",
            "source",
            "instrument_id",
            "event_type",
            "source_ts",
            "received_ts",
            "sequence",
            "payload",
        },
    )
    data["event_type"] = kind = EventType(data["event_type"])
    data["timestamp_basis"] = TimestampBasis(data.get("timestamp_basis", "exchange"))
    data["source_ts"] = utc_string(data["source_ts"])
    data["received_ts"] = utc_string(data["received_ts"])
    cls = {EventType.TRADE: TradePayload, EventType.QUOTE: QuotePayload, EventType.BAR: BarPayload}[
        kind
    ]
    numeric = {
        EventType.TRADE: {"price", "quantity"},
        EventType.QUOTE: {"bid", "ask", "bid_quantity", "ask_quantity"},
        EventType.BAR: {"open", "high", "low", "close", "volume"},
    }[kind]
    required = numeric | ({"interval_seconds"} if kind is EventType.BAR else set())
    payload = _shape(data["payload"], cls, required)
    if "buyer_is_maker" in payload and type(payload["buyer_is_maker"]) is not bool:
        raise ValueError("Expected boolean buyer maker flag")
    for key in numeric:
        payload[key] = decimal_string(payload[key])
    if "open_ts" in payload:
        payload["open_ts"] = utc_string(payload["open_ts"])
    data["payload"] = cls(**payload)
    return CanonicalMarketEvent(**data)
