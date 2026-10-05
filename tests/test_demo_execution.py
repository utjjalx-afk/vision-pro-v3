"""Synthetic-only gateway tests. These never import or connect to a real MT5 terminal."""

import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import RLock

import pytest
from test_broker_sizing import NOW, Calculator, inputs

from vision.analysis.contracts import wire
from vision.analysis.synthesizer.contracts import Direction
from vision.broker.sizer import size
from vision.execution.demo.api import dispatch
from vision.execution.demo.gateway import DemoGateway
from vision.execution.demo.journal import DemoJournal, replay
from vision.execution.demo.models import (
    BrokerFacts,
    BrokerResult,
    DemoPolicy,
    GatewayState,
    RiskContext,
)
from vision.execution.demo.native import DispatchBlocked
from vision.execution.demo.parity import parity
from vision.intents.models import TradeIntent
from vision.journal.replay import replay_entries
from vision.journal.repository import SQLiteRepository, export
from vision.risk.governor import HardRiskGovernor, RiskLimits

D = Decimal


class Transport:
    def __init__(self):
        self.s, self.r, self.p = inputs()
        self.lock, self.calls, self.positions, self.deals, self.orders = RLock(), [], [], [], []
        self.category, self.fault, self.ready, self.raise_send = "ACK", None, True, False
        self.sizing_failure, self.move_at_send = None, False

    def snapshot(self):
        return self.s

    def readiness(self, _):
        if not self.ready:
            raise DispatchBlocked("BROKER_TRADE_PERMISSION_REQUIRED")

    def profit(self, *args):
        return Calculator().profit(*args)

    def margin(self, *args):
        return Calculator().margin(*args)

    def audit_size(self, request, policy):
        snapshot = self.s
        if self.sizing_failure:
            snapshot = self.sizing_failure(snapshot)
        receipt = size(snapshot, request, policy, Calculator(), now=NOW)
        return {
            "snapshot": wire(snapshot),
            "after_snapshot": wire(snapshot),
            "request": wire(request),
            "policy": wire(policy),
            "now": wire(NOW),
            "calculations": wire(receipt.calculations),
            "result": receipt.to_dict(),
        }

    def send(self, request, *, expected_snapshot):
        if self.move_at_send:
            raise DispatchBlocked("FINAL_BROKER_STATE_CHANGED")
        self.calls.append(request)
        if self.raise_send:
            raise TimeoutError("synthetic timeout")
        if self.category == "ACK":
            if request.get("position_id"):
                self.positions = []
                self.deals.append(
                    {
                        **self.deals[0],
                        "deal_id": "synthetic-exit",
                        "order_id": "synthetic-close",
                        "entry": 1,
                        "side": request["side"],
                        "profit": "5",
                        "price": "2001",
                    }
                )
            else:
                price = str(
                    self.s.quotes[0].ask if request["side"] == "LONG" else self.s.quotes[0].bid
                )
                common = {
                    "symbol": request["symbol"],
                    "magic": 120012,
                    "comment": request["comment"],
                    "side": request["side"],
                }
                self.orders = [{**common, "order_id": "synthetic-order", "active": False}]
                self.deals = [
                    {
                        **common,
                        "deal_id": "synthetic-deal",
                        "order_id": "synthetic-order",
                        "position_id": "synthetic-position",
                        "entry": 0,
                        "volume": str(request["volume"]),
                        "price": price,
                        "profit": "0",
                        "commission": "-0.1",
                        "swap": "0",
                        "fee": "0",
                    }
                ]
                self.positions = [
                    {
                        **common,
                        "position_id": "synthetic-position",
                        "volume": str(request["volume"]),
                        "price": price,
                        "stop": str(request["stop"]),
                        "tp": str(request["tp"] or 0),
                    }
                ]
                if self.fault:
                    self.fault(self)
        return BrokerResult(
            self.category,
            "synthetic-request",
            "synthetic-order",
            "synthetic-deal",
            request["volume"],
            self.s.quotes[0].ask,
        )

    def facts(self, *, since):
        return BrokerFacts(
            self.s.account.identity,
            self.s.account.currency,
            tuple(self.orders),
            tuple(self.deals),
            tuple(self.positions),
            NOW,
        )


