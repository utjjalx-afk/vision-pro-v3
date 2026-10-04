"""Exact compilation and preflight for Phase-8 research only. No deployment capabilities."""

import json
from dataclasses import dataclass
from datetime import datetime

from vision.analysis.contracts import arithmetic, wire
from vision.core.codec import decimal_string, event_from_dict, utc_string
from vision.core.contracts import BarPayload, CanonicalMarketEvent, _utc
from vision.core.instruments import InstrumentRecord, digest
from vision.core.state.replay import record_from_dict, shape
from vision.execution.paper.models import MarketRules, PaperMode
from vision.journal.repository import canonical
from vision.research.audit import lookahead_audit
from vision.research.backtest import (
    BacktestInput,
    dataset_audit,
    decision,
    indicator,
    inputs_from_dict,
    reconstruct,
    run,
)
from vision.research.backtest import (
    StrategyDefinition as CompiledRule,
)
from vision.strategies.dsl import (
    Lifecycle,
    StrategyDefinition,
    acceptance,
    cost_model,
    decimal,
    integer,
    object_fields,
    parse,
    preview,
    risk_limits,
    text,
)

COMPILER = "phase9-v1"


@dataclass(frozen=True, slots=True)
class StrategyDataset:
    events: tuple
    record: InstrumentRecord
    rules: MarketRules
    lower_events: tuple = ()
    regimes: tuple = ()
    evaluation_start: int = 0

    def __post_init__(self):
        for values in (self.events, self.lower_events, self.regimes):
            if type(values) is not tuple or len(values) > 500:
                raise ValueError("Immutable bounded dataset required")
        if not self.events or not all(
            isinstance(e, CanonicalMarketEvent) for e in self.events + self.lower_events
        ):
            raise ValueError("Canonical strategy dataset required")
        if not isinstance(self.record, InstrumentRecord) or not isinstance(self.rules, MarketRules):
            raise ValueError("Explicit instrument economics required")
        integer(self.evaluation_start, 0, len(self.events) - 1)
        for pair in self.regimes:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("Immutable event/regime pair required")
            text(pair[0])
            text(pair[1])
        if len(dict(self.regimes)) != len(self.regimes):
            raise ValueError("Unique regime references required")
        if set(dict(self.regimes)) - {e.event_id for e in self.events}:
            raise ValueError("Unknown regime event")

    @property
    def dataset_hash(self):
        return digest(wire(self))


def dataset_from_backtest(value):
    if not isinstance(value, BacktestInput):
        raise ValueError("Typed backtest input required")
    return StrategyDataset(
        value.events,
        value.record,
        value.rules,
        value.lower_events,
        value.regimes,
        value.evaluation_start,
    )


def dataset_from_dict(value):
    data = shape(value, StrategyDataset)
    for key in ("events", "lower_events", "regimes"):
        if not isinstance(data[key], list) or len(data[key]) > 500:
            raise ValueError("Bounded explicit dataset arrays required")
    data["events"] = tuple(event_from_dict(e) for e in data["events"])
    data["lower_events"] = tuple(event_from_dict(e) for e in data["lower_events"])
    if any(not isinstance(pair, list) or len(pair) != 2 for pair in data["regimes"]):
        raise ValueError("Explicit event/regime arrays required")
    data["regimes"] = tuple(tuple(pair) for pair in data["regimes"])
    data["record"] = record_from_dict(data["record"])
    rules = shape(data["rules"], MarketRules)
    for key in (
        "minimum_quantity",
        "maximum_quantity",
        "quantity_step",
        "minimum_notional",
        "maximum_notional",
    ):
        rules[key] = decimal_string(rules[key]) if rules[key] is not None else None
    data["rules"] = MarketRules(**rules)
    return StrategyDataset(**data)


@dataclass(frozen=True, slots=True)
class Compilation:
    compilation_id: str
    strategy_id: str
    config_hash: str
    dataset_hash: str
    compiler_version: str
    status: str
    reasons: tuple[str, ...]
    preview_text: str
    preview_hash: str
    artifact_json: str
    dataset_json: str
    compiled_json: str | None

    def to_dict(self):
        return wire(self)


