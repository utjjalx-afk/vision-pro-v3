"""Strict JSON strategy artifacts. No eval, arbitrary plugins, code or inferred defaults."""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from vision.analysis.contracts import number
from vision.core.instruments import digest
from vision.execution.paper.models import PaperCosts, PaperMode
from vision.journal.repository import canonical
from vision.research.backtest import AcceptancePolicy
from vision.risk.governor import RiskLimits


class Lifecycle(StrEnum):
    CANDIDATE = "CANDIDATE"
    TESTING = "TESTING"
    VERIFIED = "VERIFIED"
    PAPER = "PAPER"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"
    PARKED = "PARKED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


def object_fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("Exact object fields required: " + fields)
    return value


def text(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip():
        raise ValueError("Bounded explicit text required")
    if any(ord(c) < 32 for c in value):
        raise ValueError("Control characters are forbidden")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Bounded explicit integer required")
    return value


def decimal(value):
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value)
    ):
        raise ValueError("Exact decimal string required")
    result = Decimal(value)
    number(result)
    return result


def strings(value, *, minimum=1, maximum=8):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("Explicit bounded array required")
    for item in value:
        text(item)
    if len(set(value)) != len(value):
        raise ValueError("Array entries must be unique")


def cost_model(value):
    object_fields(value, "mode spread_bps slippage_bps commission_bps")
    return PaperCosts(
        PaperMode(value["mode"]),
        *(decimal(value[k]) for k in ("spread_bps", "slippage_bps", "commission_bps")),
    )


def risk_limits(value):
    object_fields(
        value,
        "per_trade_fraction portfolio_fraction daily_equity_loss_fraction "
        "drawdown_fraction symbol_exposure_fraction gross_exposure_fraction",
    )
    return RiskLimits(**{k: decimal(v) for k, v in value.items()})


def acceptance(value):
    object_fields(value, "minimum_trades minimum_net_pnl maximum_drawdown_fraction maximum_pbo")
    integer(value["minimum_trades"], 1, 500)
    return AcceptancePolicy(
        value["minimum_trades"],
        decimal(value["minimum_net_pnl"]),
        decimal(value["maximum_drawdown_fraction"]),
        decimal(value["maximum_pbo"]),
    )


