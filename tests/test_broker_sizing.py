"""Broker-native sizing, strict offline replay and authenticated local API tests."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from vision.analysis.contracts import wire
from vision.broker.client import BrokerBridgeClient
from vision.broker.codec import dto, replay
from vision.broker.models import (
    BrokerAccount,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
    BrokerSpec,
    SizeRequest,
    SizingPolicy,
)
from vision.broker.mt5_bridge import BridgeUnavailable, MT5Reader
from vision.broker.server import make_server
from vision.broker.sizer import size


def test_exact_plus_suffix_survives_native_calls_and_replay():
    reader, native, info, _, _ = native_fixture()
    info.name = "XAUUSD+"
    reader.mapping = {"metals:XAU/USD": "XAUUSD+"}
    _, request, policy = inputs()
    snapshot = reader.snapshot()
    request = replace(
        request,
        broker_symbol="XAUUSD+",
        expected_account_identity=snapshot.account.identity,
        expected_spec_revision=snapshot.specs[0].revision,
    )
    calls = []
    native.order_calc_profit = lambda action, name, volume, entry, stop: (
        calls.append(name) or (stop - entry) * volume * 97 * (1 if action == 0 else -1)
    )
    native.order_calc_margin = lambda action, name, volume, entry: (
        calls.append(name) or volume * 200
    )
    audit = reader.audit_size(request, policy)
    assert replay(audit).status == "BROKER_SIZE_APPROVED"
    assert calls == ["XAUUSD+"] * 3


@pytest.mark.parametrize("name", ["XAUUSD+ ", "XAUUSD/", "XAUUSD;send", "x" * 65])
def test_plus_suffix_fix_preserves_symbol_bounds(name):
    with pytest.raises(ValueError):
        MT5Reader(None, {"metals:XAU/USD": name}, b"x" * 32)


NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
D = Decimal


def inputs():
    account = BrokerAccount(
        "synthetic-account-digest", "EUR", D(10000), D(10000), D(8000), True, True, NOW
    )
    spec = BrokerSpec(
        "XAUUSD.a",
        "metals:XAU/USD",
        D("0.01"),
        D(100),
        D("0.01"),
        D(0),
        D("0.01"),
        D("0.01"),
        D(1),
        D(1),
        D(100),
        10,
        0,
        4,
        "USD",
        "USD",
        NOW,
        account.identity,
    )
    quote = BrokerQuote(spec.symbol, D(2000), D("2000.2"), NOW, NOW)
    snapshot = BrokerSnapshot(account, (spec,), (quote,), (), 0, NOW, "phase11-v1")
    request = SizeRequest(
        "synthetic-intent",
        spec.canonical,
        spec.symbol,
        "LONG",
        D(1990),
        D(100),
        NOW,
        NOW + timedelta(seconds=30),
        account.identity,
        spec.revision,
        True,
        "synthetic-eligibility",
    )
    policy = SizingPolicy(
        D("0.01"),
        D("0.05"),
        timedelta(seconds=5),
        timedelta(seconds=2),
        D(100),
        D(1),
        "BLOCK",
        True,
    )
    return snapshot, request, policy


class Calculator:
    """Deliberately differs from the declared contract size: must trust native result."""

    def profit(self, side, symbol, volume, entry, stop):
        return (stop - entry) * volume * D(97) * (1 if side == "LONG" else -1)

    def margin(self, side, symbol, volume, entry):
        return volume * D(200)


def run(snapshot=None, request=None, policy=None, calc=None):
    s, r, p = inputs()
    return size(snapshot or s, request or r, policy or p, calc or Calculator(), now=NOW)


def test_floor_native_loss_actual_recalculation_and_account_currency():
    result = run()
    assert result.status == "BROKER_SIZE_APPROVED"
    assert result.volume == D("0.10")
    assert result.one_lot_loss == D("989.4")
    assert result.actual_risk <= 100
    assert result.margin == result.volume * 200
    assert result.account_currency == "EUR" and not result.execution_authorized
    assert [v.kind for v in result.calculations] == ["profit", "profit", "margin"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"stop": D(2001)}, "INVALID_SL_SIDE"),
        ({"stop": D("1999.95")}, "STOP_TOO_CLOSE"),
        ({"stop": D("1990.001")}, "STOP_OFF_TICK_GRID"),
        ({"risk_budget": D(5)}, "BELOW_MINIMUM_VOLUME"),
        ({"risk_budget": D(101)}, "PER_TRADE_RISK_CAP"),
        ({"strategy_eligible": False}, "STRATEGY_INELIGIBLE"),
        ({"expected_account_identity": "other"}, "ACCOUNT_IDENTITY_CHANGED"),
        ({"expected_spec_revision": "other"}, "SPEC_REVISION_CHANGED"),
        ({"canonical": "forex:EUR/USD"}, "SYMBOL_MAPPING_OR_SPEC_MISSING"),
        ({"requested_at": NOW + timedelta(seconds=1)}, "INTENT_EXPIRED_OR_FUTURE"),
    ],
)
def test_request_blocks_without_silent_adjustment(changes, reason):
    s, r, _ = inputs()
    result = run(request=replace(r, **changes))
    assert result.status == "BLOCKED" and reason in result.reasons
    assert s.snapshot_id == inputs()[0].snapshot_id


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"demo": False}, "DEMO_ACCOUNT_CONNECTED_REQUIRED"),
        ({"connected": False}, "DEMO_ACCOUNT_CONNECTED_REQUIRED"),
        ({"equity": D(0)}, "ACCOUNT_ECONOMICS_INVALID"),
        ({"free_margin": D(1)}, "INSUFFICIENT_FREE_MARGIN"),
    ],
)
def test_account_health_and_margin_veto(changes, reason):
    s, _, _ = inputs()
    assert reason in run(snapshot=replace(s, account=replace(s.account, **changes))).reasons


def test_short_uses_bid_and_ask_stop_trigger():
    _, r, _ = inputs()
    result = run(request=replace(r, side="SHORT", stop=D(2010)))
    assert result.status == "BROKER_SIZE_APPROVED"
    assert result.calculations[0].entry == 2000
    assert "INVALID_SL_SIDE" in run(request=replace(r, side="SHORT", stop=D("2000.1"))).reasons


def test_unknown_existing_risk_and_aggregate_cap():
    s, _, _ = inputs()
    position = BrokerPosition("existing", s.specs[0].symbol, "LONG", D(1), D(1800), D(0))
    assert (
        "UNKNOWN_OR_NO_SL_EXISTING_RISK" in run(snapshot=replace(s, positions=(position,))).reasons
    )
    position = replace(position, stop=D(1990))
    result = run(snapshot=replace(s, positions=(position,)))
    assert result.open_risk == 971  # Current equity exposure, not losing entry exposure.
    assert "AGGREGATE_OPEN_RISK_CAP" in result.reasons
    assert (
        "EXISTING_STOP_CROSSED"
        in run(snapshot=replace(s, positions=(replace(position, stop=D(2001)),))).reasons
    )
    assert "PENDING_ORDER_RISK_UNSUPPORTED" in run(snapshot=replace(s, pending_orders=1)).reasons


def test_failures_non_linear_final_risk_and_margin_unavailable():
    class NoProfit(Calculator):
        def profit(self, *_):
            return None

    assert "ONE_LOT_LOSS_UNAVAILABLE" in run(calc=NoProfit()).reasons

    class Nonlinear(Calculator):
        def profit(self, side, symbol, volume, entry, stop):
            return super().profit(side, symbol, volume, entry, stop) * (1 if volume == 1 else 10)

    assert "FINAL_RISK_CAP" in run(calc=Nonlinear()).reasons

    class NoMargin(Calculator):
        def margin(self, *_):
            return None

    assert "MARGIN_UNAVAILABLE" in run(calc=NoMargin()).reasons


def test_stale_quote_snapshot_skew_and_invalid_volume_grid():
    s, r, _ = inputs()
    assert (
        "STALE_OR_FUTURE_BROKER_QUOTE"
        in run(
            snapshot=replace(
                s, quotes=(replace(s.quotes[0], source_at=NOW - timedelta(seconds=6)),)
            )
        ).reasons
    )
    assert (
        "SNAPSHOT_SKEW"
        in run(
            snapshot=replace(
                s, quotes=(replace(s.quotes[0], observed_at=NOW - timedelta(seconds=3)),)
            )
        ).reasons
    )
    assert (
        "STALE_OR_FUTURE_SNAPSHOT"
        in run(snapshot=replace(s, as_of=NOW - timedelta(seconds=6))).reasons
    )
    spec = replace(s.specs[0], volume_step=D("0.02"))
    assert (
        "UNSUPPORTED_VOLUME_GRID"
        in run(
            snapshot=replace(s, specs=(spec,)),
            request=replace(r, expected_spec_revision=spec.revision),
        ).reasons
    )


def test_native_limits_floor_and_directional_volume_limit():
    s, r, _ = inputs()
    spec = replace(s.specs[0], volume_max=D("0.035"))
    result = run(
        snapshot=replace(s, specs=(spec,)), request=replace(r, expected_spec_revision=spec.revision)
    )
    assert result.volume == D("0.03")
    spec = replace(s.specs[0], volume_limit=D("0.01"))
    position = BrokerPosition("existing", spec.symbol, "LONG", D("0.01"), D(2000), D(1995))
    assert (
        "BELOW_MINIMUM_VOLUME"
        in run(
            snapshot=replace(s, specs=(spec,), positions=(position,)),
            request=replace(r, expected_spec_revision=spec.revision),
        ).reasons
    )


def test_decimal_independence_and_exact_replay_rejects_forged_receipts():
    s, r, p = inputs()
    result = run()
    value = {
        "snapshot": wire(s),
        "request": wire(r),
        "policy": wire(p),
        "now": wire(NOW),
        "calculations": wire(result.calculations),
        "result": result.to_dict(),
    }
    assert replay(json.loads(json.dumps(value))) == result
    with localcontext() as context:
        context.prec = 3
        assert run() == result
    forged = copy.deepcopy(value)
    forged["result"]["volume"] = "1"
    with pytest.raises(ValueError):
        replay(forged)
    forged = copy.deepcopy(value)
    forged["calculations"][0]["account_currency"] = "USD"
    with pytest.raises(ValueError):
        replay(forged)
    with pytest.raises(ValueError):
        dto(SizeRequest, {**wire(r), "order": "send"})


class FakeReader:
    def health(self):
        return {"demo": True, "order_dispatch_available": False}

    def discovery(self):
        return {"metals:XAU/USD": ["XAUUSD.a", "XAUUSDm"]}

    def snapshot(self):
        return inputs()[0]

    def size(self, r, p):
        return size(self.snapshot(), r, p, Calculator(), now=NOW)


def test_authenticated_loopback_api_no_order_endpoint(capsys):
    token = "synthetic-local-token-" + "x" * 32
    server = make_server(FakeReader(), token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        client = BrokerBridgeClient(token, port=port)
        assert client.health()["demo"] and client.snapshot() == inputs()[0]
        assert len(client.symbols()["metals:XAU/USD"]) == 2
        _, r, p = inputs()
        assert client.size(r, p).status == "BROKER_SIZE_APPROVED"
        for path, auth, status in [
            ("health", None, 401),
            ("orders", token, 404),
            ("health", "bad", 401),
        ]:
            req = Request(
                f"http://127.0.0.1:{port}/v1/{path}",
                headers={} if auth is None else {"Authorization": "Bearer " + auth},
            )
            with pytest.raises(HTTPError) as error:
                urlopen(req, timeout=3)
            assert error.value.code == status
        with pytest.raises(RuntimeError):
            BrokerBridgeClient("bad" * 20, port=port).health()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
    assert not capsys.readouterr().err


def test_native_demo_identity_and_suffix_discovery_never_prints_account():
    account = SimpleNamespace(
        login=123,
        server="private-demo",
        trade_mode=0,
        currency="EUR",
        equity=10000.0,
        balance=10000.0,
        margin_free=8000.0,
    )
    native = SimpleNamespace(
        account_info=lambda: account,
        terminal_info=lambda: SimpleNamespace(connected=True),
        symbols_get=lambda: (
            SimpleNamespace(name="XAUUSD.a", currency_base="XAU", currency_profit="USD"),
        ),
        version=lambda: (500, 1, "synthetic"),
    )
    reader = MT5Reader(native, {"metals:XAU/USD": "XAUUSD.a"}, b"x" * 32, clock=lambda: NOW)
    assert reader.discovery()["metals:XAU/USD"] == ["XAUUSD.a"]
    assert "private-demo" not in json.dumps(reader.health())
    account.login = 456
    with pytest.raises(BridgeUnavailable):
        reader.account()
    account.login = 123
    account.trade_mode = 2
    with pytest.raises(BridgeUnavailable):
        reader.account()


def test_explicit_mapping_and_unknown_policy_cannot_be_guessed():
    with pytest.raises(ValueError):
        MT5Reader(None, {"XAUUSD": "XAUUSD.a"}, b"x" * 32)
    _, _, p = inputs()
    with pytest.raises(ValueError):
        replace(p, demo_only=False)
    with pytest.raises(ValueError):
        replace(p, unknown_risk_policy="IGNORE")


def native_fixture():
    s, _, _ = inputs()
    info = SimpleNamespace(
        name="XAUUSD.a",
        visible=True,
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        volume_limit=0.0,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value_profit=1.0,
        trade_tick_value_loss=1.0,
        trade_contract_size=100.0,
        trade_stops_level=10,
        trade_freeze_level=0,
        trade_mode=4,
    )
    account = SimpleNamespace(
        login=123,
        server="synthetic-private",
        trade_mode=0,
        currency="EUR",
        equity=10000.0,
        balance=10000.0,
        margin_free=8000.0,
    )
    tick = SimpleNamespace(bid=2000.0, ask=2000.2, time_msc=int(NOW.timestamp() * 1000))
    native = SimpleNamespace(
        account_info=lambda: account,
        terminal_info=lambda: SimpleNamespace(connected=True),
        positions_get=lambda: (),
        orders_get=lambda: (),
        symbol_info=lambda _: info,
        symbol_info_tick=lambda _: tick,
        symbols_get=lambda: (info,),
        version=lambda: (500, 1, "fixture"),
        order_calc_profit=lambda action, symbol, volume, entry, stop: (
            (stop - entry) * volume * 97 * (1 if action == 0 else -1)
        ),
        order_calc_margin=lambda action, symbol, volume, entry: volume * 200,
    )
    reader = MT5Reader(
        native, {s.specs[0].canonical: s.specs[0].symbol}, b"x" * 32, clock=lambda: NOW
    )
    return reader, native, info, tick, account


def test_native_snapshot_currency_mapping_and_end_state_recheck():
    reader, native, _, tick, _ = native_fixture()
    snapshot = reader.snapshot()
    _, request, policy = inputs()
    request = replace(
        request,
        expected_account_identity=snapshot.account.identity,
        expected_spec_revision=snapshot.specs[0].revision,
    )
    assert reader.size(request, policy).status == "BROKER_SIZE_APPROVED"
    assert replay(reader.audit_size(request, policy)).status == "BROKER_SIZE_APPROVED"

    def changed(*_):
        tick.ask += 0.01
        return 20.0

    native.order_calc_margin = changed
    assert reader.size(request, policy).reasons == ("BROKER_STATE_CHANGED_DURING_SIZING",)
    assert replay(reader.audit_size(request, policy)).reasons == (
        "BROKER_STATE_CHANGED_DURING_SIZING",
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("none_positions", "ACCOUNT_RISK_UNAVAILABLE"),
        ("none_orders", "ACCOUNT_RISK_UNAVAILABLE"),
        ("hidden", "SYMBOL_NOT_VISIBLE_OR_UNAVAILABLE"),
        ("wrong_currency", "BROKER_MAPPING_ECONOMICS_MISMATCH"),
        ("no_tick", "BROKER_QUOTE_UNAVAILABLE"),
    ],
)
def test_native_missing_truth_is_not_guessed(change, reason):
    reader, native, info, _, _ = native_fixture()
    if change == "none_positions":
        native.positions_get = lambda: None
    if change == "none_orders":
        native.orders_get = lambda: None
    if change == "hidden":
        info.visible = False
    if change == "wrong_currency":
        info.currency_profit = "USDT"
    if change == "no_tick":
        native.symbol_info_tick = lambda _: None
    with pytest.raises(BridgeUnavailable) as error:
        reader.snapshot()
    assert str(error.value) == reason


def test_demo_reconciliation_requires_four_assets_and_never_self_accepts():
    from vision.broker.reconcile import reconcile

    reader, _, _, _, _ = native_fixture()
    with pytest.raises(ValueError):
        reconcile(reader, inputs()[2], {})


@pytest.mark.parametrize(
    "body",
    [b'{"request":{},"request":{}}', b'{"order":1}', b"[]", b'{"request":NaN}', b"x" * 100001],
    ids=["duplicates", "unknown", "array", "nonfinite", "oversize"],
)
def test_authenticated_api_rejects_untrusted_bodies_without_dispatch(body):
    token = "synthetic" + "x" * 40
    server = make_server(FakeReader(), token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/size",
            data=body,
            headers={"Authorization": "Bearer " + token},
        )
        with pytest.raises((HTTPError, OSError)) as error:
            urlopen(request, timeout=3)
        if isinstance(error.value, HTTPError):
            assert error.value.code == 400
            assert b"INVALID_SIZING_REQUEST" in error.value.read()
        else:
            assert len(body) > 100000  # Immediate oversized-body rejection can reset TCP.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_public_fixture_receipt_roundtrip_and_forgery_rejection():
    from vision.broker.codec import receipt_from_dict

    result = run()
    assert receipt_from_dict(result.to_dict()) == result
    value = result.to_dict()
    value["execution_authorized"] = True
    with pytest.raises(ValueError):
        receipt_from_dict(value)


def test_four_asset_synthetic_demo_report_never_self_promotes():
    from vision.broker.mt5_bridge import CANONICAL
    from vision.broker.reconcile import reconcile

    reader, _, _, _, _ = native_fixture()
    base = reader.snapshot()
    reader.mapping = dict.fromkeys(CANONICAL, "synthetic")

    def snapshot():
        return replace(
            base,
            specs=tuple(
                replace(base.specs[0], symbol=f"asset{i}", canonical=key)
                for i, key in enumerate(CANONICAL)
            ),
        )

    reader.snapshot = snapshot
    reader.audit_size = lambda request, policy: {
        "result": {"status": "BLOCKED", "execution_authorized": False}
    }
    plans = {key: {"LONG": D(1990), "SHORT": D(2010), "risk_budget": D(100)} for key in CANONICAL}
    report = reconcile(reader, inputs()[2], plans)
    assert len(report["evidence"]) == 8
    assert (
        report["status"] == "AWAITING_OPERATOR_RECONCILIATION" and not report["acceptance_passed"]
    )


@pytest.mark.parametrize(
    "flag",
    [
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ],
)
def test_sizing_replay_guards_before_file_or_terminal(flag, monkeypatch, capsys):
    import sys

    from vision.__main__ import main

    monkeypatch.setenv(flag, "true")
    monkeypatch.setattr(sys, "argv", ["vision", "broker-sizing-replay", "missing.json"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and "must be false" in capsys.readouterr().err


def test_windows_native_import_is_not_required_by_cross_platform_core(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(BridgeUnavailable) as error:
        MT5Reader.connect({"metals:XAU/USD": "XAUUSD.a"}, b"x" * 32, terminal_path="unused")
    assert str(error.value) == "WINDOWS_MT5_REQUIRED"


def test_receipt_binds_policy_and_maximum_position_calculation_boundary():
    s, _, p = inputs()
    base = run()
    changed = run(policy=replace(p, margin_reserve=D(101)))
    assert changed.volume == base.volume and changed.policy_id != base.policy_id
    assert changed.receipt_id != base.receipt_id
    positions = tuple(
        BrokerPosition(f"position-{i}", s.specs[0].symbol, "LONG", D("0.01"), D(2000), D("1999.99"))
        for i in range(256)
    )
    result = run(snapshot=replace(s, positions=positions))
    assert len(result.calculations) == 259 and result.status == "BROKER_SIZE_APPROVED"