def compile_strategy(artifact, dataset):
    if not isinstance(artifact, StrategyDefinition) or not isinstance(dataset, StrategyDataset):
        raise ValueError("Validated artifact and canonical dataset required")
    v, p, reasons = artifact.value, preview(artifact), []
    market, execution, risk = v["market"], v["execution"], v["risk"]
    feature = v["features"][0]
    entry, exit_rule = v["entry"], v["exit"]
    e, x = entry["condition"], exit_rule["condition"]
    required = {"cash_spot_long", "market_orders", "stop_market", "target_trigger_market"}
    if v["lifecycle"] not in {"CANDIDATE", "TESTING"}:
        reasons.append("LIFECYCLE_RESEARCH_BLOCKED")
    if (
        len(v["features"]) != 1
        or feature["id"] != "trend"
        or feature["kind"] not in {"SMA", "EMA"}
        or feature["input"] != "close"
    ):
        reasons.append("UNSUPPORTED_FEATURE")
    if (
        entry["action"] != "ENTER_LONG"
        or exit_rule["action"] != "CLOSE_POSITION"
        or e["left"] != "close"
        or x["left"] != "close"
        or e["operator"] != "GT"
        or x["operator"] != "LT"
        or e["right_feature"] != "trend"
        or x["right_feature"] != "trend"
    ):
        reasons.append("UNSUPPORTED_ENTRY_EXIT")
    with arithmetic():
        threshold = decimal(e["multiplier"]) - 1
        if threshold < 0 or threshold >= 1 or decimal(x["multiplier"]) != 1 - threshold:
            reasons.append("ASYMMETRIC_OR_UNSUPPORTED_THRESHOLDS")
    if (
        feature["lookback"] < 2
        or feature["warmup"] < feature["lookback"]
        or feature["warmup"] != market["warmup"]
    ):
        reasons.append("WARMUP_RULE_MISMATCH")
    if (
        market["canonical_symbol"] != dataset.record.canonical.key
        or market["asset_class"] != "crypto"
        or dataset.record.spec.venue not in market["instrument"]["venues"]
        or dataset.record.spec.venue not in {"BINANCE_SPOT", "BYBIT_SPOT"}
        or set(market["instrument"]["venues"]) - {"BINANCE_SPOT", "BYBIT_SPOT"}
        or market["instrument"]["quantity_unit"] != "base"
        or dataset.record.quantity_unit.value != "base"
        or market["instrument"]["valuation_model"] != "linear_quote"
        or dataset.record.valuation_model != "linear_quote"
    ):
        reasons.append("INSTRUMENT_CONSTRAINTS")
    if (
        dataset.rules.spec_revision != dataset.record.revision
        or dataset.rules.instrument_id != dataset.record.spec.instrument_id
        or dataset.rules.evidence_digest != dataset.record.provenance.metadata_digest
    ):
        reasons.append("INSTRUMENT_RULE_PROVENANCE")
    if market["required_data"] != ["closed_bars"]:
        reasons.append("UNSUPPORTED_REQUIRED_DATA")
    if (
        execution["mode"] != "BACKTEST_ONLY"
        or execution["timing"] != "SIGNAL_AT_CLOSE_NEXT_BAR_OPEN"
        or execution["ambiguity"] != "INCONCLUSIVE_STOP_FIRST"
        or execution["liquidity"] != "FIXED_SYNTHETIC_L1"
        or set(execution["capabilities"]) != required
        or cost_model(execution["costs"]).mode is not PaperMode.REALISTIC
    ):
        reasons.append("UNSUPPORTED_EXECUTION_SEMANTICS")
    stop, target, quantity = risk["invalidation"], risk["profit_exit"], risk["quantity"]
    if (
        quantity["unit"] != "base"
        or quantity["sizing"] != "FIXED"
        or {k: v for k, v in stop.items() if k != "fraction"}
        != {
            "kind": "STOP_LOSS",
            "anchor": "SIGNAL_CLOSE",
            "trigger": "BID_LTE",
            "fill": "MARKET",
            "gap_policy": "OPEN_QUOTE",
            "no_stop": "BLOCK",
        }
        or not 0 < decimal(stop["fraction"]) < 1
        or {k: v for k, v in target.items() if k != "fraction"}
        != {"kind": "TAKE_PROFIT", "anchor": "SIGNAL_CLOSE", "trigger": "BID_GTE", "fill": "MARKET"}
    ):
        reasons.append("UNSUPPORTED_INVALIDATION_RISK")
    data_issues = dataset_audit(dataset.events, dataset.record)
    if dataset.lower_events:
        data_issues += dataset_audit(dataset.lower_events, dataset.record)
    reasons.extend("DATA_" + reason for reason in data_issues)
    for old, new in zip(dataset.events[:-1], dataset.events[1:], strict=True):
        if old.received_ts > new.received_ts:
            reasons.append("DATA_RECEIPT_ORDER")
        if isinstance(new.payload, BarPayload) and new.payload.open_ts is not None:
            if old.received_ts > new.payload.open_ts:
                reasons.append("DATA_SIGNAL_UNAVAILABLE_NEXT_OPEN")
    if any(
        not isinstance(event.payload, BarPayload)
        or event.payload.interval_seconds != market["timeframe_seconds"]
        for event in dataset.events
    ):
        reasons.append("DECLARED_TIMEFRAME_MISMATCH")
    first = dataset.events[dataset.evaluation_start].payload
    if (
        isinstance(first, BarPayload)
        and first.open_ts is not None
        and dataset.record.provenance.observed_at > first.open_ts
    ):
        reasons.append("DATA_FUTURE_INSTRUMENT_SPEC")
    if len(dataset.events) < market["warmup"] + 1:
        reasons.append("INSUFFICIENT_WARMUP_AND_NEXT_OPEN")
    compiled_json = None
    if not reasons:
        rule = CompiledRule(
            "close-trend-v1",
            feature["kind"],
            feature["lookback"],
            feature["warmup"],
            threshold,
            decimal(stop["fraction"]),
            decimal(target["fraction"]),
            decimal(quantity["value"]),
            decimal(feature["recursive_tolerance"]),
        )
        for event in dataset.events:
            _, problem = reconstruct(event, dataset.lower_events)
            if problem:
                reasons.append("DATA_" + problem)
        lookahead = lookahead_audit(dataset.events, lambda rows, i: decision(rows[: i + 1], rule))
        if lookahead["status"] != "PASS":
            reasons.append("LOOKAHEAD_AUDIT_FAILED")
        if rule.indicator == "EMA":
            with arithmetic():
                for i in range(rule.warmup, len(dataset.events)):
                    history = dataset.events[: i + 1]
                    full = indicator(history, rule)
                    if (
                        abs(full - indicator(history[-rule.warmup :], rule)) / full
                        > rule.recursive_tolerance
                    ):
                        reasons.append("RECURSIVE_INDICATOR_CONTAMINATION")
                        break
        inputs = BacktestInput(
            dataset.events,
            dataset.record,
            dataset.rules,
            rule,
            cost_model(execution["costs"]),
            risk_limits(risk["hard_limits"]),
            acceptance(v["acceptance"]),
            decimal(v["research"]["starting_cash"]),
            v["research"]["commit_sha"],
            dataset.lower_events,
            dataset.regimes,
            dataset.evaluation_start,
        )
        if not reasons:
            compiled_json = canonical(wire(inputs))
    reasons = tuple(sorted(set(reasons)))
    identity = digest(
        {
            "artifact": artifact.strategy_id,
            "dataset": dataset.dataset_hash,
            "preview": p.preview_hash,
            "compiler": COMPILER,
            "reasons": reasons,
            "compiled": compiled_json,
        }
    )
    return Compilation(
        identity,
        artifact.strategy_id,
        artifact.config_hash,
        dataset.dataset_hash,
        COMPILER,
        "BLOCKED" if reasons else "READY",
        reasons,
        p.text,
        p.preview_hash,
        artifact.artifact_json,
        canonical(wire(dataset)),
        compiled_json,
    )


