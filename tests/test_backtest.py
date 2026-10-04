import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from test_paper import setup as setup

from vision.analysis.contracts import arithmetic, wire
from vision.core.contracts import BarPayload, CanonicalMarketEvent, EventType, TimestampBasis
from vision.journal.models import Experiment
from vision.journal.replay import replay as journal_replay
from vision.journal.repository import SQLiteRepository, canonical, export
from vision.journal.service import FailureMemory, ResearchJournal
from vision.research.audit import (
    lookahead_audit,
    pbo,
    record_run,
    research_suite,
    source_sensitivity,
    walk_forward,
)
from vision.research.backtest import (
    AcceptancePolicy,
    BacktestInput,
    ResultStatus,
    StrategyDefinition,
    decision,
    inputs_from_dict,
    replay,
    run,
)


@pytest.fixture
def data(request):
    broker, record, rules, _, _ = request.getfixturevalue("setup")
    begin = broker.started_at + timedelta(seconds=58)

    def event(index, values, interval=60, offset=0):
        opening = begin + timedelta(seconds=index * 60 + offset)
        end = opening + timedelta(seconds=interval)
        return CanonicalMarketEvent(
            f"bar-{index}-{offset}-{interval}",
            "binance.spot",
            record.spec.instrument_id,
            EventType.BAR,
            end,
            end,
            index * 60 + offset + 1,
            BarPayload(*(Decimal(str(v)) for v in values), interval, opening),
            continuity="contiguous",
            delivery_kind="backfill",
        )

    def inputs(
        rows=((100, 101, 99, 100, 10), (100, 111, 99, 110, 10), (110, 122, 109, 120, 10)), **kwargs
    ):
        return BacktestInput(
            tuple(event(i, row) for i, row in enumerate(rows)),
            record,
            rules,
            StrategyDefinition(lookback=2, warmup=2),
            broker.costs,
            broker.limits,
            AcceptancePolicy(minimum_trades=1),
            Decimal("10000"),
            "1" * 40,
            **kwargs,
        )

    return inputs, event


def test_next_open_timing_shared_broker_and_exact_profit(data):
    inputs, _ = data
    item = inputs()
    result = run(item)
    assert result.status is ResultStatus.PASS
    values = json.loads(result.result_json)
    assert Decimal(values["net_pnl"]) == 11
    assert values["signals"][1]["action"] == "ENTER"
    checkpoint = values["checkpoint"]
    orders = [e["command"]["order"] for e in checkpoint["journal"] if e["command"]["op"] == "order"]
    assert orders[0]["created_at"] == wire(item.events[2].payload.open_ts)
    assert Decimal(orders[0]["stop_loss"]) == Decimal("104.50")
    assert values["trades"][0]["lineage"]["intent_id"] is None
    assert replay({"input": wire(item), "run": result.to_dict()}) == result
    with pytest.raises(FrozenInstanceError):
        result.status = ResultStatus.FAIL


@pytest.mark.parametrize("case", ["stop", "gap", "ambiguous", "open", "no-trades"])
def test_failure_paths(data, case):
    inputs, _ = data
    first = ((100, 101, 99, 100, 10), (100, 111, 99, 110, 10))
    rows = {
        "stop": first + ((110, 115, 100, 105, 10),),
        "gap": first + ((110, 115, 109, 110, 10), (100, 101, 99, 100, 10)),
        "ambiguous": first + ((110, 122, 100, 120, 10),),
        "open": first + ((110, 115, 109, 110, 10),),
        "no-trades": ((100, 101, 99, 100, 10),) * 3,
    }
    result = run(inputs(rows[case]))
    value = json.loads(result.result_json)
    if case in {"stop", "gap"}:
        assert result.status is ResultStatus.FAIL
        assert value["trades"][0]["state"] == "STOP_HIT"
        assert Decimal(value["net_pnl"]) == (Decimal("-5.5") if case == "stop" else Decimal("-10"))
    else:
        assert result.status is ResultStatus.INCONCLUSIVE
    if case == "ambiguous":
        assert value["ambiguities"] and Decimal(value["net_pnl"]) < 0