def setup(tmp_path, *, enabled=True, accepted=True):
    t = Transport()
    policy = DemoPolicy(
        t.s.account.identity,
        "synthetic-phase11-evidence",
        accepted,
        D(1),
        10,
        timedelta(seconds=5),
        timedelta(seconds=30),
        120012,
    )
    repo = SQLiteRepository(tmp_path / "demo.sqlite")
    j = DemoJournal(repo, experiment_id="synthetic-demo")
    clock = [NOW]
    g = DemoGateway(
        t, j, policy, HardRiskGovernor(RiskLimits()), enabled=enabled, clock=lambda: clock[0]
    )
    i = TradeIntent(
        t.r.intent_id,
        "synthetic-synthesis",
        t.r.canonical,
        "synthetic-instrument",
        Direction.LONG,
        D("0.5"),
        NOW,
        NOW,
        ("technical", "flow"),
        ("synthetic-evidence",),
        ("synthetic-reliability",),
        t.r.expires_at,
        ("risk_veto",),
        "synthetic-input",
        "synthetic-source",
        "synthetic-spec",
    )
    command = dict(
        client_order_id="synthetic-client",
        intent=i,
        request=t.r,
        sizing_policy=t.p,
        sizing_audit=t.audit_size(t.r, t.p),
        sized_at=NOW,
        risk_context=RiskContext(t.s.account.identity, "EUR", D(10000), D(10000), NOW),
    )
    return g, t, repo, command, clock


def arm(g):
    g.arm(account_identity=g.policy.account_identity, operator_confirmation="ARMED_DEMO")


def test_complete_lifecycle_duplicate_and_journal_replay(tmp_path):
    g, t, repo, c, _ = setup(tmp_path)
    assert g.state == GatewayState.DISARMED
    arm(g)
    h = g.execute(**c)
    assert h["state"] == "POSITION_OPEN" and len(t.calls) == 1
    assert g.execute(**c) == h and len(t.calls) == 1
    assert replay(repo.entries()) == g.journal.heads()
    replay_entries(repo.entries())
    closed = g.close(c["client_order_id"], operator_confirmation="CLOSE_DEMO_POSITION")
    assert closed["state"] == "CLOSED" and closed["details"]["realized_pnl"] == "4.8"
    assert not closed["details"]["reliability_update"]
    report = parity(closed)
    assert report["slippage_price"] == "0.0"
    assert report["paper_cfd_status"] == "UNAVAILABLE_SPOT_ONLY_PAPER_BROKER"
    assert report["paper_cfd_margin"] is None
    replay_entries(repo.entries())
    assert export(repo)["head"]


@pytest.mark.parametrize("enabled,accepted", [(False, True), (True, False), (False, False)])
def test_feature_and_acceptance_gates(tmp_path, enabled, accepted):
    g, t, _, c, _ = setup(tmp_path, enabled=enabled, accepted=accepted)
    with pytest.raises(ValueError):
        arm(g)
    with pytest.raises(ValueError):
        g.execute(**c)
    assert not t.calls


@pytest.mark.parametrize("change", ["live", "disconnect", "other-account"])
def test_arm_account_gate(tmp_path, change):
    g, t, _, _, _ = setup(tmp_path)
    changes = {
        "live": {"demo": False},
        "disconnect": {"connected": False},
        "other-account": {"identity": "other-account"},
    }[change]
    if change == "other-account":
        t.s = replace(
            t.s,
            specs=tuple(replace(s, account_identity="other-account") for s in t.s.specs),
            account=replace(t.s.account, **changes),
        )
    else:
        t.s = replace(t.s, account=replace(t.s.account, **changes))
    with pytest.raises(ValueError):
        arm(g)


