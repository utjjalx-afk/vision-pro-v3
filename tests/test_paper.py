import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from vision.core.contracts import EventType, QuotePayload, TimestampBasis
from vision.core.instruments import (
    CanonicalSymbol,
    InstrumentRecord,
    QuantityUnit,
    SpecProvenance,
    digest,
)
from vision.core.state.portfolio import DataQuality, Side
from vision.execution.paper.broker import PaperBroker
from vision.execution.paper.models import MarketRules, OrderState, PaperCosts, PaperMode, PaperOrder
from vision.execution.paper.rules import rules_from_metadata


@pytest.fixture
def setup(now, binance_instrument, market_event):
    rec = InstrumentRecord(
        binance_instrument,
        CanonicalSymbol(binance_instrument.asset_class, "BTC", "USDT"),
        QuantityUnit.BASE,
        SpecProvenance(
            "binance.spot", "synthetic-fixture", now, digest({"fixture": 1}), "fixture-v1"
        ),
        "linear_quote",
    )
    rules = MarketRules(
        rec.spec.instrument_id,
        rec.revision,
        Decimal("0.001"),
        Decimal("10"),
        Decimal("0.001"),
        Decimal("1"),
        None,
        rec.provenance.metadata_digest,
    )
    broker = PaperBroker(
        "USDT",
        Decimal("10000"),
        now,
        costs=PaperCosts(slippage_bps=Decimal(0), commission_bps=Decimal(0)),
    )
    broker.register(rec, rules)

    def quote(price="100", seq=1, at=now, divergent=False, state="healthy", size="10"):
        p = Decimal(price)
        event = replace(
            market_event,
            event_id=f"q-{seq}",
            event_type=EventType.QUOTE,
            sequence=seq,
            source_ts=at,
            received_ts=at,
            payload=QuotePayload(p, p + Decimal(1), Decimal(size), Decimal(size)),
            timestamp_basis=TimestampBasis.EXCHANGE,
        )
        broker.update_quote(event, DataQuality(at, ((rec.spec.instrument_id, state),), divergent))
        return event

    quote()

    def order(identity="o1", quantity="1", stop="90", at=now, side=Side.LONG):
        return PaperOrder(
            identity,
            rec.spec.instrument_id,
            rec.revision,
            side,
            Decimal(quantity),
            Decimal(stop) if stop else None,
            at,
        )

    return broker, rec, rules, quote, order


def test_market_bid_ask_fill_cash_pnl_close(setup, now):
    broker, rec, rules, quote, order = setup
    result = broker.submit(order())
    assert result.fill.price == 101
    assert result.states == (
        OrderState.NEW,
        OrderState.VALIDATING,
        OrderState.RISK_APPROVED,
        OrderState.FILLED,
    )
    assert (
        result.fill.risk_decision_id == result.decision.risk_decision_id
        and result.fill.order_id == result.order_id
    )
    position = broker.positions[result.fill.position_id]
    assert position.position.input_id == result.fill.fill_id
    snap = broker.snapshot(now)
    assert snap.cash == 9899 and snap.equity == 9999 and snap.unrealized_pnl == -1
    quote("110", 2)
    close = broker.close(result.fill.position_id, "close", now)
    assert close.fill.price == 110
    assert broker.snapshot(now).cash == 10009 and broker.snapshot(now).realized_pnl == 9
    assert broker.snapshot(now).unrealized_pnl == 0


def test_realistic_costs_adverse_ticks_and_fee_reconciliation(setup, now):
    broker, _, _, quote, order = setup
    broker.configure(
        costs=PaperCosts(
            spread_bps=Decimal("2"), slippage_bps=Decimal("3"), commission_bps=Decimal("10")
        )
    )
    fill = broker.submit(order()).fill
    assert fill.price == Decimal("101.05") and fill.commission == Decimal("0.10105")
    close = broker.close(fill.position_id, "close", now).fill
    assert close.price == Decimal("99.96")
    snapshot = broker.snapshot(now)
    assert snapshot.cash == broker.initial_cash + snapshot.realized_pnl
    assert snapshot.commissions == fill.commission + close.commission