@pytest.mark.parametrize(
    "fault",
    [
        "gap",
        "reorder",
        "duplicate",
        "quality",
        "receipt",
        "epoch",
        "sequence",
        "timestamp",
        "source",
        "missing-open",
    ],
)
def test_rulebook_data_fail_closed(data, fault):
    inputs, _ = data
    item = inputs()
    events = list(item.events)
    if fault == "gap":
        events.pop(1)
    elif fault == "reorder":
        events.reverse()
    elif fault == "duplicate":
        events[1] = events[0]
    else:
        changes = {
            "quality": {"continuity": "unverified"},
            "receipt": {"timestamp_basis": TimestampBasis.RECEIPT},
            "epoch": {"source_epoch": 1},
            "sequence": {"sequence": 0},
            "timestamp": {"source_ts": events[1].payload.open_ts},
            "source": {"source": "bybit.spot"},
            "missing-open": {"payload": replace(events[1].payload, open_ts=None)},
        }
        events[1] = replace(events[1], **changes[fault])
    result = run(replace(item, events=tuple(events)))
    assert result.status is ResultStatus.FAIL
    value = json.loads(result.result_json)
    assert value["audits"] and value["checkpoint"] is None and value["net_pnl"] is None


def test_delayed_close_cannot_enter_at_earlier_open(data):
    inputs, _ = data
    item = inputs()
    changed = replace(item.events[1], received_ts=item.events[1].received_ts + timedelta(seconds=1))
    result = run(replace(item, events=(item.events[0], changed, item.events[2])))
    assert result.status is ResultStatus.INCONCLUSIVE
    value = json.loads(result.result_json)
    assert "SIGNAL_NOT_AVAILABLE_AT_NEXT_OPEN" in value["audits"] and not value["trades"]


def test_cost_stress_uses_actual_fill_commissions(data):
    inputs, _ = data
    base = inputs()
    costs = replace(
        base.costs, spread_bps=Decimal(5), slippage_bps=Decimal(4), commission_bps=Decimal(20)
    )
    stressed = run(replace(base, costs=costs))
    result = json.loads(stressed.result_json)
    assert Decimal(result["net_pnl"]) < Decimal(json.loads(run(base).result_json)["net_pnl"])
    assert Decimal(result["trades"][0]["fees"]) > 0
    assert Decimal(result["trades"][0]["slippage_cost"]) > 0


def test_prefix_lookahead_detects_future_callback(data):
    inputs, _ = data
    events = inputs().events
    safe = lookahead_audit(
        events, lambda rows, index: decision(rows[: index + 1], inputs().strategy)
    )
    leaked = lookahead_audit(events, lambda rows, index: rows[-1].payload.close)
    assert safe["status"] == "PASS"
    assert leaked["status"] == "FAIL" and len(leaked["mismatches"]) == 2


def test_recursive_seed_contamination(data):
    inputs, _ = data
    rows = tuple((i + 100, i + 101, i + 99, i + 100, 10) for i in range(12))
    item = inputs(rows)
    result = run(
        replace(
            item, strategy=replace(item.strategy, indicator="EMA", recursive_tolerance=Decimal("0"))
        )
    )
    assert "RECURSIVE_INDICATOR_CONTAMINATION" in json.loads(result.result_json)["audits"]


@pytest.mark.parametrize("stop_first", [True, False])
def test_finer_canonical_bars_resolve_ambiguity_without_interpolation(data, stop_first):
    inputs, event = data
    rows = ((100, 101, 99, 100, 10), (100, 111, 99, 110, 10), (110, 122, 100, 120, 10))
    item = inputs(rows)
    lower = (
        (
            event(2, (110, 115, 100, 105, 5), interval=30),
            event(2, (105, 122, 105, 120, 5), interval=30, offset=30),
        )
        if stop_first
        else (
            event(2, (110, 122, 109, 120, 5), interval=30),
            event(2, (120, 120, 100, 120, 5), interval=30, offset=30),
        )
    )
    result = run(replace(item, lower_events=lower))
    values = json.loads(result.result_json)
    assert not values["ambiguities"]
    assert Decimal(values["net_pnl"]) == (Decimal("-5.5") if stop_first else Decimal(11))
    assert result.status is (ResultStatus.FAIL if stop_first else ResultStatus.PASS)
    broken = (replace(lower[0], payload=replace(lower[0].payload, volume=Decimal(6))), lower[1])
    rejected = run(replace(item, lower_events=broken))
    assert rejected.status is ResultStatus.INCONCLUSIVE
    assert "LOWER_TF_RECONSTRUCTION_INVALID" in json.loads(rejected.result_json)["audits"]