def test_arming_expiry_and_restart(tmp_path):
    g, t, repo, c, clock = setup(tmp_path)
    arm(g)
    clock[0] += timedelta(seconds=30)
    with pytest.raises(ValueError):
        g.execute(**c)
    assert g.state == GatewayState.DISARMED and not t.calls
    restarted = DemoGateway(t, g.journal, g.policy, g.governor, enabled=True, clock=lambda: NOW)
    assert restarted.state == GatewayState.DISARMED
    with pytest.raises(ValueError):
        restarted.execute(**c)
    repo.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda t: setattr(t, "s", replace(t.s, quotes=(replace(t.s.quotes[0], ask=D("2000.3")),))),
        lambda t: setattr(t, "s", replace(t.s, account=replace(t.s.account, equity=D(9999)))),
        lambda t: setattr(t, "s", replace(t.s, account=replace(t.s.account, free_margin=D(1)))),
        lambda t: setattr(
            t, "s", replace(t.s, specs=(replace(t.s.specs[0], volume_step=D("0.02")),))
        ),
        lambda t: setattr(t, "s", replace(t.s, pending_orders=1)),
        lambda t: setattr(t, "ready", False),
        lambda t: setattr(t, "move_at_send", True),
    ],
)
def test_fresh_recheck_never_sends_changed_state(tmp_path, mutation):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    mutation(t)
    assert g.execute(**c)["state"] == "REJECTED"
    assert not t.calls


@pytest.mark.parametrize("category", ["UNKNOWN", "PARTIAL", "REJECTED"])
def test_uncertain_partial_rejected_never_resend(tmp_path, category):
    g, t, repo, c, _ = setup(tmp_path)
    arm(g)
    t.category = category
    h = g.execute(**c)
    assert h["state"] in {"UNKNOWN", "RECONCILIATION_REQUIRED", "REJECTED"}
    assert g.execute(**c) == h and len(t.calls) == 1
    restart = DemoGateway(t, g.journal, g.policy, g.governor, enabled=True, clock=lambda: NOW)
    assert restart.state == "DISARMED"
    assert restart.execute(**c) == h and len(t.calls) == 1
    if category != "REJECTED":
        with pytest.raises(ValueError):
            arm(restart)
    replay_entries(repo.entries())


def test_timeout_persisted_before_call(tmp_path):
    g, t, repo, c, _ = setup(tmp_path)
    arm(g)
    original = t.send

    def uncertain(request, *, expected_snapshot):
        assert list(replay(repo.entries()).values())[0]["state"] == "SUBMITTING"
        t.raise_send = True
        return original(request, expected_snapshot=expected_snapshot)

    t.send = uncertain
    assert g.execute(**c)["state"] == "UNKNOWN"
    assert len(t.calls) == 1


@pytest.mark.parametrize(
    "fault,expected",
    [
        (lambda t: t.positions[0].update(stop="0"), "CRITICAL_PROTECTION_FAULT"),
        (lambda t: t.positions[0].update(stop="1989"), "RECONCILIATION_REQUIRED"),
        (lambda t: t.positions[0].update(volume="0.09"), "RECONCILIATION_REQUIRED"),
        (lambda t: t.positions[0].update(side="SHORT"), "RECONCILIATION_REQUIRED"),
        (lambda t: t.positions[0].update(tp="2100"), "RECONCILIATION_REQUIRED"),
        (lambda t: t.positions.append(dict(t.positions[0])), "RECONCILIATION_REQUIRED"),
        (lambda t: t.deals[0].update(volume="0.09"), "RECONCILIATION_REQUIRED"),
        (lambda t: t.deals.clear(), "RECONCILIATION_REQUIRED"),
    ],
)
def test_fill_and_protection_faults(tmp_path, fault, expected):
    g, t, repo, c, _ = setup(tmp_path)
    arm(g)
    t.fault = fault
    assert g.execute(**c)["state"] == expected
    assert g.state in {GatewayState.HALTED, GatewayState.RECONCILIATION_REQUIRED}
    replay_entries(repo.entries())
    # Monitoring continues through halt/disarm; no native submission is invoked.
    g.halt()
    g.monitor(c["client_order_id"])
    assert len(t.calls) == 1