def test_ideal_mode_explicitly_optimistic(setup, now):
    broker, _, _, _, order = setup
    broker.configure(costs=PaperCosts(mode=PaperMode.IDEAL))
    result = broker.submit(order())
    assert result.fill.price == Decimal("100.5") and result.fill.commission == 0


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("no_sl", "NO_STOP_LOSS"),
        ("short", "MARGIN_MODEL_UNVERIFIED"),
        ("step", "QUANTITY_RULE"),
        ("min_notional", "NOTIONAL_RULE"),
        ("stop_above", "INVALID_STOP_LOSS"),
        ("stop_tick", "INVALID_STOP_LOSS"),
        ("limit", "ORDER_TYPE_UNSUPPORTED"),
        ("revision", "INSTRUMENT_SPEC_INCOMPLETE"),
        ("size", "INSUFFICIENT_TOP_OF_BOOK"),
        ("cash", "INSUFFICIENT_CASH"),
        ("fx", "FX_CONVERSION_REQUIRED"),
    ],
)
def test_validation_no_fill_or_cash_mutation(setup, now, kind, reason):
    broker, rec, rules, quote, order = setup
    value = order()
    if kind == "no_sl":
        value = replace(value, stop_loss=None)
    if kind == "short":
        value = replace(value, side=Side.SHORT)
    if kind == "step":
        value = replace(value, quantity=Decimal("0.0015"))
    if kind == "min_notional":
        value = replace(value, quantity=Decimal("0.001"))
    if kind == "stop_above":
        value = replace(value, stop_loss=Decimal(110))
    if kind == "stop_tick":
        value = replace(value, stop_loss=Decimal("90.001"))
    if kind == "limit":
        value = replace(value, order_type="LIMIT")
    if kind == "revision":
        value = replace(value, spec_revision="unknown")
    if kind == "size":
        quote(size="0.5", seq=2)
    if kind == "cash":
        value = replace(value, quantity=Decimal("100"))
    if kind == "fx":
        broker = PaperBroker("USD", Decimal("10000"), now)
        broker.register(rec, rules)
        broker.update_quote(quote(seq=2), DataQuality(now, ((rec.spec.instrument_id, "healthy"),)))
    cash = broker.cash
    result = broker.submit(value)
    assert (
        reason in result.decision.reasons
        and result.fill is None
        and result.states[-1] is OrderState.REJECTED
    )
    assert broker.cash == cash and not broker.positions


@pytest.mark.parametrize(
    "field,reason",
    [
        ("per_trade_fraction", "PER_TRADE_RISK_CAP"),
        ("portfolio_fraction", "PORTFOLIO_RISK_CAP"),
        ("symbol_exposure_fraction", "SYMBOL_EXPOSURE_CAP"),
        ("gross_exposure_fraction", "GROSS_EXPOSURE_CAP"),
    ],
)
def test_hard_risk_caps(setup, field, reason):
    broker, _, _, _, order = setup
    broker.configure(limits=replace(broker.limits, **{field: Decimal("0.0001")}))
    assert reason in broker.submit(order()).decision.reasons


@pytest.mark.parametrize("state", ["stale", "gap", "disconnected", "degraded", "warming_up"])
def test_bad_feed_blocks_new_entries_existing_remain_managed(setup, now, state):
    broker, _, _, quote, order = setup
    opened = broker.submit(order()).fill
    quote("110", 2, state=state)
    assert "FEED_NOT_READY" in broker.submit(order("o2")).decision.reasons
    assert broker.snapshot(now).unrealized_pnl == 9
    assert broker.close(opened.position_id, "exit", now).fill is not None


def test_divergence_blocks_entries_but_stop_closes_at_gap_bid(setup, now):
    broker, _, _, quote, order = setup
    fill = broker.submit(order()).fill
    quote("80", 2, divergent=True)
    position = broker.positions[fill.position_id]
    assert position.close_fill_id is not None
    stop = next(
        result.fill
        for result in broker.orders.values()
        if result.fill and result.fill.reason == "GAP_THROUGH_STOP"
    )
    assert stop.price == 80 and broker.snapshot(now).realized_pnl == -21
    assert "DATA_DIVERGENT" in broker.submit(order("next", stop="70")).decision.reasons


def test_stale_quote_no_fabricated_stop_fill(setup, now):
    broker, rec, _, quote, order = setup
    fill = broker.submit(order()).fill
    at = now + timedelta(seconds=6)
    broker.update_quality(DataQuality(at, ((rec.spec.instrument_id, "stale"),)))
    snap = broker.snapshot(at)
    assert snap.equity is None and broker.positions[fill.position_id].close_fill_id is None
    assert "STALE_OR_MISSING_QUOTE" in broker.submit(order("later", at=at)).decision.reasons


