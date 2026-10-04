"""Failure-oriented diagnostics with fixed candidates; no parameter search or promotion."""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from itertools import combinations

from vision.analysis.contracts import arithmetic, wire
from vision.core.instruments import digest
from vision.execution.paper.models import PaperCosts
from vision.research.backtest import BacktestInput, ResultStatus, StrategyDefinition, decision, run


def lookahead_audit(events, evaluator):
    """Evaluator(full, index) must equal evaluator(prefix, index) at every close."""
    if type(events) is not tuple or not 1 <= len(events) <= 500:
        raise ValueError("Bounded immutable dataset required")
    mismatches = []
    for index in range(len(events)):
        if evaluator(events, index) != evaluator(events[: index + 1], index):
            mismatches.append(events[index].event_id)
    return {
        "status": "FAIL" if mismatches else "PASS",
        "mismatches": mismatches,
        "scope": "prefix invariance only; not arbitrary-code or external-data proof",
    }


def source_sensitivity(base, alternatives):
    if type(alternatives) is not tuple or len(alternatives) > 8:
        raise ValueError("Bounded predetermined source alternatives required")
    if not alternatives:
        return {"status": "INCONCLUSIVE", "reason": "NO_ALTERNATIVE_SOURCE", "runs": []}
    baseline = run(base)
    runs = []
    for variant in alternatives:
        if not isinstance(variant, BacktestInput):
            raise ValueError("Typed source variant required")
        if (
            variant.strategy != base.strategy
            or variant.costs != base.costs
            or variant.limits != base.limits
            or variant.acceptance != base.acceptance
            or variant.starting_cash != base.starting_cash
            or variant.commit_sha != base.commit_sha
            or variant.record.canonical != base.record.canonical
            or variant.evaluation_start != base.evaluation_start
            or [e.payload.open_ts for e in variant.events]
            != [e.payload.open_ts for e in base.events]
            or {e.source for e in variant.events} == {e.source for e in base.events}
        ):
            raise ValueError("Source sensitivity must isolate aligned source/economics")
        runs.append(run(variant))
    statuses = [baseline.status] + [r.status for r in runs]
    pnl_values = [json_result(r)["net_pnl"] for r in [baseline] + runs]
    status = (
        "INCONCLUSIVE"
        if ResultStatus.INCONCLUSIVE in statuses
        else "FAIL"
        if ResultStatus.FAIL in statuses
        else "PASS"
    )
    return {
        "status": status,
        "baseline": baseline.run_id,
        "runs": [r.to_dict() for r in runs],
        "net_pnl_range": None
        if any(p is None for p in pnl_values)
        else [
            min(Decimal(p) for p in pnl_values).to_eng_string(),
            max(Decimal(p) for p in pnl_values).to_eng_string(),
        ],
    }


