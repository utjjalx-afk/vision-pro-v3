"""Synthetic v20 wire contracts and session/continuity safety; no credentials/network."""

import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import jsonschema
import pytest

from vision.analysis.context import capture_from_hub, quality_reasons
from vision.analysis.contracts import wire
from vision.core.bus.events import BackpressureError, EventBus
from vision.core.codec import event_from_dict
from vision.core.contracts import AssetClass, InstrumentSpec
from vision.core.instruments import InstrumentRecord, QuantityUnit, SpecProvenance
from vision.market_data.adapters.binance import MarketDataError
from vision.market_data.adapters.oanda import (
    OandaNormalizer,
    OandaREST,
    parse_metadata,
    provider_time,
    time_sequence,
)
from vision.market_data.forex import replay, snapshot
from vision.market_data.fx_sources import FXSourceSelector, NoFXConversion
from vision.market_data.health.gate import HealthStatus, stream_key
from vision.market_data.hub import MarketDataHub
from vision.market_data.sessions import SessionCalendar, SessionState, calendar_from_dict

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/forex/oanda_replay.json"


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def calendar(symbol="EUR_USD", **changes):
    opening = 17 * 60 + 5 if symbol == "EUR_USD" else 18 * 60 + 5
    closing = 16 * 60 + 59
    weekly = ((6, opening, 1440), (4, 0, closing)) + tuple(
        window for day in range(4) for window in ((day, 0, closing), (day, opening, 1440))
    )
    return SessionCalendar(
        **{
            "calendar_id": f"synthetic-{symbol}-2026",
            "reference": "synthetic-calendar-fixture",
            "timezone": "America/New_York",
            "valid_from": dt("2026-01-01T00:00:00Z"),
            "valid_until": dt("2027-01-01T00:00:00Z"),
            "weekly": weekly,
            "overrides": (("2026-12-25", ()),),
            "exceptions_verified": True,
            "timezone_rules_version": "2026.5",
            **changes,
        }
    )


def metadata(symbol="EUR_USD", environment="practice", at=None):
    return parse_metadata(meta_row(symbol), environment, at or dt("2026-03-06T20:00:00Z"))


def meta_row(symbol):
    return {
        "name": symbol,
        "type": "CURRENCY" if symbol == "EUR_USD" else "METAL",
        "pipLocation": -4 if symbol == "EUR_USD" else -2,
        "displayPrecision": 5 if symbol == "EUR_USD" else 3,
        "tradeUnitsPrecision": 0,
        "minimumTradeSize": "1",
    }


def quote_row(at="2026-03-06T20:00:00.123456789Z", symbol="EUR_USD", price="1.10000"):
    return {
        "instrument": symbol,
        "time": at,
        "status": "tradeable",
        "bids": [{"price": price, "liquidity": 100}],
        "asks": [{"price": str(Decimal(price) + Decimal("0.0002")), "liquidity": 150}],
    }


def candle_row(at, *, complete=True):
    return {
        "time": at,
        "complete": complete,
        "volume": 23,
        "bid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
        "ask": {"o": "1.1002", "h": "1.2002", "l": "1.0002", "c": "1.1502"},
    }


def candles(*opens, symbol="EUR_USD"):
    return {"instrument": symbol, "granularity": "M1", "candles": [candle_row(v) for v in opens]}


def normalizer(symbol="EUR_USD", epoch=1):
    return OandaNormalizer(symbol, "practice", source_epoch=epoch)


def hub_at(at, symbol="EUR_USD", **kwargs):
    clock = [dt(at)]
    hub = MarketDataHub(clock=lambda: clock[0], **kwargs)
    hub.register_market(metadata(symbol), calendar(symbol), granularity="M1")
    return hub, clock


@pytest.mark.parametrize(
    "symbol,asset,pip",
    [
        ("EUR_USD", AssetClass.FOREX, "0.0001"),
        ("XAU_USD", AssetClass.METALS, "0.01"),
        ("XAG_USD", AssetClass.METALS, "0.01"),
    ],
)
def test_canonical_instruments_do_not_invent_economics(symbol, asset, pip):
    m = metadata(symbol)
    assert m.canonical.asset_class is asset
    assert str(m.pip_size) == pip
    assert m.canonical.key.endswith("/USD")
    hub, _ = hub_at("2026-03-06T20:00:01Z", symbol)
    event = normalizer(symbol).quote(quote_row(symbol=symbol), hub.clock())
    assert hub.ingest(event).accepted
    assert hub.registry.get(m.instrument_id) is None
    assert not hub.status()["market_metadata"][m.instrument_id]["execution_spec_ready"]
    assert event_from_dict(event.to_dict()) == event