def test_liquidity_consumed_not_reused_and_pending_stop_retries(setup, now):
    broker, _, _, quote, order = setup
    quote(seq=2, size="1")
    first = broker.submit(order()).fill
    assert "INSUFFICIENT_TOP_OF_BOOK" in broker.submit(order("second")).decision.reasons
    quote("80", 3, size="0.1")
    assert broker.positions[first.position_id].close_fill_id is None
    quote("79", 4, size="2")
    assert broker.positions[first.position_id].close_fill_id is not None


def test_request_idempotency_and_collision(setup):
    broker, _, _, _, order = setup
    first = broker.submit(order())
    head = broker.journal_head
    assert broker.submit(order()) == first and broker.journal_head == head
    with pytest.raises(ValueError):
        broker.submit(order(quantity="2"))


def test_restart_replay_same_ids_ledger_and_stop(tmp_path, setup, now):
    broker, _, _, quote, order = setup
    fill = broker.submit(order()).fill
    quote("110", 2)
    path = tmp_path / "paper.json"
    broker.save(path)
    restored = PaperBroker.load(path)
    assert restored.snapshot(now).to_dict() == broker.snapshot(now).to_dict()
    assert restored.submit(order()) == broker.orders["o1"]
    assert (
        restored.close(fill.position_id, "close", now).fill.fill_id
        == broker.close(fill.position_id, "close", now).fill.fill_id
    )
    assert PaperBroker.load(path).snapshot(now).cash == broker.cash


@pytest.mark.parametrize("change", ["cash", "command", "drop", "head", "unknown"])
def test_corrupt_checkpoint_rejected(tmp_path, setup, change):
    broker, _, _, _, order = setup
    broker.submit(order())
    path = tmp_path / "paper.json"
    broker.save(path)
    value = json.loads(path.read_text())
    if change == "cash":
        value["config"]["starting_cash"] = "99999"
    if change == "command":
        value["journal"][-1]["command"]["order"]["quantity"] = "2"
    if change == "drop":
        value["journal"].pop(0)
    if change == "head":
        value["head"] = "wrong"
    if change == "unknown":
        value["extra"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        PaperBroker.load(path)


def test_checkpoint_write_failure_rolls_back(tmp_path, setup, monkeypatch):
    broker, _, _, _, order = setup
    broker.journal_path = tmp_path / "paper.json"
    before = broker.cash
    head = broker.journal_head

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("vision.execution.paper.broker.os.replace", fail)
    with pytest.raises(OSError):
        broker.submit(order())
    assert broker.cash == before and broker.journal_head == head and not broker.positions


def test_daily_equity_loss_includes_unrealized_not_just_cash(setup, now):
    broker, _, _, quote, order = setup
    broker.configure(limits=replace(broker.limits, daily_equity_loss_fraction=Decimal("0.0005")))
    broker.submit(order())
    quote("95", 2)
    assert broker.daily_loss_latched
    assert "DAILY_EQUITY_LOSS_LIMIT" in broker.submit(order("second", stop="90")).decision.reasons


def test_drawdown_from_equity_high_water_survives_rebound(setup):
    broker, _, _, quote, order = setup
    broker.configure(limits=replace(broker.limits, drawdown_fraction=Decimal("0.0005")))
    broker.submit(order())
    quote("120", 2)
    quote("110", 3)
    assert broker.drawdown_latched and broker.high_water == 10019
    quote("121", 4)
    assert "MAX_DRAWDOWN" in broker.submit(order("blocked")).decision.reasons


def test_daily_rollover_carries_last_equity_and_drawdown_persists(setup, now):
    broker, _, _, quote, order = setup
    broker.submit(order())
    before = broker.last_equity
    next_day = now + timedelta(days=1)
    quote("80", 2, at=next_day)
    assert broker.daily_baseline == before


def test_ambient_decimal_precision_does_not_change_fills(setup, now):
    broker, _, _, _, order = setup
    with localcontext() as context:
        context.prec = 3
        result = broker.submit(order(quantity="0.123", stop="90.01"))
    assert result.fill.price == 101 and result.fill.quantity == Decimal("0.123")


def test_metadata_market_rules_bind_hash_and_block_unknown_average_price(setup):
    _, rec, _, _, _ = setup
    metadata = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                        "maxQty": "10",
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": "5",
                        "avgPriceMins": 0,
                        "applyToMarket": True,
                    },
                ],
            }
        ]
    }
    rec = replace(rec, provenance=replace(rec.provenance, metadata_digest=digest(metadata)))
    rules = rules_from_metadata(rec, metadata)
    assert rules.minimum_notional == 5
    metadata["symbols"][0]["filters"][-1]["avgPriceMins"] = 5
    with pytest.raises(ValueError):
        rules_from_metadata(rec, metadata)


