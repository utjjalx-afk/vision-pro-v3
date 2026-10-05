"""Native chart provenance, account gating, reconnect windows and report exports."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from threading import RLock
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from vision.apps.dashboard.mt5_market import SYMBOLS, DemoMarketReader
from vision.apps.dashboard.research import daily_report, markdown
from vision.apps.dashboard.runtime import start_public_epoch
from vision.apps.dashboard.server import create_app
from vision.apps.dashboard.state import DashboardState
from vision.broker.models import BrokerAccount, BrokerSnapshot, BrokerSpec

AT = datetime(2026, 10, 5, 12, tzinfo=UTC)


class Native:
    TIMEFRAME_M5 = 5

    def symbol_info(self, name):
        base = {"EURUSD": "EUR", "XAUUSD": "XAU", "XAGUSD": "XAG", "BTCUSD": "BTC"}[name]
        return NS(currency_base=base, currency_profit="USD", chart_mode=0)

    def symbol_info_tick(self, name):
        return NS(bid=100.0, ask=101.0, time_msc=int((AT + timedelta(hours=3)).timestamp() * 1000))

    def copy_rates_from_pos(self, name, frame, start, count):
        assert (start, count) == (1, 512)
        return [
            {
                "time": int((AT + timedelta(minutes=i)).timestamp()),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "tick_volume": 2,
            }
            for i in range(100)
        ]

    def __getattr__(self, name):
        raise AssertionError(f"Unapproved native operation: {name}")


class Reader:
    def __init__(self):
        self._native, self.lock, self.identity, self.demo = Native(), RLock(), "demo-pin", True

    def clock(self):
        return AT

    def account(self):
        if not self.demo:
            raise ValueError("CONNECTED_DEMO_ACCOUNT_REQUIRED")
        return NS(identity=self.identity)

    def snapshot(self):
        account = self.account()
        specs = tuple(
            BrokerSpec(
                k,
                v,
                D(".01"),
                D(100),
                D(".01"),
                D(0),
                D(".01"),
                D(".01"),
                D(1),
                D(1),
                D(1),
                0,
                0,
                4,
                "USD",
                "USD",
                AT,
                account.identity,
            )
            for k, v in SYMBOLS.items()
        )
        return BrokerSnapshot(
            BrokerAccount(account.identity, "USD", D(1000), D(1000), D(1000), True, True, AT),
            specs,
            (),
            (),
            0,
            AT,
            "phase11-v1",
        )


def test_native_history_preserves_future_clock_and_tick_volume_without_authorizing():
    reader = Reader()
    monitor = DemoMarketReader(reader)
    m = monitor.market("MT5:DEMO:EURUSD", 300)
    assert m["source"] == "mt5.demo" and len(m["candles"]) == 100
    assert m["quote_age_seconds"] == -10800 and m["health"] == "BLOCKED"
    assert m["candles"][0]["time"] == int(AT.timestamp())
    assert m["new_signals_permitted"] is False
    assert m["indicators"]["vwap"] == []  # Native tick count is not real volume.
    assert m["flow"]["health"] == "UNAVAILABLE"
    reader.identity = "another-demo"
    with pytest.raises(ValueError, match="account changed"):
        monitor.market("MT5:DEMO:EURUSD", 300)  # Cache must not bypass account pin.


def test_real_account_and_unrelated_symbol_blocked_even_with_cached_history():
    reader = Reader()
    monitor = DemoMarketReader(reader)
    monitor.market("MT5:DEMO:BTCUSD", 300)
    reader.demo = False
    with pytest.raises(ValueError, match="DEMO"):
        monitor.market("MT5:DEMO:BTCUSD", 300)
    with pytest.raises(ValueError, match="Unsupported"):
        monitor.market("MT5:DEMO:BTC_ETF", 300)


def test_explicit_reconnect_epoch_discards_trade_window_and_latches_warming_up():
    state = DashboardState(clock=lambda: AT)
    instrument = "BINANCE:SPOT:BTCUSDT"
    key = f"binance.spot:{instrument}:trade"
    state.hub.gate.register(key)
    state.hub.gate.streams[key].gap = True
    state.events[instrument] = [1]
    state.quotes[instrument] = object()
    state.flow[(instrument, 60)] = object()
    state.ofi[("binance.spot", instrument, 1)] = D(42)
    assert start_public_epoch(state, "BTCUSDT", (key,)) == 1
    assert not state.hub.gate.streams[key].gap
    assert state.hub.gate.snapshot(AT)[key] == "warming_up"
    assert not state.flow and not state.ofi and not state.events and not state.quotes
    assert state.timeline[0]["kind"] == "SOURCE_WINDOW_RESET"
    assert start_public_epoch(state, "BTCUSDT", (key,)) == 2


def test_private_reports_export_missing_evidence_and_never_invent_pnl():
    state = DashboardState(clock=lambda: AT)
    report = daily_report(state)
    assert report["daily_pnl"] is None and report["positions"] is None
    assert report["execution_authorized"] is False and report["account"] is None
    assert "UNAVAILABLE" in markdown(report)
    with TestClient(create_app(state, token="v" * 48)) as client:
        assert client.get("/api/research/report").status_code == 401
        client.post(
            "/auth/session", headers={"origin": "http://127.0.0.1:8787"}, json={"token": "v" * 48}
        )
        response = client.get("/api/research/report?format=md")
        assert (
            response.status_code == 200 and "attachment" in response.headers["content-disposition"]
        )
        assert client.get("/api/research/report").json()["trades_placed_by_collector"] == 0
        assert client.post("/api/research/report").status_code == 405
        assert client.get("/api/research/report?format=exe").status_code == 400


def test_native_chart_api_remains_explicitly_noncanonical_and_read_only():
    monitor = DemoMarketReader(Reader())
    state = DashboardState()

    def unrelated_market(*args):
        raise AssertionError("Native chart must not recompute Binance analysis")

    state.market = unrelated_market
    with TestClient(create_app(state, token="v" * 48, monitor=monitor)) as client:
        client.post(
            "/auth/session", headers={"origin": "http://127.0.0.1:8787"}, json={"token": "v" * 48}
        )
        value = client.get("/api/dashboard/summary?instrument=MT5:DEMO:BTCUSD&seconds=300").json()
        assert len(value["instruments"]) == 4
        assert value["agents"]["reason"] == "BROKER_HISTORY_IS_NOT_APPROVED_ANALYSIS"
        assert value["execution"]["state"] == "DISARMED" and value["live_enabled"] is False
        assert (
            client.get("/api/dashboard/summary?instrument=MT5:DEMO:BTC_ETF&seconds=300").status_code
            == 400
        )


def test_daily_capture_is_immutable_and_week_checks_digest(monkeypatch, tmp_path):
    import io
    import json

    import vision.apps.dashboard.research as research

    report = daily_report(DashboardState(clock=lambda: AT))

    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(report if isinstance(request, str) else {}).encode())

    monkeypatch.setattr(research, "build_opener", lambda *args: Opener())
    token = tmp_path / "viewer.token"
    token.write_text("v" * 48)
    directory = tmp_path / "research"
    target = research.capture(token, directory, start_date="2026-10-05")
    summary = json.loads((directory / "weekly-summary.json").read_text())
    assert summary["days_collected"] == 1 and summary["status"] == "IN_PROGRESS"
    assert summary["performance_conclusion"] == "INSUFFICIENT_VERIFIED_TRADE_EVIDENCE"
    with pytest.raises(FileExistsError):
        research.capture(token, directory)
    value = json.loads(target.read_text())
    value["broker_state"] = "HEALTHY"
    target.write_text(json.dumps(value))
    report["captured_at"] = "2026-10-05T12:01:00+00:00"
    with pytest.raises(ValueError, match="digest mismatch"):
        research.capture(token, directory)


def test_daily_collector_refuses_real_and_changed_demo_account(monkeypatch, tmp_path):
    import io
    import json

    import vision.apps.dashboard.research as research

    report = daily_report(DashboardState(clock=lambda: AT))
    report["account"] = {"demo": False, "identity": "real"}

    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(report if isinstance(request, str) else {}).encode())

    monkeypatch.setattr(research, "build_opener", lambda *args: Opener())
    token = tmp_path / "viewer.token"
    token.write_text("v" * 48)
    with pytest.raises(ValueError, match="Real account"):
        research.capture(token, tmp_path / "research")
    report["account"] = {"demo": True, "identity": "demo-A"}
    research.capture(token, tmp_path / "research")
    report["account"]["identity"] = "demo-B"
    with pytest.raises(ValueError, match="account changed"):
        research.capture(token, tmp_path / "research")


def test_public_history_finishes_before_live_socket_opens(monkeypatch):
    """Slow REST backfills must not queue stale live trades during startup."""
    import asyncio

    from vision.apps.dashboard import runtime

    loaded = []

    class REST:
        def bars(self, sub, limit):
            assert limit == 512
            loaded.append(sub.interval)
            return []

    class Socket:
        async def __aenter__(self):
            assert loaded == ["1m", "5m", "15m", "1h", "4h", "1d"]
            raise asyncio.CancelledError

        async def __aexit__(self, *args):
            pass

    state = NS(hub=NS(registry=None, register=lambda *a, **kw: None), connections={})
    monkeypatch.setattr(runtime, "BinanceREST", REST)
    monkeypatch.setattr(runtime, "refresh_public_spec", lambda *a: NS(spec=None))
    monkeypatch.setattr(runtime, "connect", lambda *a, **kw: Socket())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.public_feed(state, "BTCUSDT", ("1m", "5m", "15m", "1h", "4h", "1d")))