def test_verified_spec_integration_requires_exact_metadata_binding():
    m = metadata()
    # Synthetic verified record demonstrates integration only, not real FX economics.
    spec = InstrumentSpec(
        m.instrument_id,
        "OANDA_V20",
        m.symbol,
        AssetClass.FOREX,
        "EUR",
        "USD",
        Decimal("0.00001"),
        Decimal(1),
        Decimal(1),
        Decimal(1),
    )
    record = InstrumentRecord(
        spec,
        m.canonical,
        QuantityUnit.BASE,
        SpecProvenance(
            m.source,
            "synthetic-verified-unit-fixture",
            m.observed_at,
            m.metadata_digest,
            "synthetic-unit-economics",
        ),
        "unsupported",
    )
    hub = MarketDataHub(clock=lambda: m.observed_at)
    hub.register_market(m, calendar(), granularity="M1", record=record)
    assert hub.registry.get(m.instrument_id) == record
    with pytest.raises(ValueError):
        m.verify_record(
            replace(record, provenance=replace(record.provenance, metadata_digest="f" * 64))
        )
    newer = parse_metadata(
        {**meta_row(m.symbol), "minimumTradeSize": "2"}, "practice", m.observed_at
    )
    hub.register_market(newer, calendar(), granularity="M1")
    assert hub.registry.get(m.instrument_id) is None


def test_nanosecond_sequence_and_offset_utc_normalization():
    at = "2026-03-06T15:00:00.123456789-05:00"
    assert provider_time(at) == dt("2026-03-06T20:00:00.123456Z")
    assert time_sequence(at) == time_sequence("2026-03-06T20:00:00.123456789Z")
    event = normalizer().quote(quote_row(at), dt("2026-03-06T20:00:01Z"))
    assert event.provider_sequence == at
    other = normalizer().quote(quote_row("2026-03-06T20:00:00.123456790Z"), event.received_ts)
    assert other.sequence == event.sequence + 1
    assert other.event_id != event.event_id


@pytest.mark.parametrize(
    "bad",
    [
        "2026-03-06",
        "2026-03-06T20:00:00",
        "bad",
        "2026-03-06T20:00:00.1234567890Z",
        "2026-99-99T00:00:00Z",
    ],
)
def test_bad_provider_time_rejected(bad):
    with pytest.raises(ValueError):
        provider_time(bad)


@pytest.mark.parametrize(
    "change",
    [
        {"instrument": "BTC_USD"},
        {"status": "non-tradeable"},
        {"bids": []},
        {"bids": [{"price": "NaN", "liquidity": 1}]},
        {"bids": [{"price": "1.2", "liquidity": 1}]},
        {"bids": [{"price": "1.1", "liquidity": True}]},
        {"bids": [{"price": "1.1", "liquidity": 0}]},
        {"asks": [{"price": "1.3", "liquidity": 1}, {"price": "1.2", "liquidity": 1}]},
    ],
)
def test_unavailable_malformed_crossed_quotes_rejected(change):
    with pytest.raises((ValueError, KeyError)):
        normalizer().quote({**quote_row(), **change}, dt("2026-03-06T20:00:01Z"))


def test_bid_ask_bars_preserve_price_count_volume_and_identity():
    rows = normalizer().candles(
        candles("2026-03-06T20:00:00Z"),
        dt("2026-03-06T20:01:01Z"),
        granularity="M1",
        delivery_kind="live",
    )
    a, b = rows
    assert a.payload.price_basis == "bid" and b.payload.price_basis == "ask"
    assert a.payload.volume_basis == "price_count" and a.payload.volume == 23
    assert a.event_id != b.event_id and stream_key(a) != stream_key(b)
    assert event_from_dict(a.to_dict()) == a
    schema = json.loads((ROOT / "schemas/canonical-market-event.schema.json").read_text())
    for event in rows:
        jsonschema.validate(event.to_dict(), schema)
    with pytest.raises(ValueError):
        replace(a.payload, volume_basis=None)


