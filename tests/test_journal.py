import json
import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from test_paper import setup as setup
from test_synthesis import factory as factory

from vision.analysis.contracts import Lane, wire
from vision.core.instruments import digest
from vision.execution.paper.models import PaperCosts
from vision.failure_memory.models import FailureRecord, FailureStatus, ValidationStage
from vision.journal.models import ExitDeclaration, Experiment, OutcomeState
from vision.journal.replay import replay
from vision.journal.repository import Pending, SQLiteRepository, canonical, export
from vision.journal.service import FailureMemory, ResearchJournal


@pytest.fixture
def paper_setup(request):
    return request.getfixturevalue("setup")


@pytest.fixture
def synthesis_factory(request):
    return request.getfixturevalue("factory")


@pytest.fixture
def journal_setup(tmp_path, now, paper_setup):
    broker, record, _, quote, order = paper_setup
    clock = [now]
    path = tmp_path / "research.sqlite"
    repository = SQLiteRepository(path)
    journal = ResearchJournal(repository, clock=lambda: clock[0])
    metadata = Experiment(
        "exp",
        "family",
        canonical({"strategy": "synthetic", "version": 1}),
        "1" * 40,
        (record.revision,),
        (("binance.spot", 0),),
        None,
        broker.costs,
        now,
    )
    journal.open_experiment(metadata)
    yield journal, repository, path, clock, broker, record, quote, order, metadata
    repository.close()


def complete(setup, *, price="110", state=None):
    journal, _, _, clock, broker, _, quote, order, _ = setup
    entry = broker.submit(order())
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    declaration = None
    if state is not None:
        plan = ExitDeclaration(
            entry.fill.position_id,
            "close",
            state,
            "synthetic explicit exit",
            ("quote-entry",),
            Decimal("109") if state is OutcomeState.TARGET_HIT else None,
            clock[0] + timedelta(seconds=1) if state is OutcomeState.EXPIRED else None,
        )
        declaration = journal.declare_exit("exp", receipt.entry_id, plan)
    clock[0] += timedelta(seconds=1)
    quote(price, 2, at=clock[0])
    broker.close(entry.fill.position_id, "close", clock[0])
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    result = journal.finalize(
        "exp", receipt.entry_id, declaration_entry_id=declaration.entry_id if declaration else None
    )
    return entry, receipt, result


def test_durable_paper_outcome_roundtrip_actual_values(journal_setup):
    _, repository, _, _, broker, _, _, _, _ = journal_setup
    entry, _, result = complete(journal_setup)
    g = result.payload["outcome"]
    assert g["state"] == "MANUAL_EXIT" and Decimal(g["realized_pnl"]) == broker.realized == 9
    assert Decimal(g["fees"]) == 0 and Decimal(g["slippage_cost"]) == 0
    with localcontext() as c:
        c.prec = 256
        assert Decimal(g["r_multiple"]) == Decimal(9) / Decimal(11)
    assert g["lineage"]["fill_id"] == entry.fill.fill_id
    assert g["lineage"]["risk_decision_id"] == entry.decision.risk_decision_id
    assert g["lineage"]["outcome_id"] == g["outcome_id"]
    assert g["lineage"]["assessment_ids"] == [] and g["lineage"]["intent_id"] is None
    assert replay(export(repository))["trade_outcomes"] == [g]


@pytest.mark.parametrize(
    "state",
    [
        OutcomeState.TARGET_HIT,
        OutcomeState.EXPIRED,
        OutcomeState.SYSTEM_EXIT,
        OutcomeState.MANUAL_EXIT,
    ],
)
def test_exit_declaration_classifies_actual_close(journal_setup, state):
    _, _, result = complete(journal_setup, state=state)
    assert result.payload["outcome"]["state"] == state.value
    assert replay(export(journal_setup[1]))["trade_outcomes"][0]["state"] == state.value