def test_run_rejects_forged_results_and_is_decimal_context_independent(data):
    inputs, _ = data
    item = inputs()
    with localcontext() as context:
        context.prec = 6
        low = run(item)
    with localcontext() as context:
        context.prec = 50
        high = run(item)
    assert low == high
    envelope = {"input": wire(item), "run": high.to_dict()}
    envelope["run"]["result"]["net_pnl"] = "9999"
    with pytest.raises(ValueError, match="mismatch"):
        replay(envelope)


def test_immutable_result_journal_and_failure_memory(data, tmp_path):
    inputs, _ = data
    item = inputs(((100, 101, 99, 100, 10),) * 3)
    at = item.events[-1].received_ts
    repository = SQLiteRepository(tmp_path / "backtests.sqlite")
    journal = ResearchJournal(repository, clock=lambda: at)
    metadata = Experiment(
        "backtest",
        "family",
        canonical(item.config()),
        item.commit_sha,
        (item.record.revision,),
        (("binance.spot", 0),),
        None,
        item.costs,
        at,
    )
    journal.open_experiment(metadata)
    entry = record_run(journal, "backtest", item)
    assert record_run(journal, "backtest", item) == entry
    assert FailureMemory(journal).records()[0].payload["failure"]["status"] == "PARKED"
    assert journal.prospective_snapshot(at) == ()
    second = journal.open_experiment(replace(metadata, experiment_id="replica"))
    assert "DUPLICATE_EXPERIMENT" in second.payload["warnings"]
    assert "FAMILY_PSEUDO_REPLICATION" in second.payload["warnings"]
    portable = export(repository)
    repository.close()
    reopened = SQLiteRepository(tmp_path / "backtests.sqlite", readonly=True)
    assert export(reopened) == portable
    assert journal_replay(portable)["backtest_runs"] == [entry.payload["run"]]
    reopened.close()


def test_walk_forward_has_disjoint_frozen_tests_and_untouched_holdout(data):
    inputs, _ = data
    item = inputs(((100, 101, 99, 100, 10),) * 20)
    report = walk_forward(item, train=4, test=4, holdout=4)
    assert report["status"] == "INCONCLUSIVE"
    ranges = [f["test_range"] for f in report["folds"]]
    assert ranges == [[4, 8], [8, 12], [12, 16]]
    assert report["holdout_range"] == [16, 20]
    assert report["oos"]["result"]["trades"] == []
    assert report["selection"].startswith("NONE")
    assert walk_forward(item, train=20, test=4, holdout=4)["oos"] is None


def test_suite_never_optimizes_or_promotes(data):
    inputs, _ = data
    item = inputs(((100, 101, 99, 100, 10),) * 20)
    report = research_suite(
        item,
        costs=(item.costs, replace(item.costs, slippage_bps=Decimal(20))),
        strategies=(item.strategy, replace(item.strategy, threshold=Decimal("0.02"))),
        train=4,
        test=4,
        holdout=4,
    )
    assert report["promotion"] == "NONE" and report["status"] == "INCONCLUSIVE"
    assert len(report["cost_stress"]) == len(report["parameter_sensitivity"]) == 2
    assert report["source_sensitivity"]["reason"] == "NO_ALTERNATIVE_SOURCE"
    assert report["pbo"]["reason"] == "TRIAL_UNIVERSE_UNDECLARED"


def test_source_sensitivity_requires_actual_distinct_aligned_provider(data):
    inputs, _ = data
    item = inputs()
    with pytest.raises(ValueError):
        source_sensitivity(item, (item,))


@pytest.mark.parametrize("complete", [False, True])
def test_cscv_mean_return_pbo_diagnostic(data, complete):
    matrix = tuple((Decimal(i % 4), Decimal(3 - i % 4)) for i in range(8))
    result = pbo(matrix, complete_candidate_set=complete)
    assert result["status"] == "INCONCLUSIVE"
    assert result["pbo"] is None
    assert result["reason"] == ("DEGENERATE_IS_TIES" if complete else "TRIAL_UNIVERSE_UNDECLARED")