@pytest.mark.parametrize(
    "change",
    [
        {"complete": "true"},
        {"volume": True},
        {"time": "2026-03-06T20:00:01Z"},
        {"bid": {"o": "1", "h": "0", "l": "1", "c": "1"}},
    ],
)
def test_bad_candles_rejected(change):
    payload = candles("2026-03-06T20:00:00Z")
    payload["candles"][0].update(change)
    with pytest.raises(ValueError):
        normalizer().candles(
            payload, dt("2026-03-06T20:01:01Z"), granularity="M1", delivery_kind="live"
        )


def test_incomplete_future_duplicate_and_missing_side_candles():
    response = candles("2026-03-06T20:00:00Z")
    response["candles"][0]["complete"] = False
    assert not normalizer().candles(
        response, dt("2026-03-06T20:00:20Z"), granularity="M1", delivery_kind="live"
    )
    response["candles"][0]["complete"] = True
    with pytest.raises(ValueError):
        normalizer().candles(
            response, dt("2026-03-06T20:00:20Z"), granularity="M1", delivery_kind="live"
        )
    response["candles"] *= 2
    with pytest.raises(ValueError):
        normalizer().candles(
            response, dt("2026-03-06T20:02:00Z"), granularity="M1", delivery_kind="live"
        )
    response["candles"] = [candle_row("2026-03-06T20:00:00Z")]
    del response["candles"][0]["ask"]
    with pytest.raises(KeyError):
        normalizer().candles(
            response, dt("2026-03-06T20:02:00Z"), granularity="M1", delivery_kind="live"
        )


@pytest.mark.parametrize(
    "at,expected",
    [
        ("2026-03-06T21:58:59Z", SessionState.OPEN),
        ("2026-03-06T21:59:00Z", SessionState.MARKET_CLOSED),
        ("2026-03-07T12:00:00Z", SessionState.MARKET_CLOSED),
        ("2026-03-08T21:04:59Z", SessionState.MARKET_CLOSED),
        ("2026-03-08T21:05:00Z", SessionState.OPEN),
        ("2026-03-09T20:59:00Z", SessionState.MARKET_CLOSED),
        ("2026-03-09T21:05:00Z", SessionState.OPEN),
        ("2026-11-01T22:05:00Z", SessionState.OPEN),
        ("2026-12-25T15:00:00Z", SessionState.MARKET_CLOSED),
        ("2027-01-01T00:00:00Z", SessionState.CALENDAR_UNKNOWN),
    ],
)
def test_weekend_daily_break_dst_holiday_and_expiry(at, expected):
    assert calendar().state(dt(at)) is expected


@pytest.mark.parametrize("symbol", ["XAU_USD", "XAG_USD"])
def test_metals_session_opens_later_than_forex(symbol):
    assert calendar(symbol).state(dt("2026-03-08T21:05:00Z")) is SessionState.MARKET_CLOSED
    assert calendar(symbol).state(dt("2026-03-08T22:05:00Z")) is SessionState.OPEN


@pytest.mark.parametrize(
    "changes",
    [
        {"exceptions_verified": False},
        {"timezone_rules_version": "unknown"},
        {"timezone": "../bad"},
        {"weekly": ((True, 0, 1440),)},
        {"weekly": ((0, 0, 60), (0, 30, 90))},
        {"weekly": ((0, 60, 0),)},
        {"overrides": (("2026-12-25", ()), ("2026-12-25", ()))},
        {"valid_until": dt("2028-01-01T00:00:00Z")},
    ],
)
def test_calendar_never_guesses_ambiguous_coverage(changes):
    with pytest.raises(ValueError):
        calendar(**changes)


def test_closed_health_preserves_disconnect_and_blocks_downstream_lanes():
    hub, clock = hub_at("2026-03-06T20:00:01Z")
    event = normalizer().quote(quote_row(), clock[0])
    assert hub.ingest(event).accepted
    hub.gate.disconnected()
    clock[0] = dt("2026-03-07T12:00:00Z")
    assert set(hub.status()["streams"].values()) == {"market_closed"}
    assert not hub.ingest(replace(event, received_ts=clock[0])).accepted
    context = capture_from_hub(hub, event.instrument_id, (event,))
    assert "DATA_MARKET_CLOSED" in quality_reasons(context)
    clock[0] = dt("2026-03-08T21:05:00Z")
    assert hub.status()["streams"][stream_key(event)] == "disconnected"
    fresh = normalizer(epoch=2).quote(quote_row("2026-03-08T21:05:00Z"), clock[0])
    assert hub.ingest(fresh).accepted
    assert not hub.ingest(
        replace(fresh, source_epoch=1, event_id="regressed", sequence=fresh.sequence + 1)
    ).accepted
    clock[0] = dt("2027-01-01T00:00:00Z")
    assert set(hub.status()["streams"].values()) == {"calendar_unknown"}


