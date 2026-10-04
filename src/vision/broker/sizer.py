"""UniversalBrokerSizer: native account-currency calculations, no inferred multipliers."""

from decimal import ROUND_FLOOR, Decimal
from typing import Protocol

from vision.analysis.contracts import arithmetic, wire
from vision.broker.models import Calculation, SizingReceipt, number
from vision.core.instruments import digest


class BrokerCalculator(Protocol):
    def profit(self, side, symbol, volume, entry, stop): ...
    def margin(self, side, symbol, volume, entry): ...


def size(snapshot, request, policy, calculator, *, now):
    """Only an explicit eligible sizing request is accepted; raw TradeIntent is unsized."""
    from vision.broker.models import BrokerSnapshot, SizeRequest, SizingPolicy
    from vision.core.contracts import _utc

    if (
        not isinstance(snapshot, BrokerSnapshot)
        or not isinstance(request, SizeRequest)
        or not isinstance(policy, SizingPolicy)
    ):
        raise ValueError("Typed broker snapshot, eligible request and policy required")
    _utc(now)
    with arithmetic():
        return _size(snapshot, request, policy, calculator, now)


def _size(snapshot, request, policy, calculator, now):
    account = snapshot.account
    specs = {v.symbol: v for v in snapshot.specs}
    quotes = {v.symbol: v for v in snapshot.quotes}
    spec = specs.get(request.broker_symbol)
    quote = quotes.get(request.broker_symbol)
    calls = []
    values = {
        "volume": None,
        "one_lot_loss": None,
        "actual_risk": None,
        "margin": None,
        "open_risk": None,
    }

    def receipt(*reasons):
        return SizingReceipt(
            digest(wire(request)),
            digest(wire(policy)),
            snapshot.snapshot_id,
            spec.revision if spec else None,
            account.identity,
            account.currency,
            "BLOCKED" if reasons else "BROKER_SIZE_APPROVED",
            tuple(reasons),
            calculations=tuple(calls),
            **values,
        )

    def calc(kind, side, symbol, volume, entry, stop=None):
        try:
            result = (
                calculator.profit(side, symbol, volume, entry, stop)
                if kind == "profit"
                else calculator.margin(side, symbol, volume, entry)
            )
            if result is not None:
                number(result)
        except (ValueError, ArithmeticError, RuntimeError, OSError):
            result = None
        calls.append(Calculation(kind, side, symbol, volume, entry, stop, result, account.currency))
        return result

    if not account.demo or not account.connected:
        return receipt("DEMO_ACCOUNT_CONNECTED_REQUIRED")
    if request.expected_account_identity != account.identity:
        return receipt("ACCOUNT_IDENTITY_CHANGED")
    if not request.strategy_eligible:
        return receipt("STRATEGY_INELIGIBLE")
    if not request.requested_at <= now < request.expires_at:
        return receipt("INTENT_EXPIRED_OR_FUTURE")
    times = [
        snapshot.as_of,
        account.observed_at,
        *(v.observed_at for v in snapshot.specs),
        *(v.observed_at for v in snapshot.quotes),
    ]
    if any(not 0 <= (now - at).total_seconds() <= policy.max_age.total_seconds() for at in times):
        return receipt("STALE_OR_FUTURE_SNAPSHOT")
    if max(times) - min(times) > policy.max_skew:
        return receipt("SNAPSHOT_SKEW")
    if spec is None or quote is None or spec.canonical != request.canonical:
        return receipt("SYMBOL_MAPPING_OR_SPEC_MISSING")
    if spec.revision != request.expected_spec_revision:
        return receipt("SPEC_REVISION_CHANGED")
    if snapshot.pending_orders:
        return receipt("PENDING_ORDER_RISK_UNSUPPORTED")
    if account.equity <= 0 or account.free_margin <= 0:
        return receipt("ACCOUNT_ECONOMICS_INVALID")
    if request.risk_budget > account.equity * policy.per_trade_fraction:
        return receipt("PER_TRADE_RISK_CAP")
    if spec.trade_mode not in ({1, 4} if request.side == "LONG" else {2, 4}):
        return receipt("BROKER_DIRECTION_DISABLED")
    if spec.volume_min % spec.volume_step != 0:
        return receipt("UNSUPPORTED_VOLUME_GRID")
    if any(
        not 0 <= (now - q.source_at).total_seconds() <= policy.max_age.total_seconds()
        for q in snapshot.quotes
    ):
        return receipt("STALE_OR_FUTURE_BROKER_QUOTE")
    entry = quote.ask if request.side == "LONG" else quote.bid
    trigger = quote.bid if request.side == "LONG" else quote.ask
    if (request.side == "LONG" and request.stop >= trigger) or (
        request.side == "SHORT" and request.stop <= trigger
    ):
        return receipt("INVALID_SL_SIDE")
    if request.stop % spec.tick_size:
        return receipt("STOP_OFF_TICK_GRID")
    if abs(trigger - request.stop) < max(spec.stops_level, spec.freeze_level) * spec.point:
        return receipt("STOP_TOO_CLOSE")
    loss = calc("profit", request.side, spec.symbol, Decimal(1), entry, request.stop)
    if loss is None or loss >= 0:
        return receipt("ONE_LOT_LOSS_UNAVAILABLE")
    values["one_lot_loss"] = -loss
    available = request.risk_budget - policy.loss_buffer
    if available <= 0:
        return receipt("LOSS_BUFFER_EXHAUSTS_BUDGET")
    raw = min(available / -loss, spec.volume_max)
    if spec.volume_limit > 0:
        existing = sum(
            (
                p.volume
                for p in snapshot.positions
                if p.symbol == spec.symbol and p.side == request.side
            ),
            Decimal(0),
        )
        raw = min(raw, max(Decimal(0), spec.volume_limit - existing))
    volume = (raw / spec.volume_step).to_integral_value(rounding=ROUND_FLOOR) * spec.volume_step
    if volume < spec.volume_min:
        return receipt("BELOW_MINIMUM_VOLUME")
    actual = calc("profit", request.side, spec.symbol, volume, entry, request.stop)
    if actual is None or actual >= 0:
        return receipt("FINAL_RISK_UNAVAILABLE")
    risk = -actual + policy.loss_buffer
    values.update(volume=volume, actual_risk=risk)
    if risk > request.risk_budget or risk > account.equity * policy.per_trade_fraction:
        return receipt("FINAL_RISK_CAP")
    margin = calc("margin", request.side, spec.symbol, volume, entry)
    values["margin"] = margin
    if margin is None or margin < 0:
        return receipt("MARGIN_UNAVAILABLE")
    if margin + policy.margin_reserve > account.free_margin:
        return receipt("INSUFFICIENT_FREE_MARGIN")
    open_risk = Decimal(0)
    for p in snapshot.positions:
        current, p_spec = quotes.get(p.symbol), specs.get(p.symbol)
        if p.stop <= 0 or current is None or p_spec is None:
            return receipt("UNKNOWN_OR_NO_SL_EXISTING_RISK")
        close = current.bid if p.side == "LONG" else current.ask
        if (p.side == "LONG" and p.stop >= close) or (p.side == "SHORT" and p.stop <= close):
            return receipt("EXISTING_STOP_CROSSED")
        # Additional equity loss from the current closeable quote, not loss from entry.
        result = calc("profit", p.side, p.symbol, p.volume, close, p.stop)
        if result is None or result >= 0:
            return receipt("EXISTING_RISK_CALCULATION_UNAVAILABLE")
        open_risk += -result + policy.loss_buffer
    values["open_risk"] = open_risk
    if open_risk + risk > account.equity * policy.aggregate_fraction:
        return receipt("AGGREGATE_OPEN_RISK_CAP")
    return receipt()