def test_gap_stop_actual_broker_records(journal_setup):
    journal, repository, _, clock, broker, _, quote, order, _ = journal_setup
    broker.submit(order())
    journal.paper("exp", broker.checkpoint(), "o1")
    clock[0] += timedelta(seconds=1)
    quote("80", 2, at=clock[0])
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    result = journal.finalize("exp", receipt.entry_id).payload["outcome"]
    assert result["state"] == "STOP_HIT"
    assert Decimal(result["realized_pnl"]) == -21
    assert result["lineage"]["exit_id"] is not None
    assert replay(export(repository))["trade_outcomes"][0] == result


@pytest.mark.parametrize("rejected", [True, False])
def test_no_result_has_no_fabricated_realized_metrics(journal_setup, rejected):
    journal, _, _, _, broker, _, _, order, _ = journal_setup
    broker.submit(order(quantity="0.0001" if rejected else "1"))
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    g = journal.finalize("exp", receipt.entry_id).payload["outcome"]
    assert g["state"] == "NO_RESULT"
    assert all(g[key] is None for key in ("realized_pnl", "fees", "slippage_cost", "r_multiple"))


def test_no_result_correction_appends_superseding_record(journal_setup):
    journal, repository, _, clock, broker, _, quote, order, _ = journal_setup
    entry = broker.submit(order())
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    first = journal.finalize("exp", receipt.entry_id)
    frozen = first.payload_json
    clock[0] += timedelta(seconds=1)
    quote("110", 2, at=clock[0])
    broker.close(entry.fill.position_id, "close", clock[0])
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    with pytest.raises(ValueError):
        journal.finalize("exp", receipt.entry_id)
    second = journal.finalize(
        "exp",
        receipt.entry_id,
        supersedes=first.key,
        correction_reason="actual close now recorded",
        correction_evidence_ids=("close",),
    )
    assert first.payload_json == frozen and second.payload["outcome"]["supersedes"] == first.key
    assert len([e for e in repository.entries() if e.kind == "trade_outcome"]) == 2
    assert replay(export(repository))["trade_outcomes"] == [second.payload["outcome"]]
    assert (
        journal.finalize(
            "exp",
            receipt.entry_id,
            supersedes=first.key,
            correction_reason="actual close now recorded",
            correction_evidence_ids=("close",),
        )
        == second
    )
    with pytest.raises(ValueError):
        journal.finalize(
            "exp",
            receipt.entry_id,
            supersedes=first.key,
            correction_reason="fork",
            correction_evidence_ids=("fork",),
        )


def test_finalization_idempotence_and_no_update_delete(journal_setup):
    journal, repository, path, clock, _, _, _, _, _ = journal_setup
    _, receipt, result = complete(journal_setup)
    clock[0] += timedelta(seconds=10)
    assert journal.finalize("exp", receipt.entry_id) == result
    assert len(repository.entries()) == 4
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE research_journal SET kind='failure' WHERE sequence=1")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM research_journal WHERE sequence=1")


@pytest.mark.parametrize("status", list(FailureStatus))
@pytest.mark.parametrize(
    "stage", [ValidationStage.DATA, ValidationStage.RISK, ValidationStage.VALIDATION]
)
def test_failure_memory_status_reason_evidence_stage(journal_setup, status, stage):
    journal, repository, _, _, _, _, _, _, _ = journal_setup
    memory = FailureMemory(journal)
    record = FailureRecord("failure", status, stage, "validation failed", ("synthetic-evidence",))
    receipt = memory.record("exp", record)
    assert memory.record("exp", record) == receipt
    assert memory.records(family_id="family") == (receipt,)
    assert memory.records(family_id="other") == ()
    assert replay(export(repository))["failures"] == [wire(record)]