def test_configuration_is_journaled_then_locked(tmp_path, setup, now):
    broker, _, _, _, order = setup
    costs = PaperCosts(slippage_bps=Decimal("5"))
    broker.configure(costs=costs)
    broker.submit(order())
    with pytest.raises(ValueError):
        broker.configure(costs=PaperCosts())
    with pytest.raises(AttributeError):
        broker.costs = PaperCosts()
    path = tmp_path / "paper.json"
    broker.save(path)
    restored = PaperBroker.load(path)
    assert (
        restored.costs == costs
        and restored.snapshot(now).to_dict() == broker.snapshot(now).to_dict()
    )


def test_hub_integration_requires_admitted_quotes(setup, now):
    from vision.market_data.adapters.binance import Subscription
    from vision.market_data.hub import MarketDataHub

    broker, rec, _, quote, order = setup
    hub = MarketDataHub(clock=lambda: now)
    hub.register(rec.spec, Subscription("BTCUSDT", ("quote",)), record=rec)
    event = quote(seq=2)
    with pytest.raises(ValueError):
        broker.capture_from_hub(hub, event)
    assert hub.ingest(event).accepted
    # Existing broker had already seen event 2; consume the next genuinely admitted quote.
    event = replace(event, event_id="q-3", sequence=3)
    hub.ingest(event)
    broker.capture_from_hub(hub, event)
    fill = broker.submit(order()).fill
    assert fill is not None
    hub.gate.disconnected()
    broker.capture_from_hub(hub)
    assert "FEED_NOT_READY" in broker.submit(order("blocked")).decision.reasons


def test_daily_loss_latch_survives_restart(tmp_path, setup, now):
    broker, _, _, quote, order = setup
    broker.configure(limits=replace(broker.limits, daily_equity_loss_fraction=Decimal("0.0005")))
    broker.submit(order())
    quote("95", 2)
    path = tmp_path / "paper.json"
    broker.save(path)
    restored = PaperBroker.load(path)
    assert restored.daily_loss_latched
    assert (
        "DAILY_EQUITY_LOSS_LIMIT" in restored.submit(order("blocked", stop="90")).decision.reasons
    )


def test_no_stop_risk_unknown_governor():
    from vision.risk.governor import HardRiskGovernor, RiskLimits

    reasons = HardRiskGovernor(RiskLimits()).assess(
        equity=Decimal(1000),
        trade_risk=Decimal(1),
        open_risk=None,
        projected_equity=Decimal(999),
        symbol_exposure=Decimal(1),
        gross_exposure=Decimal(1),
        daily_baseline=Decimal(1000),
        high_water=Decimal(1000),
    )
    assert reasons == ("UNKNOWN_OPEN_RISK",)


def test_non_nested_steps_use_exact_common_multiple():
    from vision.execution.paper.rules import common_step

    assert common_step(Decimal("0.003"), Decimal("0.002")) == Decimal("0.006")
    assert common_step(Decimal("0.001"), Decimal(0)) == Decimal("0.001")


def test_pending_stop_keeps_valuation_but_blocks_unknown_risk(setup, now):
    broker, _, _, quote, order = setup
    broker.submit(order())
    quote("80", 2, size="0.01")
    snapshot = broker.snapshot(now)
    assert (
        snapshot.equity is not None
        and snapshot.open_risk is None
        and "STOP_EXIT_PENDING" in snapshot.issues
    )
    assert "UNKNOWN_OPEN_RISK" in broker.submit(order("new", stop="70")).decision.reasons


def test_checkpoint_replay_cli(tmp_path, setup, now):
    import os
    import subprocess
    import sys

    broker, _, _, _, order = setup
    broker.submit(order())
    path = tmp_path / "paper.json"
    broker.save(path)
    environment = {
        **os.environ,
        **dict.fromkeys(
            (
                "VISION_LIVE_TRADING_ENABLED",
                "VISION_MT5_EXECUTION_ENABLED",
                "VISION_PAPER_TRADING_ENABLED",
                "VISION_AGENTS_ENABLED",
            ),
            "false",
        ),
    }
    result = subprocess.run(
        [sys.executable, "-m", "vision", "paper-replay", str(path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == broker.snapshot(now).to_dict()


@pytest.mark.parametrize(
    "payload", [None, 1, {"version": 1, "config": None, "journal": [], "head": ""}]
)
def test_malformed_checkpoint_structure_is_rejected(tmp_path, payload):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        PaperBroker.load(path)