def test_duplicate_identity_and_intent_collision(tmp_path):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    g.execute(**c)
    with pytest.raises(ValueError):
        g.execute(**{**c, "tp": D(2100)})
    with pytest.raises(ValueError):
        g.execute(**{**c, "client_order_id": "second-client"})
    assert len(t.calls) == 1


@pytest.mark.parametrize("delta", [-1, 6])
def test_receipt_timestamp_rejection(tmp_path, delta):
    g, t, _, c, clock = setup(tmp_path)
    arm(g)
    clock[0] += timedelta(seconds=delta)
    with pytest.raises(ValueError):
        g.execute(**c)
    assert not t.calls


def test_future_native_tick_block_is_not_bypassed(tmp_path):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    t.sizing_failure = lambda s: replace(
        s, quotes=(replace(s.quotes[0], source_at=NOW + timedelta(hours=3)),)
    )
    h = g.execute(**c)
    assert "STALE_OR_FUTURE_BROKER_QUOTE" in h["details"]["reason"]
    assert not t.calls


@pytest.mark.parametrize("field,value", [("daily_baseline", D(11000)), ("high_water", D(12000))])
def test_hard_governor_veto(tmp_path, field, value):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    c["risk_context"] = replace(c["risk_context"], **{field: value})
    assert g.execute(**c)["state"] == "REJECTED" and g.state == GatewayState.RISK_LOCKED
    assert not t.calls


@pytest.mark.parametrize("tp", [D(2000), D("2100.001")])
def test_invalid_tp_no_rounding(tmp_path, tp):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    assert g.execute(**{**c, "tp": tp})["state"] == "REJECTED"
    assert not t.calls


def test_api_exact_envelope_and_typed_intent(tmp_path):
    g, t, _, c, _ = setup(tmp_path)
    dispatch(
        g,
        "/v1/demo/arm",
        {"account_identity": g.policy.account_identity, "operator_confirmation": "ARMED_DEMO"},
    )
    payload = {k: wire(v) for k, v in {**c, "tp": None}.items()}
    assert dispatch(g, "/v1/demo/execute", payload)["order"]["state"] == "POSITION_OPEN"
    with pytest.raises(ValueError):
        dispatch(g, "/v1/demo/execute", {**payload, "raw_order": {}})
    altered = copy.deepcopy(payload)
    altered["intent"]["direction"] = "SHORT"
    altered["client_order_id"] = "different"
    with pytest.raises(ValueError):
        dispatch(g, "/v1/demo/execute", altered)
    assert len(t.calls) == 1


def test_slippage_fill_risk_halts_without_auto_close(tmp_path):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)

    def slipped(t):
        t.deals[0]["price"] = t.positions[0]["price"] = "2002"

    t.fault = slipped
    h = g.execute(**c)
    assert h["state"] == "RECONCILIATION_REQUIRED"
    assert h["details"]["incident"] == "ACTUAL_FILL_RISK_CAP"
    assert g.state == "RISK_LOCKED" and len(t.calls) == 1


def test_authenticated_demo_routes_keep_default_reader_calculation_only(tmp_path):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from vision.broker.server import make_server

    g, t, _, _, _ = setup(tmp_path)
    token = "synthetic-bearer-not-a-credential-0000"
    server = make_server(t, token, port=0, demo_gateway=g)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/demo/status"
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(url)
        assert error.value.code == 401
        with urlopen(Request(url, headers={"Authorization": "Bearer " + token})) as response:
            assert b"DISARMED" in response.read()
        with pytest.raises(HTTPError) as error:
            urlopen(
                Request(
                    url.replace("status", "execute"),
                    data=b'{"raw_order":{}}',
                    headers={"Authorization": "Bearer " + token},
                )
            )
        assert error.value.code == 400 and not t.calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "canonical,symbol",
    [
        ("forex:EUR/USD", "EURUSD"),
        ("metals:XAU/USD", "XAUUSD+"),
        ("crypto:BTC/USD", "BTCUSD"),
        ("metals:XAG/USD", "XAGUSD"),
    ],
)
@pytest.mark.parametrize("side,stop", [("LONG", D(1990)), ("SHORT", D(2010))])
def test_four_asset_both_direction_lifecycle_synthetic(tmp_path, canonical, symbol, side, stop):
    g, t, repo, c, _ = setup(tmp_path)
    spec = replace(t.s.specs[0], symbol=symbol, canonical=canonical)
    t.s = replace(t.s, specs=(spec,), quotes=(replace(t.s.quotes[0], symbol=symbol),))
    request = replace(
        t.r,
        canonical=canonical,
        broker_symbol=symbol,
        side=side,
        stop=stop,
        expected_spec_revision=spec.revision,
    )
    c.update(
        request=request,
        intent=replace(c["intent"], symbol=canonical, direction=Direction(side)),
        sizing_audit=t.audit_size(request, t.p),
    )
    arm(g)
    assert g.execute(**c)["state"] == "POSITION_OPEN"
    assert (
        g.close(c["client_order_id"], operator_confirmation="CLOSE_DEMO_POSITION")["state"]
        == "CLOSED"
    )
    assert len(t.calls) == 2
    replay_entries(repo.entries())