def test_cscv_uses_complementary_blocks_and_exact_expected_rank():
    matrix = tuple(
        (Decimal(10), Decimal(0)) if i < 4 else (Decimal(0), Decimal(3)) for i in range(8)
    )
    result = pbo(matrix, complete_candidate_set=True)
    assert result["status"] == "PASS" and result["splits"] == 6
    with arithmetic():
        assert Decimal(result["pbo"]) == Decimal(1) / Decimal(3)
    assert all(Decimal(v) in {Decimal("0.5"), Decimal(1)} for v in result["oos_rank_percentiles"])


@pytest.mark.parametrize(
    "change",
    [
        {"lookback": 0},
        {"warmup": 1},
        {"indicator": "CENTERED"},
        {"stop_fraction": Decimal(0)},
        {"quantity": Decimal("NaN")},
        {"version": "unknown"},
    ],
)
def test_invalid_strategy_fail_closed(change):
    with pytest.raises(ValueError):
        StrategyDefinition(**change)


def test_strict_replay_shapes(data):
    inputs, _ = data
    value = wire(inputs())
    assert inputs_from_dict(value) == inputs()
    value["unexpected"] = True
    with pytest.raises(ValueError):
        inputs_from_dict(value)


@pytest.mark.parametrize("fault", ["alignment", "future-spec"])
def test_timestamp_and_spec_lookahead_audits(data, fault):
    inputs, _ = data
    item = inputs()
    if fault == "alignment":
        item = replace(
            item,
            events=tuple(
                replace(
                    e,
                    payload=replace(e.payload, open_ts=e.payload.open_ts + timedelta(seconds=1)),
                    source_ts=e.source_ts + timedelta(seconds=1),
                    received_ts=e.received_ts + timedelta(seconds=1),
                )
                for e in item.events
            ),
        )
    else:
        rec = replace(
            item.record,
            provenance=replace(item.record.provenance, observed_at=item.events[-1].received_ts),
        )
        item = replace(item, record=rec, rules=replace(item.rules, spec_revision=rec.revision))
    assert run(item).status is ResultStatus.FAIL


def alternative_source(item):
    spec = replace(item.record.spec, instrument_id="BYBIT:SPOT:BTCUSDT", venue="BYBIT_SPOT")
    rec = replace(
        item.record, spec=spec, provenance=replace(item.record.provenance, source="bybit.spot")
    )
    return replace(
        item,
        record=rec,
        rules=replace(item.rules, instrument_id=spec.instrument_id, spec_revision=rec.revision),
        events=tuple(
            replace(
                e,
                source="bybit.spot",
                instrument_id=spec.instrument_id,
                event_id="bybit-" + e.event_id,
            )
            for e in item.events
        ),
        regimes=(),
    )


def test_aligned_source_sensitivity_and_regime_attribution(data):
    inputs, _ = data
    item = inputs()
    report = source_sensitivity(item, (alternative_source(item),))
    assert report["status"] == "PASS" and report["net_pnl_range"] == ["11.00", "11.00"]
    attributed = run(replace(item, regimes=((item.events[1].event_id, "synthetic-up"),)))
    assert Decimal(json.loads(attributed.result_json)["regime_pnl"]["synthetic-up"]) == 11


def test_full_suite_durable_replay_and_semantic_tamper(data, tmp_path):
    from vision.research.audit import SuiteDefinition, record_suite, suite_from_dict

    inputs, _ = data
    item = inputs(((100, 101, 99, 100, 10),) * 12)
    alt = alternative_source(item)
    definition = SuiteDefinition(
        item,
        (item.costs, replace(item.costs, slippage_bps=Decimal(20))),
        (item.strategy, replace(item.strategy, threshold=Decimal("0.02"))),
        (alt,),
        4,
        4,
        4,
        True,
    )
    assert suite_from_dict(wire(definition)) == definition
    repository = SQLiteRepository(tmp_path / "suite.sqlite")
    at = item.events[-1].received_ts
    journal = ResearchJournal(repository, clock=lambda: at)
    journal.open_experiment(
        Experiment(
            "suite",
            "family",
            canonical(definition.config()),
            item.commit_sha,
            (item.record.revision, alt.record.revision),
            (("binance.spot", 0), ("bybit.spot", 0)),
            None,
            item.costs,
            at,
        )
    )
    entry = record_suite(journal, "suite", definition)
    assert entry.payload["report"]["promotion"] == "NONE"
    assert journal_replay(export(repository))["backtest_suites"] == [entry.payload["report"]]
    assert FailureMemory(journal).records()[0].payload["failure"]["status"] == "PARKED"
    assert journal.prospective_snapshot(at) == ()
    # Changing an outcome and recomputing its chain is insufficient: replay reruns the suite.
    from vision.core.instruments import digest
    from vision.journal.repository import GENESIS

    forged, previous = export(repository), GENESIS
    for row in forged["entries"]:
        if row["kind"] == "backtest_suite":
            payload = json.loads(row["payload_json"])
            payload["report"]["status"] = "PASS"
            row["payload_json"] = canonical(payload)
        row["previous_hash"] = previous
        body = {k: v for k, v in row.items() if k != "entry_hash"}
        row["entry_hash"] = previous = digest(body)
    forged["head"] = previous
    with pytest.raises(ValueError, match="semantic"):
        journal_replay(forged)
    repository.close()