def bar(at, received, *, epoch=1, delivery="live"):
    return normalizer(epoch=epoch).candles(
        candles(at), received, granularity="M1", delivery_kind=delivery
    )[0]


def test_session_gap_is_skipped_but_missing_open_bar_is_latched():
    hub, clock = hub_at("2026-03-06T21:58:01Z")
    assert hub.ingest(bar("2026-03-06T21:57:00Z", clock[0])).accepted
    clock[0] = dt("2026-03-08T21:06:01Z")
    pending = bar("2026-03-08T21:05:00Z", clock[0], epoch=2)
    # Friday 16:58 was still OPEN and must not be hidden by the weekend.
    assert hub.ingest(pending).status is HealthStatus.GAP
    history = (bar("2026-03-06T21:58:00Z", clock[0], epoch=2, delivery="backfill"),)
    assert hub.recover_market_bar(pending, history).accepted
    assert list(hub.recovered) == list(history)
    assert history[0] not in hub.bus.drain()
    clock[0] = dt("2026-03-08T21:08:01Z")
    assert hub.ingest(bar("2026-03-08T21:07:00Z", clock[0], epoch=2)).status is HealthStatus.GAP


def test_weekend_with_complete_prior_history_is_continuous():
    hub, clock = hub_at("2026-03-06T21:59:00Z")
    clock[0] = dt("2026-03-06T21:58:59Z")
    # Last open candle ends precisely at closure. Admit later using explicit history recovery
    # cursor for this synthetic continuity-only fixture.
    previous = bar("2026-03-06T21:58:00Z", dt("2026-03-06T21:59:00Z"))
    hub.gate.commit(previous)
    clock[0] = dt("2026-03-08T21:06:00Z")
    assert hub.ingest(bar("2026-03-08T21:05:00Z", clock[0], epoch=2)).accepted


def test_recovery_atomicity_provenance_and_backpressure():
    hub, clock = hub_at("2026-03-06T20:01:01Z", bus=EventBus(capacity=1))
    assert hub.ingest(bar("2026-03-06T20:00:00Z", clock[0])).accepted
    clock[0] = dt("2026-03-06T20:03:01Z")
    pending = bar("2026-03-06T20:02:00Z", clock[0], epoch=2)
    assert hub.ingest(pending).status is HealthStatus.GAP
    history = (bar("2026-03-06T20:01:00Z", clock[0], epoch=2, delivery="backfill"),)
    key = stream_key(pending)
    with pytest.raises(ValueError):
        hub.recover_market_bar(pending, (replace(history[0], source_epoch=1),))
    with pytest.raises(ValueError):
        hub.recover_market_bar(pending, ())
    with pytest.raises(BackpressureError):
        hub.recover_market_bar(pending, history)
    assert hub.gate.streams[key].gap and not hub.recovered
    hub.bus.drain()
    assert hub.recover_market_bar(pending, history).accepted
    assert hub.bus.drain() == [pending]
    assert not hub.ingest(history[0]).accepted


def test_stale_future_duplicate_and_unregistered_data_do_not_advance_cursor():
    hub, clock = hub_at("2026-03-06T20:00:01Z")
    event = normalizer().quote(quote_row(), clock[0])
    assert hub.ingest(event).accepted
    for wrong in [
        event,
        replace(event, event_id="future", source_ts=clock[0] + timedelta(seconds=10)),
        replace(event, event_id="stale", received_ts=clock[0] - timedelta(seconds=10)),
        replace(event, instrument_id="OANDA:PRACTICE:UNKNOWN"),
    ]:
        assert not hub.ingest(wrong).accepted
    assert hub.gate.streams[stream_key(event)].last_event == event


class Reply:
    status = 200

    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        return self.raw[:size]


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return Reply(value)


def rest_with(responses):
    opener = Opener(responses)
    delays = []
    rest = OandaREST(
        token="synthetic-token",
        account_id="synthetic-account",
        environment="practice",
        opener=opener,
        sleep=delays.append,
        monotonic=lambda: 1,
    )
    return rest, opener, delays


