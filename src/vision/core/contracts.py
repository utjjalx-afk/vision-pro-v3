"""Initial immutable wire-contract stubs, not a market-data engine."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    FOREX = "forex"
    METALS = "metals"


class EventType(StrEnum):
    TRADE = "trade"
    QUOTE = "quote"
    BAR = "bar"


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("Identifiers must be nonempty strings without surrounding whitespace")


def _positive_decimal(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("Prices and instrument increments must be positive finite Decimals")


def _quantity(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_signed():
        raise ValueError("Quantity must be a nonnegative finite Decimal")


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise ValueError("Timestamps must be timezone-aware UTC")
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    instrument_id: str
    venue: str
    symbol: str
    asset_class: AssetClass
    base_currency: str
    quote_currency: str
    tick_size: Decimal
    lot_size: Decimal
    minimum_quantity: Decimal
    contract_size: Decimal
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for value in (
            self.instrument_id,
            self.venue,
            self.symbol,
            self.base_currency,
            self.quote_currency,
        ):
            _identifier(value)
        if not isinstance(self.asset_class, AssetClass) or self.schema_version != "1":
            raise ValueError("Unsupported asset class or schema version")
        for value in (self.tick_size, self.lot_size, self.contract_size):
            _positive_decimal(value)
        _quantity(self.minimum_quantity)

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "symbol": self.symbol,
            "asset_class": self.asset_class.value,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "tick_size": str(self.tick_size),
            "lot_size": str(self.lot_size),
            "minimum_quantity": str(self.minimum_quantity),
            "contract_size": str(self.contract_size),
        }


@dataclass(frozen=True, slots=True)
class TradePayload:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        _positive_decimal(self.price)
        _quantity(self.quantity)


@dataclass(frozen=True, slots=True)
class QuotePayload:
    bid: Decimal
    ask: Decimal
    bid_quantity: Decimal
    ask_quantity: Decimal

    def __post_init__(self) -> None:
        _positive_decimal(self.bid)
        _positive_decimal(self.ask)
        _quantity(self.bid_quantity)
        _quantity(self.ask_quantity)
        if self.bid > self.ask:
            raise ValueError("Crossed quotes are rejected by the initial contract stub")


@dataclass(frozen=True, slots=True)
class BarPayload:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    interval_seconds: int

    def __post_init__(self) -> None:
        for value in (self.open, self.high, self.low, self.close):
            _positive_decimal(value)
        _quantity(self.volume)
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Invalid OHLC bounds")
        if type(self.interval_seconds) is not int or self.interval_seconds <= 0:
            raise ValueError("Bar interval must be a positive integer")


@dataclass(frozen=True, slots=True)
class CanonicalMarketEvent:
    event_id: str
    source: str
    instrument_id: str
    event_type: EventType
    source_ts: datetime
    received_ts: datetime
    sequence: int
    payload: TradePayload | QuotePayload | BarPayload
    schema_version: str = "1"

    def __post_init__(self) -> None:
        for value in (self.event_id, self.source, self.instrument_id):
            _identifier(value)
        _utc(self.source_ts)
        _utc(self.received_ts)
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("Sequence must be a nonnegative integer")
        if not isinstance(self.event_type, EventType) or self.schema_version != "1":
            raise ValueError("Unsupported event type or schema version")
        expected = {
            EventType.TRADE: TradePayload,
            EventType.QUOTE: QuotePayload,
            EventType.BAR: BarPayload,
        }[self.event_type]
        if type(self.payload) is not expected:
            raise ValueError("Payload does not match event type")

    def to_dict(self) -> dict[str, object]:
        from dataclasses import fields

        payload = {
            field.name: (
                str(value)
                if isinstance(value := getattr(self.payload, field.name), Decimal)
                else value
            )
            for field in fields(self.payload)
        }
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "source": self.source,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type.value,
            "source_ts": _utc(self.source_ts),
            "received_ts": _utc(self.received_ts),
            "sequence": self.sequence,
            "payload": payload,
        }
