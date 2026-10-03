import json
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from vision.core.codec import event_from_dict, instrument_from_dict
from vision.core.contracts import TimestampBasis
from vision.market_data.adapters.binance import (
    BinanceNormalizer,
    MalformedMarketData,
    Subscription,
)


@pytest.mark.parametrize("kind", ["trade", "quote", "bar"])
def test_normalized_wire_schema_round_trip(kind, request, normalizer, now):
    raw = request.getfixturevalue(f"raw_{kind}")
    event = normalizer.message(json.dumps(raw), now)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/canonical-market-event.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(event.to_dict())
    assert event_from_dict(json.loads(json.dumps(event.to_dict()))) == event


def test_rest_and_ws_trade_ids_agree(normalizer, raw_trade, now):
    rest = {key: raw_trade[key] for key in ("a", "p", "q", "T", "m")}
    assert normalizer.trade(rest, now) == normalizer.message(raw_trade, now)
    assert str(normalizer.trade(rest, now).payload.price) == "123.4567890123456789"


def test_quote_timestamp_provenance(normalizer, raw_quote, now):
    event = normalizer.message(raw_quote, now)
    assert event.timestamp_basis is TimestampBasis.RECEIPT
    assert event.source_ts == event.received_ts == now


def test_only_finalized_utc_bars(normalizer, raw_bar, now):
    raw_bar["k"]["x"] = False
    assert normalizer.message(raw_bar, now) is None
    raw_bar["k"]["x"] = True
    event = normalizer.message(raw_bar, now)
    assert event.payload.open_ts < event.source_ts
    assert event.sequence == raw_bar["k"]["t"]
    raw_bar["k"]["T"] += 1
    with pytest.raises(MalformedMarketData):
        normalizer.message(raw_bar, now)


def test_rest_closed_bar_identity(normalizer, raw_bar, now):
    kline = raw_bar["k"]
    row = [
        kline["t"],
        kline["o"],
        kline["h"],
        kline["l"],
        kline["c"],
        kline["v"],
        kline["T"],
        "0",
        0,
        "0",
        "0",
        "0",
    ]
    assert normalizer.rest_bar(row, now).event_id == normalizer.message(raw_bar, now).event_id
    row[6] += 60000
    assert normalizer.rest_bar(row, now) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("s", "ETHUSDT"),
        ("a", True),
        ("p", 1.0),
        ("q", "-1"),
        ("p", "NaN"),
        ("p", "0"),
        ("m", "true"),
        ("T", -1),
    ],
)
def test_malformed_trade_rejected(field, value, normalizer, raw_trade, now):
    raw_trade[field] = value
    with pytest.raises(MalformedMarketData, match="Invalid Binance market message"):
        normalizer.message(raw_trade, now)


@pytest.mark.parametrize(
    "raw",
    ["{", "[]", "null", "x" * 1_000_001],
    ids=["invalid-json", "array", "null", "oversized"],
)
def test_invalid_frames_rejected(raw, normalizer, now):
    with pytest.raises(MalformedMarketData):
        normalizer.message(raw, now)


def test_combined_stream_and_payload_must_match(normalizer, raw_trade, now):
    wrapped = {"stream": "btcusdt@aggTrade", "data": raw_trade}
    assert normalizer.message(wrapped, now).sequence == 100
    wrapped["stream"] = "btcusdt@bookTicker"
    with pytest.raises(MalformedMarketData):
        normalizer.message(wrapped, now)
    trade_only = BinanceNormalizer(Subscription("BTCUSDT", ("trade",)))
    with pytest.raises(MalformedMarketData):
        trade_only.message(wrapped, now)


def test_codec_rejects_unknown_fields_and_float(market_event):
    wire = market_event.to_dict()
    wire["private_account"] = "synthetic"
    with pytest.raises(ValueError):
        event_from_dict(wire)
    wire = market_event.to_dict()
    wire["payload"]["price"] = 123.5
    with pytest.raises(ValueError):
        event_from_dict(wire)


def test_instrument_round_trip(binance_instrument):
    assert instrument_from_dict(binance_instrument.to_dict()) == binance_instrument


def test_provenance_invariant(market_event):
    with pytest.raises(ValueError):
        replace(market_event, timestamp_basis=TimestampBasis.RECEIPT)


@pytest.mark.parametrize(
    "args",
    [
        ("btcusdt",),
        ("BTCUSDT", ()),
        ("BTCUSDT", ("trade", "trade")),
        ("BTCUSDT", ("depth",)),
        ("BTCUSDT", ("bar",), "1M"),
    ],
)
def test_invalid_subscription_rejected(args):
    with pytest.raises(ValueError):
        Subscription(*args)
