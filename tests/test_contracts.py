import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from vision.core.contracts import (
    AssetClass,
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    InstrumentSpec,
    QuotePayload,
    TradePayload,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id="SYNTHETIC:TESTUSD",
        venue="SYNTHETIC",
        symbol="TESTUSD",
        asset_class=AssetClass.CRYPTO,
        base_currency="TEST",
        quote_currency="USD",
        tick_size=Decimal("0.00000001"),
        lot_size=Decimal("0.001"),
        minimum_quantity=Decimal("0"),
        contract_size=Decimal("1"),
    )


def event(kind: EventType = EventType.TRADE) -> CanonicalMarketEvent:
    payloads = {
        EventType.TRADE: TradePayload(Decimal("123.4567890123456789"), Decimal("0.001")),
        EventType.QUOTE: QuotePayload(Decimal("100"), Decimal("101"), Decimal("0"), Decimal("2")),
        EventType.BAR: BarPayload(
            Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"), Decimal("4"), 60
        ),
    }
    return CanonicalMarketEvent(
        event_id="synthetic-event-1",
        source="SYNTHETIC",
        instrument_id="SYNTHETIC:TESTUSD",
        event_type=kind,
        source_ts=NOW,
        received_ts=NOW,
        sequence=0,
        payload=payloads[kind],
    )


def validator(filename: str) -> Draft202012Validator:
    schema = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize("kind", list(EventType))
def test_event_wire_schema_and_precision(kind):
    wire = event(kind).to_dict()
    validator("canonical-market-event.schema.json").validate(wire)
    assert wire["source_ts"] == "2026-01-01T00:00:00Z"
    if kind is EventType.TRADE:
        assert json.loads(json.dumps(wire))["payload"]["price"] == "123.4567890123456789"


def test_instrument_wire_schema_handles_small_decimal():
    wire = instrument().to_dict()
    validator("instrument-spec.schema.json").validate(wire)
    assert Decimal(wire["tick_size"]) == Decimal("0.00000001")


@pytest.mark.parametrize(
    "bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 1.1]
)
def test_invalid_price_and_tick_size_rejected(bad):
    with pytest.raises(ValueError):
        TradePayload(bad, Decimal("1"))
    with pytest.raises(ValueError):
        replace(instrument(), tick_size=bad)


@pytest.mark.parametrize("bad", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 1.0])
def test_invalid_quantities_rejected(bad):
    with pytest.raises(ValueError):
        TradePayload(Decimal("1"), bad)


@pytest.mark.parametrize(
    "bad", [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))]
)
def test_non_utc_timestamps_rejected(bad):
    with pytest.raises(ValueError):
        replace(event(), source_ts=bad)


@pytest.mark.parametrize("bad", [-1, 1.0, True])
def test_invalid_sequence_rejected(bad):
    with pytest.raises(ValueError):
        replace(event(), sequence=bad)


def test_wrong_payload_kind_rejected():
    with pytest.raises(ValueError):
        replace(event(), event_type=EventType.QUOTE)


def test_semantic_bar_and_quote_validation():
    with pytest.raises(ValueError):
        QuotePayload(Decimal("102"), Decimal("101"), Decimal("1"), Decimal("1"))
    with pytest.raises(ValueError):
        BarPayload(Decimal("100"), Decimal("99"), Decimal("98"), Decimal("100"), Decimal("1"), 60)


def test_immutable_contracts():
    with pytest.raises(FrozenInstanceError):
        instrument().symbol = "CHANGED"
    with pytest.raises(FrozenInstanceError):
        event().sequence = 10


def test_unknown_schema_versions_and_asset_classes_rejected():
    with pytest.raises(ValueError):
        replace(instrument(), schema_version="2")
    with pytest.raises(ValueError):
        replace(event(), schema_version="2")
    with pytest.raises(ValueError):
        replace(instrument(), asset_class="unknown")


def test_invalid_wire_shape_rejected():
    check = validator("canonical-market-event.schema.json")
    wire = event().to_dict()
    wire["payload"]["price"] = 100.1
    with pytest.raises(ValidationError):
        check.validate(wire)
    wire = event().to_dict()
    wire["event_type"] = "quote"
    with pytest.raises(ValidationError):
        check.validate(wire)
    wire = event().to_dict()
    wire["account"] = "unexpected"
    with pytest.raises(ValidationError):
        check.validate(wire)