def test_duplicate_experiment_and_family_warning(journal_setup):
    journal, _, _, _, _, _, _, _, metadata = journal_setup
    identical = replace(metadata, experiment_id="duplicate")
    result = journal.open_experiment(identical)
    assert result.payload["warnings"] == ["DUPLICATE_EXPERIMENT", "FAMILY_PSEUDO_REPLICATION"]
    related = replace(
        metadata,
        experiment_id="variant",
        config_json=canonical({"strategy": "synthetic", "version": 2}),
    )
    assert journal.open_experiment(related).payload["warnings"] == ["FAMILY_PSEUDO_REPLICATION"]
    assert journal.open_experiment(identical) == result
    with pytest.raises(ValueError):
        journal.open_experiment(replace(metadata, commit_sha="2" * 40))


def test_profitable_paper_does_not_update_directional_reliability(journal_setup):
    journal, _, _, clock, _, _, _, _, _ = journal_setup
    complete(journal_setup)
    assert journal.prospective_snapshot(clock[0]) == ()


def test_durable_registration_restart_then_grade(journal_setup, synthesis_factory):
    journal, _, path, clock, _, _, _, _, _ = journal_setup
    now, event, assessment, quality, _, _ = synthesis_factory
    a = assessment(Lane.TECHNICAL, now)
    registration = journal.register_forecast("exp", a, event(now, "baseline"))
    reopened = SQLiteRepository(path)
    restarted = ResearchJournal(reopened, clock=lambda: clock[0])
    clock[0] += timedelta(seconds=60)
    receipt = restarted.grade_directional(
        "exp", registration.key, event(clock[0], "terminal", "102"), quality(clock[0])
    )
    assert len(restarted.prospective_snapshot(clock[0])) == 1
    assert replay(export(reopened))["directional_outcome_ids"] == [receipt.key]
    assert (
        restarted.grade_directional(
            "exp", registration.key, event(clock[0], "terminal", "102"), quality(clock[0])
        )
        == receipt
    )
    reopened.close()


def test_unregistered_or_backdated_directional_outcome_rejected(journal_setup, synthesis_factory):
    journal, _, _, clock, _, _, _, _, _ = journal_setup
    now, event, assessment, quality, _, _ = synthesis_factory
    clock[0] += timedelta(seconds=60)
    with pytest.raises(ValueError):
        journal.grade_directional("exp", "unregistered", event(clock[0], "end"), quality(clock[0]))
    with pytest.raises(ValueError):
        journal.register_forecast("exp", assessment(Lane.TECHNICAL, now), event(now, "past"))


def test_historical_typed_grades_cannot_substitute_for_durable_registration(
    journal_setup, synthesis_factory
):
    journal, _, _, clock, _, record, _, _, _ = journal_setup
    _, _, _, _, history, inputs = synthesis_factory
    data = inputs(history(Lane.TECHNICAL, 1))
    clock[0] = data.as_of
    data = replace(data, record=record)
    with pytest.raises(ValueError, match="durable prospective"):
        journal.signal("exp", data)


def test_declared_realistic_costs_from_actual_fills(tmp_path, now, paper_setup):
    broker, record, _, quote, order = paper_setup
    broker.configure(costs=PaperCosts())
    clock = [now]
    repository = SQLiteRepository(tmp_path / "realistic.sqlite")
    journal = ResearchJournal(repository, clock=lambda: clock[0])
    metadata = Experiment(
        "exp",
        "family",
        canonical({"synthetic": True}),
        "1" * 40,
        (record.revision,),
        (("binance.spot", 0),),
        None,
        broker.costs,
        now,
    )
    journal.open_experiment(metadata)
    entry = broker.submit(order())
    journal.paper("exp", broker.checkpoint(), "o1")
    clock[0] += timedelta(seconds=1)
    quote("110", 2, at=clock[0])
    exit_result = broker.close(entry.fill.position_id, "close", clock[0])
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    result = journal.finalize("exp", receipt.entry_id).payload["outcome"]
    assert Decimal(result["fees"]) == entry.fill.commission + exit_result.fill.commission
    assert Decimal(result["realized_pnl"]) == broker.realized
    assert Decimal(result["slippage_cost"]) == (entry.fill.price - Decimal(101)) + (
        Decimal(110) - exit_result.fill.price
    )
    repository.close()