def test_read_only_authorized_transport_fixed_host_paths_pacing():
    rest, opener, delays = rest_with([b"{}"] * 3)
    rest.metadata("EUR_USD")
    rest.pricing("XAU_USD")
    rest.candles("XAG_USD", "M1")
    assert delays == [0.25, 0.25]
    assert all(v.get_method() == "GET" for v in opener.requests)
    assert all(
        v.full_url.startswith("https://api-fxpractice.oanda.com/v3/") for v in opener.requests
    )
    assert all(v.get_header("Authorization") == "Bearer synthetic-token" for v in opener.requests)
    assert "price=BA" in opener.requests[-1].full_url
    assert "includeHomeConversions=false" in opener.requests[1].full_url
    assert not hasattr(rest, "orders") and not hasattr(rest, "get")
    with pytest.raises(ValueError):
        rest._get("/v3/accounts/synthetic-account/orders", {})
    with pytest.raises(ValueError):
        rest.candles("EUR_USD", "D")
    with pytest.raises(ValueError):
        rest.metadata("../orders")
    assert len(opener.requests) == 3


@pytest.mark.parametrize(
    "bad",
    [
        b'{"secret":1,"secret":2}',
        b'{"secret":NaN}',
        b"x" * 2000001,
        HTTPError("https://secret", 401, "synthetic-token", {}, None),
        URLError("synthetic-account"),
    ],
    ids=["duplicate-json", "non-finite-json", "oversized-body", "unauthorized", "network-error"],
)
def test_transport_errors_redact_account_token_and_remote_body(bad):
    rest, _, _ = rest_with([bad])
    with pytest.raises(MarketDataError) as error:
        rest.pricing("EUR_USD")
    assert "synthetic" not in str(error.value) and "secret" not in str(error.value)


def test_snapshot_closed_or_unknown_does_no_network():
    rest, opener, _ = rest_with([])
    result = snapshot(
        rest, "EUR_USD", "M1", calendar(), clock=lambda: dt("2026-03-07T12:00:00Z"), source_epoch=1
    )
    assert result["session"] == "MARKET_CLOSED" and not opener.requests
    result = snapshot(
        rest, "EUR_USD", "M1", calendar(), clock=lambda: dt("2027-01-01T00:00:00Z"), source_epoch=1
    )
    assert result["session"] == "CALENDAR_UNKNOWN" and not opener.requests


def test_snapshot_public_projection_and_live_history_separation():
    payloads = [
        {"instruments": [{**meta_row("EUR_USD"), "account": "must-not-export"}]},
        {"prices": [quote_row("2026-03-06T20:02:00Z")], "account": "must-not-export"},
        candles("2026-03-06T20:00:00Z", "2026-03-06T20:01:00Z"),
    ]
    rest, _, _ = rest_with([json.dumps(v).encode() for v in payloads])
    result = snapshot(
        rest, "EUR_USD", "M1", calendar(), clock=lambda: dt("2026-03-06T20:02:01Z"), source_epoch=1
    )
    assert len(result["events"]) == 3
    assert "must-not-export" not in json.dumps(result)
    assert all(v["delivery_kind"] == "live" for v in result["events"])


def test_failover_cannot_pair_oanda_practice_and_live():
    hub, _ = hub_at("2026-03-06T20:00:01Z")
    m = metadata(environment="live")
    hub.register_market(m, calendar(), granularity="M1")
    with pytest.raises(ValueError):
        FXSourceSelector(hub, metadata().instrument_id, m.instrument_id)
    assert (
        NoFXConversion().convert(Decimal(100), "USD", "USDT", as_of=hub.clock()).reason
        == "FX_CONVERSION_REQUIRED"
    )


def fixture_selector(price="1.10000"):
    """A synthetic second-provider stub tests the future framework; no live adapter claim."""
    hub, clock = hub_at("2026-03-06T20:00:01Z")
    primary = metadata().instrument_id
    standby = "SYNTHETIC:EURUSD"
    hub.market_instruments[standby] = SimpleNamespace(
        canonical=metadata().canonical, source="synthetic.fixture"
    )
    a = normalizer().quote(quote_row(), clock[0])
    b = replace(
        normalizer().quote(quote_row(price=price), clock[0]),
        event_id="fixture-b",
        instrument_id=standby,
        source="synthetic.fixture",
    )
    hub.sequence_steps[stream_key(b)] = None
    hub.gate.register_session(stream_key(b), calendar())
    hub.ingest(a)
    hub.ingest(b)
    # Provider selection here is quote-only; actual registered OANDA stream bundle includes bars.
    for key in list(hub.sequence_steps):
        if ":bar:" in key:
            del hub.sequence_steps[key]
    selector = FXSourceSelector(hub, primary, standby)
    selector.observe(a)
    selector.observe(b)
    return selector, a, b, clock


