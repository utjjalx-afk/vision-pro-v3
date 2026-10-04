"""Bounded canonical-bar research runner sharing the cash Spot paper broker."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from vision.analysis.contracts import arithmetic, number, wire
from vision.core.codec import decimal_string, event_from_dict
from vision.core.contracts import (
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    QuotePayload,
    TimestampBasis,
    _identifier,
)
from vision.core.instruments import InstrumentRecord, digest
from vision.core.state.portfolio import DataQuality, Side
from vision.core.state.replay import record_from_dict, shape
from vision.execution.paper.broker import PaperBroker
from vision.execution.paper.models import MarketRules, PaperCosts, PaperMode, PaperOrder
from vision.journal.repository import canonical
from vision.risk.governor import RiskLimits


class ResultStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    version: str = "close-trend-v1"
    indicator: str = "SMA"
    lookback: int = 3
    warmup: int = 12
    threshold: Decimal = Decimal("0.001")
    stop_fraction: Decimal = Decimal("0.05")
    target_fraction: Decimal = Decimal("0.10")
    quantity: Decimal = Decimal("1")
    recursive_tolerance: Decimal = Decimal("0.0001")

    def __post_init__(self):
        if self.version != "close-trend-v1" or self.indicator not in {"SMA", "EMA"}:
            raise ValueError("Supported frozen declarative rule required")
        if (
            type(self.lookback) is not int
            or type(self.warmup) is not int
            or not 2 <= self.lookback <= self.warmup <= 256
        ):
            raise ValueError("Bounded explicit indicator warmup required")
        for value in (
            self.threshold,
            self.stop_fraction,
            self.target_fraction,
            self.quantity,
            self.recursive_tolerance,
        ):
            number(value)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("Finite nonnegative Decimal rule parameters required")
        if not 0 < self.stop_fraction < 1 or self.target_fraction <= 0 or self.quantity <= 0:
            raise ValueError("Explicit positive economics required")


@dataclass(frozen=True, slots=True)
class AcceptancePolicy:
    minimum_trades: int = 5
    minimum_net_pnl: Decimal = Decimal("0")
    maximum_drawdown_fraction: Decimal = Decimal("0.10")
    maximum_pbo: Decimal = Decimal("0.20")

    def __post_init__(self):
        if type(self.minimum_trades) is not int or not 1 <= self.minimum_trades <= 500:
            raise ValueError("Explicit bounded trade sample minimum required")
        for value in (self.minimum_net_pnl, self.maximum_drawdown_fraction, self.maximum_pbo):
            number(value)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("Finite Decimal acceptance limits required")
        if not 0 <= self.maximum_drawdown_fraction <= 1:
            raise ValueError("Invalid drawdown threshold")
        if not 0 <= self.maximum_pbo <= 1:
            raise ValueError("Invalid PBO threshold")


@dataclass(frozen=True, slots=True)
class BacktestInput:
    events: tuple[CanonicalMarketEvent, ...]
    record: InstrumentRecord
    rules: MarketRules
    strategy: StrategyDefinition
    costs: PaperCosts
    limits: RiskLimits
    acceptance: AcceptancePolicy
    starting_cash: Decimal
    commit_sha: str
    # Optional canonical finer bars; never interpolated or fabricated.
    lower_events: tuple[CanonicalMarketEvent, ...] = ()
    # Causal caller labels pinned to event IDs, not inferred from future PnL.
    regimes: tuple[tuple[str, str], ...] = ()
    evaluation_start: int = 0

    def __post_init__(self):
        for values in (self.events, self.lower_events, self.regimes):
            if type(values) is not tuple or len(values) > 500:
                raise ValueError("Immutable bounded offline inputs required")
        if not self.events or not all(
            isinstance(e, CanonicalMarketEvent) for e in self.events + self.lower_events
        ):
            raise ValueError("Canonical dataset required")
        if type(self.evaluation_start) is not int or not 0 <= self.evaluation_start < len(
            self.events
        ):
            raise ValueError("Explicit evaluation boundary required")
        if not isinstance(self.record, InstrumentRecord) or not isinstance(self.rules, MarketRules):
            raise ValueError("Pinned instrument economics required")
        if not isinstance(self.strategy, StrategyDefinition) or not isinstance(
            self.costs, PaperCosts
        ):
            raise ValueError("Typed rule/cost policy required")
        if not isinstance(self.limits, RiskLimits) or not isinstance(
            self.acceptance, AcceptancePolicy
        ):
            raise ValueError("Typed risk/acceptance policy required")
        if self.costs.mode is not PaperMode.REALISTIC:
            raise ValueError("Backtests require explicit realistic paper costs")
        number(self.starting_cash)
        if (
            not isinstance(self.starting_cash, Decimal)
            or not self.starting_cash.is_finite()
            or self.starting_cash <= 0
        ):
            raise ValueError("Positive Decimal starting cash required")
        if not isinstance(self.commit_sha, str) or not re.fullmatch(
            "[0-9a-f]{40}", self.commit_sha
        ):
            raise ValueError("Explicit code revision required")
        for pair in self.regimes:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("Immutable event/regime pair required")
            for value in pair:
                _identifier(value)
        if len({p[0] for p in self.regimes}) != len(self.regimes):
            raise ValueError("Unique causal regime labels required")
        if set(dict(self.regimes)) - {e.event_id for e in self.events}:
            raise ValueError("Regime labels must reference dataset events")

    @property
    def dataset_hash(self):
        return digest(wire((self.events, self.lower_events, self.regimes)))

    def config(self):
        value = wire(self)
        return {
            k: v for k, v in value.items() if k not in {"events", "lower_events", "regimes"}
        } | {
            "dataset_hash": self.dataset_hash,
            "timing": "signal-at-close-next-bar-open",
            "intrabar": "stop-first-if-ambiguous",
            "liquidity": "explicit-fixed-synthetic-L1-equal-to-order-quantity",
            "drawdown": "close-to-close-marked-equity; not intrabar maximum",
        }


@dataclass(frozen=True, slots=True)
class BacktestRun:
    run_id: str
    dataset_hash: str
    config_hash: str
    commit_sha: str
    strategy_version: str
    status: ResultStatus
    result_json: str
    input_json: str

    def __post_init__(self):
        if not isinstance(self.status, ResultStatus):
            raise ValueError("Explicit result status required")
        for text in (self.result_json, self.input_json):
            if canonical(json.loads(text)) != text:
                raise ValueError("Canonical immutable run records required")

    def to_dict(self):
        return {**wire(self), "result": json.loads(self.result_json)}


def indicator(history, strategy):
    """Only closed canonical bars enter this pure shared rule."""
    if len(history) < strategy.lookback:
        return None
    with arithmetic():
        closes = [e.payload.close for e in history]
        if strategy.indicator == "SMA":
            return sum(closes[-strategy.lookback :]) / strategy.lookback
        value, alpha = closes[0], Decimal(2) / (strategy.lookback + 1)
        for close in closes[1:]:
            value = alpha * close + (1 - alpha) * value
        return +value


def decision(history, strategy):
    if len(history) < strategy.warmup:
        return "HOLD"
    with arithmetic():
        mean, close = indicator(history, strategy), history[-1].payload.close
        if close > mean * (1 + strategy.threshold):
            return "ENTER"
        if close < mean * (1 - strategy.threshold):
            return "EXIT"
        return "HOLD"


def dataset_audit(events, record):
    issues, ids, previous = [], set(), None
    for event in events:
        if event.event_id in ids:
            issues.append("DUPLICATE_EVENT")
        ids.add(event.event_id)
        p = event.payload
        if not isinstance(p, BarPayload) or p.open_ts is None:
            issues.append("CLOSED_BAR_REQUIRED")
            continue
        end = p.open_ts + timedelta(seconds=p.interval_seconds)
        if (p.open_ts - datetime(1970, 1, 1, tzinfo=UTC)) % timedelta(
            seconds=p.interval_seconds
        ) != timedelta(0):
            issues.append("UTC_BAR_ALIGNMENT")
        if (
            event.source_ts < end - timedelta(milliseconds=1)
            or event.source_ts > end
            or event.received_ts < event.source_ts
        ):
            issues.append("BAR_TIMESTAMP")
        if (
            event.instrument_id != record.spec.instrument_id
            or event.source != record.provenance.source
        ):
            issues.append("SOURCE_SPEC_MISMATCH")
        if event.timestamp_basis is not TimestampBasis.EXCHANGE:
            issues.append("TIMEZONE_TIMESTAMP_BASIS")
        if event.continuity not in {"contiguous", "snapshot"}:
            issues.append("SOURCE_QUALITY_UNVERIFIED")
        if previous is not None:
            old = previous.payload
            if (
                p.open_ts != old.open_ts + timedelta(seconds=old.interval_seconds)
                or p.interval_seconds != old.interval_seconds
            ):
                issues.append("MISSING_OR_REORDERED_BAR")
            if event.source_epoch != previous.source_epoch or event.sequence <= previous.sequence:
                issues.append("SOURCE_CONTINUITY")
        previous = event
    return tuple(sorted(set(issues)))


def reconstruct(parent, lower):
    """Validate coverage/OHLCV before finer bars can resolve a parent range."""
    p = parent.payload
    end = p.open_ts + timedelta(seconds=p.interval_seconds)
    children = tuple(e for e in lower if p.open_ts <= e.payload.open_ts < end)
    if not children:
        return (parent,), None
    step = children[0].payload.interval_seconds
    if (
        step >= p.interval_seconds
        or p.interval_seconds % step
        or len(children) != p.interval_seconds // step
        or children[0].payload.open_ts != p.open_ts
        or children[-1].payload.open_ts + timedelta(seconds=step) != end
        or any(e.source_epoch != parent.source_epoch for e in children)
        or (
            children[0].payload.open,
            max(e.payload.high for e in children),
            min(e.payload.low for e in children),
            children[-1].payload.close,
            sum(e.payload.volume for e in children),
        )
        != (p.open, p.high, p.low, p.close, p.volume)
    ):
        return (parent,), "LOWER_TF_RECONSTRUCTION_INVALID"
    return children, None


def run(inputs):
    """Research-only. No callbacks, provider I/O, optimization or promotion."""
    if not isinstance(inputs, BacktestInput):
        raise ValueError("BacktestInput required")
    with arithmetic():
        return _run(inputs)


def _run(inputs):
    audit = list(dataset_audit(inputs.events, inputs.record))
    first = inputs.events[inputs.evaluation_start].payload
    if isinstance(first, BarPayload) and first.open_ts is not None:
        if inputs.record.provenance.observed_at > first.open_ts:
            audit.append("FUTURE_INSTRUMENT_SPEC")
    if inputs.lower_events:
        audit.extend(dataset_audit(inputs.lower_events, inputs.record))
        if any(
            not isinstance(e.payload, BarPayload) or e.payload.open_ts is None
            for e in inputs.lower_events
        ):
            audit.append("LOWER_TF_RECONSTRUCTION_INVALID")
    signals, trades, equity, ambiguities = [], [], [], []
    result = {
        "audits": audit,
        "signals": signals,
        "trades": trades,
        "equity": equity,
        "ambiguities": ambiguities,
        "checkpoint": None,
        "net_pnl": None,
        "maximum_drawdown": None,
        "regime_pnl": {},
    }
    if audit:
        return finish(inputs, ResultStatus.FAIL, result)
    broker = PaperBroker(
        inputs.record.spec.quote_currency,
        inputs.starting_cash,
        inputs.events[inputs.evaluation_start].payload.open_ts,
        costs=inputs.costs,
        limits=inputs.limits,
    )
    broker.register(inputs.record, inputs.rules)
    seq, active, pending, high_water, drawdown = 0, None, None, inputs.starting_cash, Decimal(0)
    targets, entry_regimes = {}, {}
    labels = dict(inputs.regimes)

    def quote(price, at, source_event, purpose):
        nonlocal seq
        seq += 1
        event = CanonicalMarketEvent(
            digest({"bar": source_event.event_id, "point": purpose, "sequence": seq}),
            source_event.source,
            source_event.instrument_id,
            EventType.QUOTE,
            at,
            at,
            seq,
            QuotePayload(price, price, inputs.strategy.quantity, inputs.strategy.quantity),
            source_epoch=source_event.source_epoch,
            continuity="snapshot",
        )
        broker.update_quote(event, DataQuality(at, ((event.instrument_id, "healthy"),)))

    for index, event in enumerate(inputs.events):
        p, opening = event.payload, event.payload.open_ts
        if index < inputs.evaluation_start:
            continue
        quote(p.open, opening, event, "open")
        if active is not None and broker.positions[active].close_fill_id is not None:
            active = None
        if active is not None and p.open >= targets[active]:
            broker.close(active, f"target-gap:{event.event_id}", opening)
            active = None
        if pending is not None:
            action, signal = pending
            if signal["available_at"] > opening:
                audit.append("SIGNAL_NOT_AVAILABLE_AT_NEXT_OPEN")
            elif action == "EXIT" and active is not None:
                broker.close(active, f"rule-exit:{event.event_id}", opening)
                active = None
            elif action == "ENTER" and active is None:
                order = PaperOrder(
                    f"entry:{event.event_id}",
                    inputs.record.spec.instrument_id,
                    inputs.record.revision,
                    Side.LONG,
                    inputs.strategy.quantity,
                    decimal_string(signal["stop"]),
                    opening,
                )
                entry = broker.submit(order)
                if entry.fill is not None:
                    active = entry.fill.position_id
                    targets[active] = decimal_string(signal["target"])
                    entry_regimes[active] = signal["regime"]
                else:
                    audit.append("RISK_REJECTED_ENTRY")
        children, problem = reconstruct(event, inputs.lower_events)
        if problem:
            audit.append(problem)
        for child in children:
            c, at = child.payload, child.payload.open_ts
            if child is not event:
                quote(c.open, at, child, "child-open")
            if active is not None and broker.positions[active].close_fill_id is not None:
                active = None
            if active is None:
                continue
            stop, target = broker.positions[active].stop_loss, targets[active]
            if c.open >= target:
                broker.close(active, f"target-gap:{child.event_id}", at)
                active = None
                continue
            stop_hit, target_hit = c.low <= stop, c.high >= target
            if stop_hit and target_hit:
                ambiguities.append(child.event_id)
            # Intrabar ordering unknown: conservative stop first; status is inconclusive.
            if stop_hit:
                quote(stop, at + timedelta(seconds=c.interval_seconds) / 3, child, "stop")
                active = None if broker.positions[active].close_fill_id else active
            elif target_hit:
                exit_at = at + timedelta(seconds=c.interval_seconds) / 3
                quote(target, exit_at, child, "target")
                broker.close(active, f"target:{child.event_id}", exit_at)
                active = None
        closing = opening + timedelta(seconds=p.interval_seconds)
        quote(p.close, closing - timedelta(microseconds=1), event, "close")
        snap = broker.snapshot(closing - timedelta(microseconds=1))
        if snap.equity is None:
            audit.append("VALUATION_UNAVAILABLE")
            continue
        high_water = max(high_water, snap.equity)
        drawdown = max(drawdown, (high_water - snap.equity) / high_water)
        equity.append({"event_id": event.event_id, "equity": str(snap.equity)})
        history = inputs.events[: index + 1]
        if inputs.strategy.indicator == "EMA" and len(history) > inputs.strategy.warmup:
            full = indicator(history, inputs.strategy)
            truncated = indicator(history[-inputs.strategy.warmup :], inputs.strategy)
            if abs(full - truncated) / full > inputs.strategy.recursive_tolerance:
                audit.append("RECURSIVE_INDICATOR_CONTAMINATION")
        action = decision(history, inputs.strategy)
        signal = {
            "event_id": event.event_id,
            "action": action,
            "available_at": max(closing, event.received_ts),
            "stop": str(p.close * (1 - inputs.strategy.stop_fraction)),
            "target": str(p.close * (1 + inputs.strategy.target_fraction)),
            "regime": labels.get(event.event_id, "UNLABELLED"),
        }
        signals.append(signal)
        pending = (action, signal)
    if active is not None:
        audit.append("OPEN_POSITION_AT_END")
    from vision.outcomes.paper import grade

    checkpoint = broker.checkpoint()
    for order in broker.orders.values():
        if order.order.close_position_id is None and order.fill is not None:
            g = grade(
                checkpoint, order.order.request_id, at=broker.last_at, receipt_id=digest(checkpoint)
            )
            if g.realized_pnl is not None:
                exit_order = next(
                    r
                    for r in broker.orders.values()
                    if r.fill is not None and r.order_id == g.lineage.exit_order_id
                )
                trigger = (
                    "STOP"
                    if g.state.value == "STOP_HIT"
                    else "TARGET_MARKET"
                    if exit_order.order.request_id.startswith("target")
                    else "RULE_NEXT_OPEN"
                )
                trades.append({**wire(g), "exit_trigger": trigger})
                label = entry_regimes.get(order.fill.position_id, "UNLABELLED")
                result["regime_pnl"][label] = str(
                    Decimal(result["regime_pnl"].get(label, "0")) + g.realized_pnl
                )
    result.update(
        audits=sorted(set(audit)),
        checkpoint=checkpoint,
        net_pnl=str(broker.realized),
        maximum_drawdown=str(drawdown),
    )
    policy = inputs.acceptance
    if audit or ambiguities:
        status = ResultStatus.INCONCLUSIVE
    elif broker.realized < policy.minimum_net_pnl or drawdown > policy.maximum_drawdown_fraction:
        status = ResultStatus.FAIL
    elif len(trades) < policy.minimum_trades:
        status = ResultStatus.INCONCLUSIVE
    else:
        status = ResultStatus.PASS
    return finish(inputs, status, result)


def finish(inputs, status, result):
    config_hash = digest(inputs.config())

    def encode(value):
        if isinstance(value, dict):
            return {k: encode(v) for k, v in value.items()}
        if isinstance(value, list):
            return [encode(v) for v in value]
        return wire(value)

    result_json, input_json = canonical(encode(result)), canonical(wire(inputs))
    identity = digest({"config": config_hash, "result": result_json, "algorithm": "phase8-v1"})
    return BacktestRun(
        identity,
        inputs.dataset_hash,
        config_hash,
        inputs.commit_sha,
        inputs.strategy.version,
        status,
        result_json,
        input_json,
    )


def inputs_from_dict(value):
    data = shape(value, BacktestInput)
    for name in ("events", "lower_events", "regimes"):
        if not isinstance(data[name], list):
            raise ValueError("Explicit replay arrays required")
    data["events"] = tuple(event_from_dict(e) for e in data["events"])
    data["lower_events"] = tuple(event_from_dict(e) for e in data["lower_events"])
    data["regimes"] = tuple(tuple(p) for p in data["regimes"])
    data["record"] = record_from_dict(data["record"])
    for name, cls in (
        ("rules", MarketRules),
        ("strategy", StrategyDefinition),
        ("costs", PaperCosts),
        ("limits", RiskLimits),
        ("acceptance", AcceptancePolicy),
    ):
        item = shape(data[name], cls)
        for key, v in item.items():
            if cls is PaperCosts and key == "mode":
                item[key] = PaperMode(v)
            elif isinstance(v, str) and key not in {
                "version",
                "indicator",
                "instrument_id",
                "spec_revision",
                "evidence_digest",
                "model",
            }:
                item[key] = decimal_string(v)
        data[name] = cls(**item)
    data["starting_cash"] = decimal_string(data["starting_cash"])
    return BacktestInput(**data)


def replay(value):
    try:
        if not isinstance(value, dict) or set(value) != {"input", "run"}:
            raise ValueError("Strict backtest replay envelope required")
        result = run(inputs_from_dict(value["input"]))
        if result.to_dict() != value["run"]:
            raise ValueError("Backtest replay result mismatch")
        return result
    except (TypeError, KeyError, IndexError, OverflowError) as error:
        raise ValueError("Invalid backtest replay structure") from error