def test_store_rollback_collision_and_immutable_payload(journal_setup):
    journal, repository, _, clock, _, _, _, _, _ = journal_setup
    receipt = journal.failure(
        "exp", FailureRecord("id", FailureStatus.BLOCKED, ValidationStage.DATA, "blocked", ("e",))
    )
    before = repository.entries()
    with pytest.raises(ValueError):
        journal.failure(
            "exp",
            FailureRecord("id", FailureStatus.BLOCKED, ValidationStage.DATA, "changed", ("e",)),
        )
    assert repository.entries() == before
    value = receipt.payload
    value["failure"]["reason"] = "mutated"
    assert receipt.payload["failure"]["reason"] == "blocked"
    with pytest.raises(FrozenInstanceError):
        receipt.kind = "paper"
    with pytest.raises(RuntimeError):
        repository.transact(
            clock[0], lambda _: (_ for _ in ()).throw(RuntimeError("builder failure"))
        )
    assert repository.entries() == before


def test_concurrent_connections_serialize_append(journal_setup):
    from concurrent.futures import ThreadPoolExecutor

    _, repository, path, clock, _, _, _, _, _ = journal_setup
    second = SQLiteRepository(path)
    writers = [
        ResearchJournal(repository, clock=lambda: clock[0]),
        ResearchJournal(second, clock=lambda: clock[0]),
    ]

    def append(index):
        return writers[index % 2].failure(
            "exp",
            FailureRecord(
                str(index), FailureStatus.PARKED, ValidationStage.STRATEGY, "parked", ("e",)
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(8)))
    assert len({r.entry_id for r in results}) == 8
    entries = repository.entries()
    assert [e.sequence for e in entries] == list(range(1, 10))
    assert len(replay(export(repository))["failures"]) == 8
    second.close()


@pytest.mark.parametrize("bad", ["target", "expiry", "late"])
def test_exit_classification_fail_closed(journal_setup, bad):
    journal, _, _, clock, broker, _, quote, order, _ = journal_setup
    entry = broker.submit(order())
    original = journal.paper("exp", broker.checkpoint(), "o1")
    state = (
        OutcomeState.TARGET_HIT
        if bad == "target"
        else OutcomeState.EXPIRED
        if bad == "expiry"
        else OutcomeState.SYSTEM_EXIT
    )
    plan = ExitDeclaration(
        entry.fill.position_id,
        "close",
        state,
        "declared",
        ("e",),
        Decimal(200) if bad == "target" else None,
        clock[0] + timedelta(seconds=100) if bad == "expiry" else None,
    )
    if bad != "late":
        declaration = journal.declare_exit("exp", original.entry_id, plan)
    clock[0] += timedelta(seconds=1)
    quote("110", 2, at=clock[0])
    broker.close(entry.fill.position_id, "close", clock[0])
    receipt = journal.paper("exp", broker.checkpoint(), "o1")
    if bad == "late":
        clock[0] += timedelta(seconds=1)
        declaration = journal.declare_exit("exp", original.entry_id, plan)
    with pytest.raises(ValueError):
        journal.finalize("exp", receipt.entry_id, declaration_entry_id=declaration.entry_id)


def test_actual_history_cannot_be_shortened_or_rewritten(journal_setup):
    journal, _, _, _, broker, _, _, order, _ = journal_setup
    original = broker.checkpoint()
    broker.submit(order())
    journal.paper("exp", broker.checkpoint(), "o1")
    from vision.execution.paper.broker import PaperBroker

    fork = PaperBroker.from_checkpoint(original)
    fork.submit(order(stop="95"))
    with pytest.raises(ValueError, match="cannot be rewritten"):
        journal.paper("exp", fork.checkpoint(), "o1")
    fork = PaperBroker.from_checkpoint(original)
    fork.submit(order(identity="different-order"))
    with pytest.raises(ValueError, match="cannot be rewritten"):
        journal.paper("exp", fork.checkpoint(), "different-order")