def test_failover_requires_recent_agreement_and_retains_venue_lineage():
    selector, a, b, _ = fixture_selector()
    selector.hub.gate.disconnected((stream_key(a),))
    assert selector.select(b) == b
    assert selector.selection_epoch == 1 and selector.active == b.instrument_id
    assert selector.status()["execution_migration_enabled"] is False
    bad, a, b, _ = fixture_selector("1.50000")
    assert bad.divergent and bad.select(b) is None


def test_failover_expired_evidence_and_closed_market_are_not_switches():
    selector, a, b, clock = fixture_selector()
    clock[0] += timedelta(seconds=40)
    selector.hub.gate.disconnected((stream_key(a),))
    assert selector.select(b) is None
    clock[0] = dt("2026-03-07T12:00:00Z")
    assert selector.select(b) is None and selector.active == a.instrument_id


def test_fixture_replay_is_deterministic_and_decimal_context_independent():
    fixture = json.loads(FIXTURE.read_text())
    result = replay(fixture)
    with localcontext() as context:
        context.prec = 3
        assert replay(fixture) == result
    assert result["trace"][-1]["status"]["streams"]
    assert set(result["trace"][-1]["status"]["streams"].values()) == {"market_closed"}
    tampered = copy.deepcopy(fixture)
    tampered["actions"][0]["at"] = "2026-03-05T00:00:00Z"
    with pytest.raises(ValueError):
        replay(tampered)
    assert calendar_from_dict(wire(calendar())) == calendar()