def walk_forward(base, *, train, test, holdout):
    """Frozen strategy, rolling training warmup, disjoint tests and final untouched tail."""
    for value in (train, test, holdout):
        if type(value) is not int or value <= 0:
            raise ValueError("Explicit positive fold sizes required")
    if train < base.strategy.warmup or holdout < 2 or train + test + holdout > len(base.events):
        return {
            "status": "INCONCLUSIVE",
            "reason": "INSUFFICIENT_WALK_FORWARD_DATA",
            "folds": [],
            "oos": None,
        }
    folds, boundary = [], len(base.events) - holdout

    def evaluate(start, end, evaluation_start):
        subset = base.events[start:end]
        ids = {e.event_id for e in subset}
        lower = tuple(
            e
            for e in base.lower_events
            if subset[0].payload.open_ts
            <= e.payload.open_ts
            < subset[-1].payload.open_ts + timedelta(seconds=subset[-1].payload.interval_seconds)
        )
        return run(
            replace(
                base,
                events=subset,
                lower_events=lower,
                regimes=tuple(p for p in base.regimes if p[0] in ids),
                evaluation_start=evaluation_start,
            )
        )

    for begin in range(train, boundary, test):
        end = min(begin + test, boundary)
        r = evaluate(begin - train, end, train)
        folds.append(
            {"train_range": [begin - train, begin], "test_range": [begin, end], "run": r.to_dict()}
        )
    oos = evaluate(max(0, boundary - train), len(base.events), min(train, boundary))
    statuses = [f["run"]["status"] for f in folds] + [oos.status.value]
    status = (
        "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    )
    return {
        "status": status,
        "folds": folds,
        "oos": oos.to_dict(),
        "holdout_range": [boundary, len(base.events)],
        "selection": "NONE; frozen rule, no training optimization",
    }


def pbo(matrix, *, blocks=4, complete_candidate_set=False):
    """CSCV diagnostic using declared aligned per-period net returns and mean ranking."""
    if (
        type(matrix) is not tuple
        or not 8 <= len(matrix) <= 500
        or type(blocks) is not int
        or blocks not in {4, 6, 8, 10}
        or len(matrix) % blocks
        or type(complete_candidate_set) is not bool
    ):
        raise ValueError("Bounded aligned return matrix and even equal blocks required")
    n = len(matrix[0])
    if (
        not 2 <= n <= 16
        or any(type(row) is not tuple or len(row) != n for row in matrix)
        or any(not isinstance(x, Decimal) or not x.is_finite() for row in matrix for x in row)
    ):
        raise ValueError("Finite Decimal return matrix for multiple candidates required")
    for row in matrix:
        for value in row:
            if len(value.as_tuple().digits) > 256 or abs(value.as_tuple().exponent) > 1000:
                raise ValueError("Bounded calculated return required")
    if not complete_candidate_set:
        return {"status": "INCONCLUSIVE", "pbo": None, "reason": "TRIAL_UNIVERSE_UNDECLARED"}
    ranks, ties, step = [], False, len(matrix) // blocks
    with arithmetic():
        for selected in combinations(range(blocks), blocks // 2):
            train = [row for i, row in enumerate(matrix) if i // step in selected]
            test = [row for i, row in enumerate(matrix) if i // step not in selected]
            means = [sum(row[j] for row in train) / len(train) for j in range(n)]
            winners = [j for j in range(n) if means[j] == max(means)]
            if len(winners) != 1:
                ties = True
                continue
            oos = [sum(row[j] for row in test) / len(test) for j in range(n)]
            winner = oos[winners[0]]
            # Midrank / N; lower percentile denotes poor out-of-sample ranking.
            rank = (
                sum(x < winner for x in oos) + (Decimal(sum(x == winner for x in oos)) + 1) / 2
            ) / n
            ranks.append(rank)
        if ties or not ranks:
            return {"status": "INCONCLUSIVE", "pbo": None, "reason": "DEGENERATE_IS_TIES"}
        probability = sum(r <= Decimal("0.5") for r in ranks) / Decimal(len(ranks))
    return {
        "status": "PASS",
        "pbo": str(probability),
        "splits": len(ranks),
        "oos_rank_percentiles": [str(r) for r in ranks],
        "metric": "mean aligned net return, not annualized Sharpe",
        "matrix_hash": digest(wire(matrix)),
        "scope": "diagnostic only; not statistical verification or live eligibility",
    }


def research_suite(
    base,
    *,
    costs,
    strategies,
    source_alternatives=(),
    train=20,
    test=10,
    holdout=10,
    complete_candidate_set=False,
):
    """Run only a bounded preregistered matrix; never select winners or retune."""
    if (
        type(costs) is not tuple
        or not 2 <= len(costs) <= 8
        or type(strategies) is not tuple
        or not 2 <= len(strategies) <= 8
        or base.costs not in costs
        or base.strategy not in strategies
        or any(not isinstance(c, PaperCosts) for c in costs)
        or any(not isinstance(s, StrategyDefinition) for s in strategies)
        or len(set(costs)) != len(costs)
        or len(set(strategies)) != len(strategies)
    ):
        raise ValueError("Predetermined cost and parameter matrices must include the baseline")
    if any(
        c.commission_bps < base.costs.commission_bps
        or c.slippage_bps < base.costs.slippage_bps
        or c.spread_bps < base.costs.spread_bps
        for c in costs
    ):
        raise ValueError("Cost stress cannot reduce baseline costs")
    baseline = run(base)
    stress = [run(replace(base, costs=c)) for c in costs]
    sensitivity = [run(replace(base, strategy=s)) for s in strategies]
    lookahead = lookahead_audit(
        base.events, lambda rows, index: decision(rows[: index + 1], base.strategy)
    )
    forward = walk_forward(base, train=train, test=test, holdout=holdout)
    sources = source_sensitivity(base, source_alternatives)
    overfit = {"status": "INCONCLUSIVE", "reason": "ALIGNED_TRIAL_RETURN_MATRIX_REQUIRED"}
    results = [json_result(r) for r in sensitivity]
    count = len(base.events) - base.evaluation_start
    if (
        count >= 8
        and count % 4 == 0
        and all(
            not r["audits"] and not r["ambiguities"] and len(r["equity"]) == count for r in results
        )
    ):
        with arithmetic():
            columns = []
            for r in results:
                previous, returns = base.starting_cash, []
                for point in r["equity"]:
                    current = Decimal(point["equity"])
                    returns.append((current - previous) / base.starting_cash)
                    previous = current
                columns.append(returns)
            matrix = tuple(tuple(column[i] for column in columns) for i in range(count))
        overfit = pbo(matrix, complete_candidate_set=complete_candidate_set)
        if overfit.get("pbo") is not None and Decimal(overfit["pbo"]) > base.acceptance.maximum_pbo:
            overfit = {**overfit, "status": "FAIL", "reason": "PBO_EXCEEDS_FROZEN_LIMIT"}
    statuses = [r.status.value for r in [baseline] + stress + sensitivity]
    statuses += [lookahead["status"], forward["status"], sources["status"], overfit["status"]]
    status = (
        "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    )
    return {
        "status": status,
        "baseline": baseline.to_dict(),
        "cost_stress": [r.to_dict() for r in stress],
        "parameter_sensitivity": [r.to_dict() for r in sensitivity],
        "lookahead": lookahead,
        "walk_forward": forward,
        "source_sensitivity": sources,
        "pbo": overfit,
        "promotion": "NONE",
    }


def json_result(run_record):
    import json

    return json.loads(run_record.result_json)


@dataclass(frozen=True, slots=True)
class SuiteDefinition:
    base: BacktestInput
    costs: tuple
    strategies: tuple
    source_alternatives: tuple = ()
    train: int = 20
    test: int = 10
    holdout: int = 10
    complete_candidate_set: bool = False

    def __post_init__(self):
        if not isinstance(self.base, BacktestInput):
            raise ValueError("Typed base backtest required")
        if any(
            type(t) is not tuple for t in (self.costs, self.strategies, self.source_alternatives)
        ):
            raise ValueError("Immutable preregistered matrices required")
        if type(self.complete_candidate_set) is not bool:
            raise ValueError("Explicit trial-universe declaration required")

    def config(self):
        return {
            "base": self.base.config(),
            "costs": wire(self.costs),
            "strategies": wire(self.strategies),
            "sources": [v.config() for v in self.source_alternatives],
            "train": self.train,
            "test": self.test,
            "holdout": self.holdout,
            "complete_candidate_set": self.complete_candidate_set,
        }

    def evaluate(self):
        return research_suite(
            self.base,
            costs=self.costs,
            strategies=self.strategies,
            source_alternatives=self.source_alternatives,
            train=self.train,
            test=self.test,
            holdout=self.holdout,
            complete_candidate_set=self.complete_candidate_set,
        )


def suite_from_dict(value):
    from vision.core.state.replay import shape
    from vision.research.backtest import inputs_from_dict

    data = shape(value, SuiteDefinition)
    base = inputs_from_dict(data["base"])
    for name in ("costs", "strategies", "source_alternatives"):
        if not isinstance(data[name], list):
            raise ValueError("Explicit suite arrays required")
    data["base"] = base
    # Reuse strict economics/config decoders, without running or selecting candidates.
    data["costs"] = tuple(inputs_from_dict({**wire(base), "costs": c}).costs for c in data["costs"])
    data["strategies"] = tuple(
        inputs_from_dict({**wire(base), "strategy": s}).strategy for s in data["strategies"]
    )
    data["source_alternatives"] = tuple(inputs_from_dict(s) for s in data["source_alternatives"])
    return SuiteDefinition(**data)


def record_suite(journal, experiment_id, definition):
    from vision.failure_memory.models import FailureRecord, FailureStatus, ValidationStage
    from vision.journal.service import FailureMemory

    entry = journal.backtest_suite(experiment_id, definition)
    status = entry.payload["report"]["status"]
    if status != "PASS":
        FailureMemory(journal).record(
            experiment_id,
            FailureRecord(
                entry.key,
                FailureStatus.REJECTED if status == "FAIL" else FailureStatus.PARKED,
                ValidationStage.VALIDATION,
                "Backtest suite acceptance: " + status,
                (entry.entry_id,),
            ),
        )
    return entry


def record_run(journal, experiment_id, inputs):
    from vision.failure_memory.models import FailureRecord, FailureStatus, ValidationStage
    from vision.journal.service import FailureMemory

    entry = journal.backtest(experiment_id, inputs)
    status = entry.payload["run"]["status"]
    if status != "PASS":
        FailureMemory(journal).record(
            experiment_id,
            FailureRecord(
                entry.key,
                FailureStatus.REJECTED if status == "FAIL" else FailureStatus.PARKED,
                ValidationStage.VALIDATION,
                "Backtest acceptance: " + status,
                (entry.entry_id,),
            ),
        )
    return entry