def test_correction_cannot_regress_to_old_open_checkpoint(journal_setup):
    journal, _, _, _, broker, _, _, order, _ = journal_setup
    broker.submit(order())
    original = journal.paper("exp", broker.checkpoint(), "o1")
    _, _, closed = complete(journal_setup)
    with pytest.raises(ValueError, match="cannot regress"):
        journal.finalize(
            "exp",
            original.entry_id,
            supersedes=closed.key,
            correction_reason="old open checkpoint",
            correction_evidence_ids=("old",),
        )


def test_same_paper_fill_cannot_be_multiple_experiments(journal_setup):
    journal, _, _, _, broker, _, _, order, metadata = journal_setup
    journal.open_experiment(replace(metadata, experiment_id="other"))
    broker.submit(order())
    journal.paper("exp", broker.checkpoint(), "o1")
    with pytest.raises(ValueError):
        journal.paper("other", broker.checkpoint(), "o1")


def test_forged_pnl_resigned_export_fails_semantic_replay(journal_setup):
    complete(journal_setup)
    value = export(journal_setup[1])
    last = value["entries"][-1]
    payload = json.loads(last["payload_json"])
    payload["outcome"]["realized_pnl"] = "9999"
    last["payload_json"] = canonical(payload)
    last["entry_hash"] = digest({k: v for k, v in last.items() if k != "entry_hash"})
    value["head"] = last["entry_hash"]
    with pytest.raises(ValueError, match="semantic"):
        replay(value)


@pytest.mark.parametrize("change", ["truncate", "reorder", "hash", "extra", "nan", "head"])
def test_export_integrity_and_shape(journal_setup, change):
    complete(journal_setup)
    value = export(journal_setup[1])
    if change == "truncate":
        value["entries"].pop()
    elif change == "reorder":
        value["entries"].reverse()
    elif change == "hash":
        value["entries"][0]["entry_hash"] = "wrong"
    elif change == "extra":
        value["entries"][0]["unexpected"] = "x"
    elif change == "nan":
        value["entries"][0]["payload_json"] = '{"n":NaN}'
    else:
        value["head"] = "wrong"
    with pytest.raises(ValueError):
        replay(value)


def test_clock_and_experiment_backdate_reject(journal_setup):
    journal, repository, _, clock, _, _, _, _, metadata = journal_setup
    with pytest.raises(ValueError):
        journal.open_experiment(
            replace(metadata, experiment_id="backdated", created_at=clock[0] - timedelta(seconds=1))
        )
    clock[0] -= timedelta(seconds=1)
    with pytest.raises(ValueError):
        journal.failure(
            "exp",
            FailureRecord(
                "failure", FailureStatus.BLOCKED, ValidationStage.DATA, "blocked", ("e",)
            ),
        )
    assert len(repository.entries()) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"commit_sha": "bad"},
        {"spec_revisions": ()},
        {"source_provenance": ()},
        {"config_json": "{} "},
    ],
)
def test_metadata_contract_rejects_missing_or_noncanonical(journal_setup, kwargs):
    with pytest.raises(ValueError):
        replace(journal_setup[-1], **kwargs)


def test_replay_after_external_raw_invalid_append_is_blocked(journal_setup):
    journal, repository, _, clock, _, _, _, _, _ = journal_setup
    repository.transact(
        clock[0], lambda _: Pending("directional_grade", "exp", "fake", canonical({"outcome": {}}))
    )
    with pytest.raises(ValueError):
        journal.prospective_snapshot(clock[0])
    with pytest.raises(ValueError):
        journal.failure(
            "exp", FailureRecord("f", FailureStatus.BLOCKED, ValidationStage.DATA, "reason", ("e",))
        )


