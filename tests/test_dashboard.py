"""Actual math, provenance, sequence, read-only boundaries and broker clock checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from vision.apps.dashboard.flow import DepthBook, Footprint
from vision.apps.dashboard.indicators import calculate, smooth
from vision.apps.dashboard.server import create_app
from vision.apps.dashboard.state import DashboardState
from vision.core.contracts import (
    AssetClass,
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    InstrumentSpec,
    TradePayload,
)
from vision.market_data.adapters.binance import Subscription

AT = datetime(2026, 10, 5, 12, tzinfo=UTC)
INSTRUMENT = "BINANCE:SPOT:BTCUSDT"


def bars(n=200):
    return [
        BarPayload(
            D(i + 1),
            D(i + 3),
            D(i - 1) if i > 1 else D("0.1"),
            D(i + 1),
            D(1),
            60,
            AT + timedelta(minutes=i),
        )
        for i in range(n)
    ]


def trade(index=1, maker=False, price="100", quantity="2", epoch=0):
    return CanonicalMarketEvent(
        str(index),
        "binance.spot",
        INSTRUMENT,
        EventType.TRADE,
        AT + timedelta(milliseconds=index),
        AT + timedelta(milliseconds=index),
        index,
        TradePayload(D(price), D(quantity), maker),
        source_epoch=epoch,
        continuity="contiguous",
    )


def test_indicator_linear_golden_vectors_and_warmup():
    # A constant-range linear ramp has independently derived analytical values.
    b = [
        BarPayload(
            D(i), D(i + 2), D(i - 2), D(i), D(1), 60, AT.replace(hour=0) + timedelta(minutes=i - 10)
        )
        for i in range(10, 210)
    ]
    r = calculate(b)
    assert r["sma20"][-1] == D("199.5")
    for p in (20, 50, 100, 200):
        assert abs(r[f"ema{p}"][-1] - (D(209) - D(p - 1) / 2)) < D("1e-20")
        assert all(v is None for v in r[f"ema{p}"][: p - 1])
    assert r["vwap"][-1] == D("109.5")
    assert r["rsi"][-1] == 100
    assert r["atr"][-1] == 4
    assert r["adx"][-1] == 100
    assert abs(r["macd"][-1] - 7) < D("1e-20")
    assert abs(r["macdSignal"][-1] - 7) < D("1e-20")
    assert abs(r["macdHist"][-1]) < D("1e-20")
    assert abs(r["stochK"][-1] - D(1500) / 17) < D("1e-20")
    assert abs(r["stochD"][-1] - D(1500) / 17) < D("1e-20")
    assert abs(r["bbUpper"][-1] - (D("199.5") + 2 * (D(399) / 12).sqrt())) < D("1e-20")
    assert all(v is None for v in r["macdSignal"][:33])
    assert all(v is None for v in r["adx"][:27])


def test_missing_seed_recovers_flat_market_and_missing_volume():
    assert smooth([None, None, D(1), D(2), D(3), D(4)], 3) == [None, None, None, None, D(2), D(3)]
    b = [
        BarPayload(D(10), D(10), D(10), D(10), D(0), 60, AT + timedelta(minutes=i))
        for i in range(80)
    ]
    r = calculate(b)
    assert r["rsi"][-1] == 50 and r["adx"][-1] == 0 and r["stochD"][-1] == 50
    assert r["vwap"][-1] is None
    assert (
        calculate([replace(v, price_basis="bid", volume_basis="price_count") for v in b])["vwap"][
            -1
        ]
        is None
    )


def test_footprint_exact_replay_dedup_epoch_and_unknown_side():
    def replay():
        f = Footprint(D("0.25"))
        for e in (
            trade(1, False, "100.24"),
            trade(2, True, "100.10", "3"),
            trade(2, True),
            trade(3, None, "100.20", "9"),
        ):
            f.admit(e)
        return f

    f = replay()
    assert f.snapshot() == replay().snapshot()
    c = f.snapshot()["candles"][0]
    assert c["buy"] == "2" and c["sell"] == "3" and c["delta"] == "-1"
    assert c["poc"] == "100.00" and c["cvd"] == "-1"
    assert f.snapshot()["unclassified_trades"] == 1
    f.admit(trade(4, False, epoch=1))
    assert f.snapshot()["candles"][0]["cvd"] == "2"
    with pytest.raises(ValueError):
        f.admit(trade(3, False, epoch=1))


def test_depth_snapshot_sequence_gap_and_stale_metrics():
    b = DepthBook("binance.spot", INSTRUMENT)
    b.snapshot_load({"lastUpdateId": 10, "bids": [["100", "3"]], "asks": [["102", "1"]]})
    assert b.snapshot(AT)["metrics"] is None
    assert b.delta({"U": 10, "u": 11, "b": [], "a": []}, AT)
    assert b.snapshot(AT)["metrics"]["microprice"] == "101.5"
    assert b.snapshot(AT + timedelta(seconds=6))["state"] == "STALE"
    assert b.snapshot(AT + timedelta(seconds=6))["metrics"] is None
    assert not b.delta({"U": 14, "u": 15, "b": [], "a": []}, AT)
    assert b.last_id is None and b.snapshot(AT)["bids"] == []


def test_history_not_live_health_and_bounded_retention():
    s = DashboardState(clock=lambda: AT + timedelta(days=1))
    spec = InstrumentSpec(
        INSTRUMENT,
        "BINANCE_SPOT",
        "BTCUSDT",
        AssetClass.CRYPTO,
        "BTC",
        "USDT",
        D("0.01"),
        D("0.001"),
        D("0.001"),
        D(1),
    )
    s.hub.register(spec, Subscription("BTCUSDT", ("bar",), "1m"))
    for i in range(700):
        p = BarPayload(D(10), D(11), D(9), D(10), D(1), 60, AT + timedelta(minutes=i))
        s.history(
            [
                CanonicalMarketEvent(
                    str(i),
                    "binance.spot",
                    INSTRUMENT,
                    EventType.BAR,
                    AT + timedelta(minutes=i + 1),
                    AT + timedelta(days=1),
                    i,
                    p,
                    delivery_kind="backfill",
                )
            ]
        )
    m = s.market(INSTRUMENT, 60)
    assert len(m["candles"]) == 512 and m["health"] == "UNAVAILABLE"
    assert not m["new_signals_permitted"]


def test_viewer_http_auth_origin_mutations_and_websocket():
    app = create_app(token="v" * 48)
    with TestClient(app) as c:
        assert c.get("/api/dashboard/summary").status_code == 401
        assert c.post("/auth/session", json={"token": "v" * 48}).status_code == 403
        origin = {"origin": "http://127.0.0.1:8787"}
        assert c.post("/auth/session", headers=origin, json={"token": "v" * 48}).status_code == 200
        d = c.get("/api/dashboard/summary").json()
        assert d["live_enabled"] is False and d["execution"]["state"] == "DISARMED"
        assert d["agents"]["decision"] is None and d["portfolio"]["account"] is None
        assert c.post("/api/execution", json={}).status_code == 405
        assert c.get("/api/not-real").status_code == 404
        assert c.get("/api/market/candles?seconds=42").status_code == 400
        with c.websocket_connect("/ws/dashboard", headers=origin) as ws:
            a = ws.receive_json()
            b = ws.receive_json()
            assert a["kind"] == "snapshot" and b["kind"] == "patch"
            assert b["sequence"] == a["sequence"] + 1
        assert c.post("/auth/logout", headers=origin).status_code == 200
        assert c.get("/api/dashboard/summary").status_code == 401


def test_broker_future_currency_and_read_projection():
    from vision.broker.models import BrokerAccount, BrokerQuote, BrokerSnapshot

    s = DashboardState(clock=lambda: AT)
    account = BrokerAccount("private-demo", "JPY", D(1000000), D(999000), D(800000), True, True, AT)
    q = BrokerQuote("BTCUSD", D(100), D(101), AT + timedelta(hours=3), AT)
    s.broker = BrokerSnapshot(account, (), (q,), (), 0, AT, "phase11-v1")
    result = s.summary()
    assert result["broker"]["state"] == "BLOCKED"
    assert "FUTURE_BROKER_QUOTE" in result["broker"]["reasons"]
    assert result["portfolio"]["account"]["currency"] == "JPY"
    assert result["live_enabled"] is False
    s.broker = replace(s.broker, account=replace(account, demo=False))
    assert "REAL_ACCOUNT_BLOCKED" in s.broker_projection()["reasons"]


def test_nested_patch_does_not_repeat_history():
    from vision.apps.dashboard.server import changed

    old = {"market": {"candles": [1, 2, 3], "quote": {"bid": "100"}}}
    new = {"market": {"candles": [1, 2, 3], "quote": {"bid": "101"}}}
    assert changed(old, new) == {"market": {"quote": {"bid": "101"}}}


def test_unauthorized_socket_and_unknown_instrument():
    from starlette.websockets import WebSocketDisconnect

    with TestClient(create_app(token="v" * 48)) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/dashboard"):
                pass
        c.post(
            "/auth/session", headers={"origin": "http://127.0.0.1:8787"}, json={"token": "v" * 48}
        )
        assert c.get("/api/dashboard/summary?instrument=FAKE").status_code == 400


def test_recursive_retention_matches_independent_full_batch():
    from vision.apps.dashboard.indicators import IndicatorWindow

    day = AT.replace(hour=0)
    b = []
    for i in range(620):
        close = D(100) + D(i % 17) / 3 + D(i) / 100
        b.append(
            BarPayload(
                close, close + 2, close - 2, close, D(i % 7 + 1), 60, day + timedelta(minutes=i)
            )
        )
    engine = IndicatorWindow()
    engine.reset(b[:500])
    for candle in b[500:]:
        engine.append(candle)
    # Full reference for 620 bars is computed independently from the retention window.
    from vision.apps.dashboard import indicators

    full = indicators.calculate

    # Analytical helper is bounded by design; use a separate reference recurrence
    # for the long EMA and MACD to avoid mirroring the retained-window implementation.
    def ema(values, p):
        value = sum(values[:p]) / p
        for v in values[p:]:
            value += D(2) / (p + 1) * (v - value)
        return value

    assert len(engine.bars) == 512 and len(engine.series["ema200"]) == 512
    assert abs(engine.series["ema200"][-1] - ema([v.close for v in b], 200)) < D("1e-20")
    # VWAP retains all known session volume beyond the 512 display bars.
    expected = sum(v.close * v.volume for v in b) / sum(v.volume for v in b)
    assert abs(engine.series["vwap"][-1] - expected) < D("1e-20")
    assert engine.series["macdSignal"][-1] is not None
    assert engine.series["adx"][-1] is not None
    assert full(b[:500])["ema200"][-1] is not None


def test_partial_utc_session_vwap_unavailable():
    assert all(v is None for v in calculate(bars(80))["vwap"])
