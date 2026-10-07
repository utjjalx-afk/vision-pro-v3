"""Native protocol checked only against a synthetic SDK; no terminal attachment."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_broker_sizing import NOW, native_fixture

from vision.execution.demo.native import DemoMT5Transport, DispatchBlocked, comment


def fixture():
    reader, native, info, _, account = native_fixture()
    terminal = native.terminal_info()
    native.terminal_info = lambda: terminal
    account.trade_allowed = account.trade_expert = terminal.trade_allowed = True
    account.margin_mode = 2
    info.trade_exemode, info.filling_mode = 2, 1
    snapshot = reader.snapshot()
    transport = DemoMT5Transport(reader, account_identity=snapshot.account.identity, magic=120012)
    calls = []
    native.order_send = lambda req: (
        calls.append(req)
        or SimpleNamespace(
            retcode=10009, request_id=1, order=2, deal=3, volume=req["volume"], price=2000.2
        )
    )
    request = {
        "symbol": info.name,
        "side": "LONG",
        "volume": Decimal("0.10"),
        "stop": Decimal(1990),
        "tp": None,
        "deviation": 10,
        "comment": comment("synthetic-client"),
    }
    return transport, native, info, account, terminal, snapshot, calls, request


def test_native_narrow_market_request_and_redacted_ids():
    t, _, _, _, _, s, calls, req = fixture()
    result = t.send(req, expected_snapshot=s)
    assert result.category == "ACK" and result.order_id != "2"
    assert calls[0]["action"] == 1 and calls[0]["type_filling"] == 0
    assert calls[0]["sl"] == 1990 and calls[0]["magic"] == 120012
    assert "price" not in calls[0]  # MT5 Market Execution has no requested price field.


@pytest.mark.parametrize(
    "field,value",
    [("trade_mode", 2), ("margin_mode", 0), ("trade_allowed", False), ("trade_expert", False)],
)
def test_native_account_hard_blocks(field, value):
    t, _, _, a, _, s, calls, req = fixture()
    setattr(a, field, value)
    with pytest.raises(RuntimeError):
        t.send(req, expected_snapshot=s)
    assert not calls


@pytest.mark.parametrize("field,value", [("trade_exemode", 0), ("filling_mode", 2)])
def test_unsupported_execution_semantics(field, value):
    t, _, info, _, _, s, calls, req = fixture()
    setattr(info, field, value)
    with pytest.raises(DispatchBlocked):
        t.send(req, expected_snapshot=s)
    assert not calls


@pytest.mark.parametrize(
    "code,category",
    [
        (10009, "ACK"),
        (10008, "ACK"),
        (10010, "PARTIAL"),
        (10012, "UNKNOWN"),
        (10031, "UNKNOWN"),
        (99999, "UNKNOWN"),
        (10016, "REJECTED"),
        (10019, "REJECTED"),
    ],
)
def test_result_codes_never_imply_resend(code, category):
    t, n, _, _, _, s, calls, req = fixture()
    n.order_send = lambda r: (
        calls.append(r)
        or SimpleNamespace(retcode=code, request_id=1, order=2, deal=3, volume=0.1, price=2000.2)
    )
    assert t.send(req, expected_snapshot=s).category == category
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"stop": Decimal(0)},
        {"volume": Decimal("0.015")},
        {"type": "LIMIT"},
        {"side": "WAIT"},
        {"comment": "unowned"},
        {"symbol": "BTC-stock"},
    ],
)
def test_native_entry_contract_cannot_bypass_gateway(change):
    t, _, _, _, _, s, calls, req = fixture()
    with pytest.raises((DispatchBlocked, ValueError)):
        t.send({**req, **change}, expected_snapshot=s)
    assert not calls


def test_native_history_unavailable_is_not_empty_exposure():
    t, n, _, _, _, _, _, _ = fixture()
    n.history_orders_get = lambda *args: None
    n.history_deals_get = lambda *args: ()
    with pytest.raises(RuntimeError, match="BROKER_HISTORY_UNAVAILABLE"):
        t.facts(since=NOW)