def validate(value):
    object_fields(
        value,
        "schema_version strategy_key strategy_version origin lifecycle market "
        "features entry exit risk execution acceptance research",
    )
    if value["schema_version"] != "vision.strategy.v1":
        raise ValueError("Unsupported DSL schema version")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", text(value["strategy_key"])):
        raise ValueError("Explicit strategy key required")
    if not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", text(value["strategy_version"])
    ):
        raise ValueError("Explicit three-part strategy version required")
    Lifecycle(value["lifecycle"])
    origin = object_fields(value["origin"], "kind generator_id")
    if origin["kind"] not in {"STRUCTURED", "GENERATED_DRAFT"}:
        raise ValueError("Explicit origin designation required")
    if origin["kind"] == "STRUCTURED":
        if origin["generator_id"] is not None:
            raise ValueError("Structured inputs cannot claim a generator")
    else:
        text(origin["generator_id"])
    market = object_fields(
        value["market"],
        "canonical_symbol asset_class timeframe_seconds warmup required_data instrument",
    )
    text(market["asset_class"])
    if not re.fullmatch(
        r"(crypto|forex|metals):[A-Z0-9]+/[A-Z0-9]+", text(market["canonical_symbol"])
    ):
        raise ValueError("Canonical market symbol required")
    if market["asset_class"] != market["canonical_symbol"].split(":")[0]:
        raise ValueError("Market asset class mismatch")
    integer(market["timeframe_seconds"], 1, 86400)
    integer(market["warmup"], 1, 256)
    strings(market["required_data"])
    instrument = object_fields(market["instrument"], "venues quantity_unit valuation_model")
    strings(instrument["venues"])
    text(instrument["quantity_unit"])
    text(instrument["valuation_model"])
    features = value["features"]
    if not isinstance(features, list) or not 1 <= len(features) <= 8:
        raise ValueError("Explicit bounded features required")
    for feature in features:
        object_fields(feature, "id kind input lookback warmup recursive_tolerance")
        for k in ("id", "kind", "input"):
            text(feature[k])
        integer(feature["lookback"], 1, 256)
        integer(feature["warmup"], 1, 256)
        if decimal(feature["recursive_tolerance"]) < 0:
            raise ValueError("Negative recursive tolerance")
    if len({f["id"] for f in features}) != len(features):
        raise ValueError("Feature IDs must be unique")
    for name in ("entry", "exit"):
        rule = object_fields(value[name], "action condition")
        text(rule["action"])
        condition = object_fields(rule["condition"], "left operator right_feature multiplier")
        for k in ("left", "operator", "right_feature"):
            text(condition[k])
        decimal(condition["multiplier"])
        if condition["right_feature"] not in {f["id"] for f in features}:
            raise ValueError("Rule references an absent feature")
    risk = object_fields(value["risk"], "quantity hard_limits invalidation profit_exit")
    quantity = object_fields(risk["quantity"], "value unit sizing")
    if decimal(quantity["value"]) <= 0:
        raise ValueError("Positive quantity required")
    text(quantity["unit"])
    text(quantity["sizing"])
    risk_limits(risk["hard_limits"])
    stop = object_fields(
        risk["invalidation"], "kind anchor fraction trigger fill gap_policy no_stop"
    )
    target = object_fields(risk["profit_exit"], "kind anchor fraction trigger fill")
    for rule in (stop, target):
        for k, v in rule.items():
            if k == "fraction":
                if decimal(v) <= 0:
                    raise ValueError("Positive explicit exit fraction required")
            else:
                text(v)
    execution = object_fields(
        value["execution"], "mode timing ambiguity liquidity costs capabilities"
    )
    for k in ("mode", "timing", "ambiguity", "liquidity"):
        text(execution[k])
    strings(execution["capabilities"])
    cost_model(execution["costs"])
    acceptance(value["acceptance"])
    research = object_fields(value["research"], "starting_cash commit_sha")
    if decimal(research["starting_cash"]) <= 0:
        raise ValueError("Positive research cash required")
    if not re.fullmatch("[0-9a-f]{40}", text(research["commit_sha"])):
        raise ValueError("Explicit code revision required")
    return value


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    """Validated artifact, distinct from the Phase-8 compiled rule DTO."""

    artifact_json: str

    def __post_init__(self):
        value = strict_json(self.artifact_json)
        validate(value)
        if canonical(value) != self.artifact_json:
            raise ValueError("Canonical immutable artifact required")

    @property
    def value(self):
        return json.loads(self.artifact_json)

    @property
    def config_hash(self):
        return digest(self.value)

    @property
    def strategy_id(self):
        value = self.value
        return digest(
            {
                "key": value["strategy_key"],
                "version": value["strategy_version"],
                "config": self.config_hash,
                "schema": value["schema_version"],
            }
        )


def strict_json(raw, *, limit=100000):
    if type(limit) is not int or not 1 <= limit <= 50000000:
        raise ValueError("Bounded JSON read policy required")
    if not isinstance(raw, str) or len(raw.encode()) > limit:
        raise ValueError("JSON input exceeds its size limit")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("Non-finite JSON constant")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (RecursionError, UnicodeError) as error:
        raise ValueError("Invalid bounded JSON encoding/depth") from error


def parse(raw):
    value = strict_json(raw)
    validate(value)
    return StrategyDefinition(canonical(value))


@dataclass(frozen=True, slots=True)
class RulePreview:
    strategy_id: str
    config_hash: str
    text: str
    preview_hash: str


def preview(artifact):
    if not isinstance(artifact, StrategyDefinition):
        raise ValueError("Validated StrategyDefinition required")
    v = artifact.value
    lines = [
        f"Strategy {v['strategy_key']} v{v['strategy_version']}",
        f"Declared lifecycle: {v['lifecycle']} (no deployment permission)",
        f"Market: {v['market']['canonical_symbol']}; "
        f"timeframe={v['market']['timeframe_seconds']}s; warmup={v['market']['warmup']}",
        f"Origin: {v['origin']['kind']}",
        "PHASE 9: explicit offline backtest research only",
    ]
    for name in ("entry", "exit"):
        c = v[name]["condition"]
        lines.append(
            f"{name.upper()}: {v[name]['action']} when "
            f"{c['left']} {c['operator']} {c['right_feature']} * {c['multiplier']}"
        )
    lines += ["All exact parameters and constraints:", json.dumps(v, sort_keys=True, indent=2)]
    text_value = "\n".join(lines)
    return RulePreview(
        artifact.strategy_id,
        artifact.config_hash,
        text_value,
        digest({"strategy": artifact.strategy_id, "preview": text_value, "compiler": "phase9-v1"}),
    )