@pytest.mark.parametrize("late_intent", [False, True])
def test_complete_lineage_with_durable_reliability_and_actual_paper(
    journal_setup, synthesis_factory, late_intent
):
    from vision.analysis.synthesizer.reliability import ReliabilityPolicy
    from vision.outcomes.prospective import GradingPolicy

    journal, repository, _, clock, broker, record, quote, order, _ = journal_setup
    now, event, assessment, quality, _, inputs = synthesis_factory
    protocol = GradingPolicy(horizon=timedelta(seconds=1))
    for index in range(10):
        clock[0] = now + timedelta(seconds=index * 2)
        registrations = [
            journal.register_forecast(
                "exp", assessment(lane, clock[0]), event(clock[0], f"base-{index}"), policy=protocol
            )
            for lane in (Lane.TECHNICAL, Lane.FLOW)
        ]
        clock[0] += timedelta(seconds=1)
        for registration in registrations:
            journal.grade_directional(
                "exp",
                registration.key,
                event(clock[0], f"terminal-{index}", "102"),
                quality(clock[0]),
            )
    clock[0] += timedelta(seconds=1)
    grades = journal.prospective_snapshot(clock[0])
    data = replace(
        inputs(),
        record=record,
        as_of=clock[0],
        quality=quality(clock[0]),
        outcomes=grades,
        assessments=tuple(assessment(lane, clock[0]) for lane in (Lane.TECHNICAL, Lane.FLOW)),
        reliability_policy=ReliabilityPolicy(minimum_samples=10, grading_policy=protocol),
    )
    signal = journal.signal("exp", data)
    entry_at = clock[0]
    if late_intent:
        clock[0] += timedelta(seconds=1)
    intent = journal.intent("exp", signal.entry_id)
    quote("100", 2, at=entry_at)
    entry = broker.submit(order(at=entry_at))
    if late_intent:
        with pytest.raises(ValueError, match="does not match"):
            journal.paper("exp", broker.checkpoint(), "o1", intent_entry_id=intent.entry_id)
        assert not any(e.kind == "paper" for e in repository.entries())
        return
    journal.paper("exp", broker.checkpoint(), "o1", intent_entry_id=intent.entry_id)
    clock[0] += timedelta(seconds=1)
    quote("110", 3, at=clock[0])
    exit_result = broker.close(entry.fill.position_id, "close", clock[0])
    receipt = journal.paper("exp", broker.checkpoint(), "o1", intent_entry_id=intent.entry_id)
    result = journal.finalize("exp", receipt.entry_id).payload["outcome"]
    lineage = result["lineage"]
    assert lineage["assessment_ids"] == [a.assessment_id for a in data.assessments]
    assert lineage["synthesis_id"] == signal.key and lineage["intent_id"] == intent.key
    assert lineage["risk_decision_id"] == entry.decision.risk_decision_id
    assert lineage["order_id"] == entry.order_id and lineage["fill_id"] == entry.fill.fill_id
    assert (
        lineage["position_id"] == entry.fill.position_id
        and lineage["exit_id"] == exit_result.fill.fill_id
    )
    assert lineage["outcome_id"] == result["outcome_id"]
    assert journal.prospective_snapshot(clock[0]) == grades
    report = replay(export(repository))
    assert len(report["directional_outcome_ids"]) == 20
    assert report["trade_outcomes"] == [result]


def test_directional_correctness_is_not_paper_strategy_profit(journal_setup, synthesis_factory):
    journal, repository, _, clock, _, _, _, _, _ = journal_setup
    now, event, assessment, quality, _, _ = synthesis_factory
    registration = journal.register_forecast(
        "exp", assessment(Lane.TECHNICAL, now), event(now, "base")
    )
    clock[0] += timedelta(seconds=60)
    journal.grade_directional(
        "exp", registration.key, event(clock[0], "end", "102"), quality(clock[0])
    )
    assert replay(export(repository))["trade_outcomes"] == []