@pytest.mark.parametrize("command", ["backtest-run", "backtest-replay"])
def test_cli_guard_before_io(data, tmp_path, command):
    from test_journal import cli

    missing, output = tmp_path / "missing", tmp_path / "output"
    args = (missing, output) if command == "backtest-run" else (missing,)
    assert cli(command, *args, enabled=True).returncode == 2
    assert not output.exists()


def test_backtest_cli_roundtrip_and_no_overwrite(data, tmp_path):
    from test_journal import cli

    inputs, _ = data
    source, output = tmp_path / "input.json", tmp_path / "run.json"
    source.write_text(canonical(wire(inputs())), encoding="utf-8")
    result = cli("backtest-run", source, output)
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "PASS"
    before = output.read_bytes()
    replayed = cli("backtest-replay", output)
    assert replayed.returncode == 0 and replayed.stdout == result.stdout
    assert cli("backtest-run", source, output).returncode == 2
    assert output.read_bytes() == before
    output.write_text("null", encoding="utf-8")
    rejected = cli("backtest-replay", output)
    assert rejected.returncode == 2 and "Traceback" not in rejected.stderr


def test_future_price_changes_cannot_change_prior_signals_or_entry_fill(data):
    inputs, _ = data
    item = inputs()
    future = replace(
        item.events[-1],
        payload=replace(item.events[-1].payload, high=Decimal(150), close=Decimal(140)),
    )
    baseline = json.loads(run(item).result_json)
    altered = json.loads(run(replace(item, events=(*item.events[:-1], future))).result_json)
    assert baseline["signals"][:2] == altered["signals"][:2]
    for result in (baseline, altered):
        entries = [e for e in result["checkpoint"]["journal"] if e["command"]["op"] == "order"]
        assert entries[0]["command"]["order"]["created_at"] == wire(item.events[-1].payload.open_ts)
    assert baseline["trades"][0]["lineage"]["fill_id"] == altered["trades"][0]["lineage"]["fill_id"]


def test_synthetic_portable_run_and_suite_stay_inconclusive():
    from pathlib import Path

    root = Path(__file__).parent / "fixtures/backtest"
    result = replay(json.loads((root / "ambiguous_run.json").read_text(encoding="utf-8")))
    assert result.status is ResultStatus.INCONCLUSIVE
    report = journal_replay(
        json.loads((root / "research_journal.json").read_text(encoding="utf-8"))
    )
    assert report["backtest_suites"][0]["status"] == "INCONCLUSIVE"
    assert report["directional_outcome_ids"] == report["prospective_registration_ids"] == []


@pytest.mark.parametrize(
    "malformed",
    [
        (),
        ((Decimal(0),),) * 8,
        ((Decimal("NaN"), Decimal(1)),) * 8,
    ],
)
def test_pbo_does_not_fabricate_from_invalid_matrices(malformed):
    with pytest.raises(ValueError):
        pbo(malformed, complete_candidate_set=True)


def test_risk_step_failures_never_bypass_shared_governor(data):
    inputs, _ = data
    item = inputs()
    result = run(replace(item, strategy=replace(item.strategy, quantity=Decimal("0.0001"))))
    values = json.loads(result.result_json)
    assert result.status is ResultStatus.INCONCLUSIVE and not values["trades"]
    assert "RISK_REJECTED_ENTRY" in values["audits"]