@dataclass(frozen=True, slots=True)
class PreviewConfirmation:
    compilation_id: str
    strategy_id: str
    preview_hash: str
    reviewer: str
    confirmed_at: datetime

    def __post_init__(self):
        for value in (self.compilation_id, self.strategy_id, self.preview_hash):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError("Exact artifact/compilation/preview hashes required")
        text(self.reviewer)
        _utc(self.confirmed_at)


def confirm_preview(compilation, *, preview_hash, reviewer, at):
    if not isinstance(compilation, Compilation) or compilation.status != "READY":
        raise ValueError("Ready audited compilation required")
    if preview_hash != compilation.preview_hash:
        raise ValueError("Confirmation must match the exact preview")
    return PreviewConfirmation(
        compilation.compilation_id, compilation.strategy_id, preview_hash, reviewer, at
    )


@dataclass(frozen=True, slots=True)
class StrategyRun:
    result_id: str
    strategy_id: str
    compilation_id: str
    lifecycle: str
    status: str
    reasons: tuple[str, ...]
    confirmation_json: str | None
    backtest_json: str | None

    def to_dict(self):
        return wire(self)


def execute(compilation, confirmation=None, *, lifecycle=None):
    if not isinstance(compilation, Compilation):
        raise ValueError("Compilation required")
    # Recompute rather than trusting a caller-constructed READY DTO.
    checked = compile_strategy(
        parse(compilation.artifact_json), dataset_from_dict(json.loads(compilation.dataset_json))
    )
    if checked != compilation:
        raise ValueError("Compilation identity or semantics mismatch")
    state = Lifecycle(
        lifecycle if lifecycle is not None else json.loads(compilation.artifact_json)["lifecycle"]
    )
    reasons = list(compilation.reasons)
    if state not in {Lifecycle.CANDIDATE, Lifecycle.TESTING}:
        reasons.append("LIFECYCLE_RESEARCH_BLOCKED")
    if confirmation is None:
        reasons.append("PREVIEW_CONFIRMATION_REQUIRED")
    elif not isinstance(confirmation, PreviewConfirmation) or (
        confirmation.compilation_id != compilation.compilation_id
        or confirmation.strategy_id != compilation.strategy_id
        or confirmation.preview_hash != compilation.preview_hash
    ):
        reasons.append("PREVIEW_CONFIRMATION_MISMATCH")
    backtest_json = None
    if reasons:
        status = "BLOCKED"
    else:
        backtest = run(inputs_from_dict(json.loads(compilation.compiled_json)))
        status, backtest_json = backtest.status.value, canonical(backtest.to_dict())
    confirmation_json = (
        canonical(wire(confirmation)) if isinstance(confirmation, PreviewConfirmation) else None
    )
    reasons = tuple(sorted(set(reasons)))
    identity = digest(
        {
            "compilation": compilation.compilation_id,
            "lifecycle": state.value,
            "reasons": reasons,
            "confirmation": confirmation_json,
            "backtest": backtest_json,
        }
    )
    return StrategyRun(
        identity,
        compilation.strategy_id,
        compilation.compilation_id,
        state.value,
        status,
        reasons,
        confirmation_json,
        backtest_json,
    )


def confirmation_from_dict(value):
    if value is None:
        return None
    data = shape(value, PreviewConfirmation)
    data["confirmed_at"] = utc_string(data["confirmed_at"])
    return PreviewConfirmation(**data)


def protocol(artifact, dataset):
    return {"artifact": artifact.value, "dataset_hash": dataset.dataset_hash, "compiler": COMPILER}


def replay(value):
    """Strict self-contained offline compilation and execution parity verification."""
    try:
        object_fields(value, "artifact dataset compilation confirmation lifecycle result")
        artifact = parse(canonical(value["artifact"]))
        dataset = dataset_from_dict(value["dataset"])
        compilation = compile_strategy(artifact, dataset)
        if compilation.to_dict() != value["compilation"]:
            raise ValueError("Strategy compilation replay mismatch")
        result = execute(
            compilation, confirmation_from_dict(value["confirmation"]), lifecycle=value["lifecycle"]
        )
        if result.to_dict() != value["result"]:
            raise ValueError("Strategy runtime replay mismatch")
        return result
    except (TypeError, KeyError, IndexError, OverflowError) as error:
        raise ValueError("Invalid strategy replay structure") from error