def test_crash_before_commit_rolls_back_and_committed_receipt_survives(journal_setup):
    import subprocess
    import sys

    _, repository, path, _, _, _, _, _, _ = journal_setup
    before = repository.entries()
    script = """import os,sqlite3,sys
c=sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute('BEGIN IMMEDIATE')
c.execute('INSERT INTO research_journal SELECT sequence+1,entry_id||\"new\",'
          'experiment_id,kind,logical_key,recorded_at,payload_json,previous_hash,'
          'entry_hash FROM research_journal WHERE sequence=1')
os._exit(9)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 9
    assert repository.entries() == before
    reopened = SQLiteRepository(path, readonly=True)
    assert reopened.entries() == before
    assert replay(export(reopened))["entries"] == 1
    with pytest.raises(ValueError):
        reopened.transact(
            before[0].recorded_at, lambda _: Pending("failure", "exp", "f", canonical({}))
        )
    reopened.close()


def test_wrong_database_version_rejects(tmp_path):
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError):
        SQLiteRepository(path)


def test_backfilled_signal_recorded_but_cannot_become_intent(journal_setup, synthesis_factory):
    journal, repository, _, clock, _, record, _, _, _ = journal_setup
    _, _, _, _, _, inputs = synthesis_factory
    data = replace(inputs(), record=record)
    clock[0] = data.as_of + timedelta(seconds=100)
    with pytest.raises(ValueError):
        journal.signal("exp", data)
    receipt = journal.signal("exp", data, backfilled=True)
    with pytest.raises(ValueError):
        journal.intent("exp", receipt.entry_id)
    assert replay(export(repository))["signal_ids"] == [receipt.key]


def cli(*args, enabled=False):
    import os
    import subprocess
    import sys

    environment = {
        **os.environ,
        **{
            key: "false"
            for key in (
                "VISION_LIVE_TRADING_ENABLED",
                "VISION_MT5_EXECUTION_ENABLED",
                "VISION_PAPER_TRADING_ENABLED",
                "VISION_AGENTS_ENABLED",
            )
        },
    }
    if enabled:
        environment["VISION_LIVE_TRADING_ENABLED"] = "true"
    return subprocess.run(
        [sys.executable, "-m", "vision", *map(str, args)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_cli_export_replay_and_no_overwrite(journal_setup, tmp_path):
    _, repository, database, *_ = journal_setup
    complete(journal_setup)
    destination = tmp_path / "portable.json"
    result = cli("research-export", database, destination)
    assert result.returncode == 0
    expected = replay(export(repository))
    assert json.loads(result.stdout) == expected
    before = destination.read_bytes()
    assert cli("research-export", database, destination).returncode == 2
    assert destination.read_bytes() == before
    result = cli("research-replay", destination, "--expected-head", expected["head"])
    assert result.returncode == 0 and json.loads(result.stdout) == expected
    assert cli("research-replay", destination, "--expected-head", "0" * 64).returncode == 2


def test_cli_missing_database_never_creates_it(tmp_path):
    missing, output = tmp_path / "missing.sqlite", tmp_path / "output.json"
    assert cli("research-export", missing, output).returncode == 2
    assert not missing.exists() and not output.exists()


@pytest.mark.parametrize("command", ["research-export", "research-replay"])
def test_cli_execution_guard_before_any_journal_io(tmp_path, command):
    source, output = tmp_path / "missing", tmp_path / "output"
    args = (source, output) if command == "research-export" else (source,)
    result = cli(command, *args, enabled=True)
    assert result.returncode == 2 and "must be false" in result.stderr
    assert not source.exists() and not output.exists()


def test_portable_synthetic_fixture_has_no_reliability_evidence():
    from pathlib import Path

    path = Path(__file__).parent / "fixtures/journal/paper_research.json"
    report = replay(json.loads(path.read_text(encoding="utf-8")))
    assert len(report["trade_outcomes"]) == 1
    assert report["trade_outcomes"][0]["state"] == "MANUAL_EXIT"
    assert report["directional_outcome_ids"] == report["prospective_registration_ids"] == []