def test_crash_after_send_reconciles_without_resubmission(tmp_path):
    g, t, repo, c, _ = setup(tmp_path)
    arm(g)
    original = t.send

    def crash(request, *, expected_snapshot):
        original(request, expected_snapshot=expected_snapshot)
        raise SystemExit("synthetic process crash")

    t.send = crash
    with pytest.raises(SystemExit):
        g.execute(**c)
    assert g.journal.heads()[c["client_order_id"]]["state"] == "SUBMITTING"
    restart = DemoGateway(t, g.journal, g.policy, g.governor, enabled=True, clock=lambda: NOW)
    assert restart.state == "DISARMED"
    with pytest.raises(ValueError):
        arm(restart)
    assert restart.monitor(c["client_order_id"])["state"] == "POSITION_OPEN"
    assert len(t.calls) == 1
    replay_entries(repo.entries())


def test_broker_triggered_sl_exit_links_position_not_comment(tmp_path):
    g, t, repo, c, _ = setup(tmp_path)
    arm(g)
    g.execute(**c)
    t.positions = []
    t.deals.append(
        {
            **t.deals[0],
            "deal_id": "sl-exit",
            "order_id": "sl-order",
            "comment": "broker-stop-trigger",
            "entry": 1,
            "side": "SHORT",
            "profit": "-98",
            "price": "1990",
        }
    )
    g.halt()
    assert g.monitor(c["client_order_id"])["state"] == "CLOSED"
    assert len(t.calls) == 1
    replay_entries(repo.entries())


def test_default_api_has_no_demo_routes(tmp_path):
    from threading import Thread
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from vision.broker.server import make_server

    _, t, _, _, _ = setup(tmp_path)
    token = "synthetic-bearer-not-a-credential-0000"
    server = make_server(t, token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(
                Request(
                    f"http://127.0.0.1:{server.server_port}/v1/demo/arm",
                    data=b"{}",
                    headers={"Authorization": "Bearer " + token},
                )
            )
        assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_disarm_does_not_reset_kill_switch(tmp_path):
    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    g.halt()
    g.disarm()
    assert g.state == "HALTED"
    with pytest.raises(ValueError):
        arm(g)
    with pytest.raises(ValueError):
        g.execute(**c)
    assert not t.calls


def test_clock_rollback_invalidates_arm(tmp_path):
    g, t, _, c, clock = setup(tmp_path)
    arm(g)
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(ValueError):
        g.execute(**c)
    assert g.state == "DISARMED" and not t.calls


def test_server_readonly_monitoring_continues_after_halt(tmp_path):
    from vision.broker.server import make_server

    g, t, _, c, _ = setup(tmp_path)
    arm(g)
    g.execute(**c)
    t.positions[0]["stop"] = "0"
    g.halt()
    server = make_server(t, "synthetic-bearer-not-a-credential-0000", port=0, demo_gateway=g)
    try:
        server.service_actions()
        assert g.journal.heads()[c["client_order_id"]]["state"] == "CRITICAL_PROTECTION_FAULT"
        assert len(t.calls) == 1
    finally:
        server.server_close()