@pytest.mark.parametrize(
    "flag",
    [
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ],
)
def test_forex_cli_global_guards_before_file_or_network(flag):
    result = subprocess.run(
        [sys.executable, "-m", "vision", "forex-replay", "missing.json"],
        env={**os.environ, flag: "true"},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2 and "must be false" in result.stderr
    assert "Forex data" not in result.stderr


def test_forex_cli_offline_replay_and_safe_missing_credentials():
    result = subprocess.run(
        [sys.executable, "-m", "vision", "forex-replay", str(FIXTURE)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == replay(json.loads(FIXTURE.read_text()))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vision",
            "forex-data",
            "--symbol",
            "EUR_USD",
            "--environment",
            "practice",
            "--granularity",
            "M1",
            "--calendar",
            "missing.json",
            "--source-epoch",
            "1",
        ],
        env={**os.environ, "OANDA_API_TOKEN": "", "OANDA_ACCOUNT_ID": ""},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 2 and "BLOCKED" in result.stderr
    assert "missing.json" not in result.stderr


def test_replay_reconnect_gap_recovery_reconstructs_same_epochs_and_events():
    value = json.loads(FIXTURE.read_text())
    value["actions"] = [
        {
            "at": "2026-03-06T20:01:01Z",
            "kind": "candles",
            "payload": candles("2026-03-06T20:00:00Z"),
        },
        {"at": "2026-03-06T20:01:02Z", "kind": "disconnect", "payload": None},
        {"at": "2026-03-06T20:02:00Z", "kind": "reconnect", "payload": {"source_epoch": 2}},
        {
            "at": "2026-03-06T20:03:01Z",
            "kind": "candles",
            "payload": candles("2026-03-06T20:02:00Z"),
        },
        {
            "at": "2026-03-06T20:03:02Z",
            "kind": "recover",
            "payload": {
                "pending": candles("2026-03-06T20:02:00Z"),
                "history": candles("2026-03-06T20:01:00Z"),
            },
        },
    ]
    result = replay(value)
    assert replay(json.loads(json.dumps(value))) == result
    assert result["trace"][3]["events"] == []
    assert all(v["status"] == "gap" for v in result["trace"][3]["decisions"])
    final = result["trace"][-1]
    assert final["status"]["recovered_bars"] == 2
    assert len(final["events"]) == 2 and all(v["source_epoch"] == 2 for v in final["events"])
    assert all(v["delivery_kind"] == "live" for v in final["events"])
    value["actions"][2]["payload"]["source_epoch"] = 1
    with pytest.raises(ValueError):
        replay(value)


def test_stale_gap_candidate_does_not_latch_missing_history():
    hub, clock = hub_at("2026-03-06T20:01:01Z")
    hub.ingest(bar("2026-03-06T20:00:00Z", clock[0]))
    clock[0] = dt("2026-03-06T20:10:00Z")
    event = bar("2026-03-06T20:02:00Z", clock[0])
    assert hub.ingest(event).status is HealthStatus.STALE
    assert not hub.gate.streams[stream_key(event)].gap


def test_session_bar_shape_rejects_forged_alignment_before_continuity():
    hub, clock = hub_at("2026-03-06T20:01:01Z")
    event = bar("2026-03-06T20:00:00Z", clock[0])
    wrong = replace(event, sequence=event.sequence + 1)
    assert not hub.ingest(wrong).accepted
    assert hub.ingest(event).accepted
    wrong = replace(
        event,
        event_id="wrong-close",
        sequence=event.sequence + 60000,
        source_ts=event.source_ts + timedelta(seconds=60),
    )
    assert not hub.ingest(wrong).accepted


def test_invalid_registration_and_calendar_revision_leave_registry_unchanged():
    hub, _ = hub_at("2026-03-06T20:01:01Z")
    before = dict(hub.market_instruments)
    with pytest.raises(ValueError):
        hub.register_market(metadata("XAU_USD"), None, granularity="M1")
    assert hub.market_instruments == before
    with pytest.raises(ValueError):
        hub.register_market(metadata(), calendar(calendar_id="new-calendar"), granularity="M1")
    assert hub.market_instruments == before


def test_no_future_source_switch_after_epoch_restarts_without_new_agreement():
    selector, a, b, _ = fixture_selector()
    newer = replace(b, source_epoch=2, sequence=b.sequence + 1, event_id="new-epoch")
    assert selector.hub.ingest(newer).accepted
    selector.hub.gate.disconnected((stream_key(a),))
    assert selector.select(newer) is None
    assert selector.active == a.instrument_id and selector.evidence_at is None


def test_primary_session_closed_cannot_trigger_different_calendar_failover():
    selector, a, b, clock = fixture_selector()
    closed = calendar(overrides=(("2026-03-06", ()),))
    selector.hub.gate.calendars[stream_key(a)] = closed
    assert selector.usable(b.instrument_id)
    assert selector.select(b) is None
    assert selector.active == a.instrument_id


@pytest.mark.parametrize(
    "changes",
    [
        {"token": "bad\nheader"},
        {"account_id": "../orders"},
        {"environment": "other"},
        {"timeout": True},
    ],
)
def test_transport_configuration_no_unsafe_headers_paths_or_hosts(changes):
    with pytest.raises(ValueError):
        OandaREST(
            **{
                "token": "synthetic-token",
                "account_id": "synthetic-account",
                "environment": "practice",
                **changes,
            }
        )


def test_explicit_bounded_historical_request_and_redirect_rejection():
    rest, opener, _ = rest_with([b"{}"])
    rest.candles("EUR_USD", "M1", start=dt("2026-03-06T20:00:00Z"), end=dt("2026-03-06T20:02:00Z"))
    assert "from=" in opener.requests[0].full_url and "count=" not in opener.requests[0].full_url
    with pytest.raises(ValueError):
        rest.candles(
            "EUR_USD", "M1", start=dt("2026-03-06T20:00:00Z"), end=dt("2026-03-08T20:00:00Z")
        )
    with pytest.raises(ValueError):
        rest.candles("EUR_USD", "M1", count=True)
    from vision.market_data.adapters.binance import NoRedirect

    with pytest.raises(MarketDataError):
        NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example")


def test_calendar_roundtrip_gap_scan_and_unknown_coverage():
    cal = calendar()
    assert calendar_from_dict(wire(cal)) == cal
    expected = cal.expected_opens(dt("2026-03-06T21:58:00Z"), dt("2026-03-08T21:06:00Z"), 60)
    assert expected == (dt("2026-03-06T21:58:00Z"), dt("2026-03-08T21:05:00Z"))
    with pytest.raises(ValueError):
        cal.expected_opens(dt("2026-12-31T23:59:00Z"), dt("2027-01-01T00:02:00Z"), 60)
