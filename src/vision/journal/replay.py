"""Complete offline semantic reconstruction, including checkpoint and prospective proofs."""

from vision.analysis.synthesizer.replay import inputs_from_dict, outcome
from vision.core.codec import event_from_dict
from vision.core.state.replay import shape
from vision.failure_memory.models import FailureRecord, FailureStatus, ValidationStage
from vision.journal.repository import entries_from_export, verify
from vision.journal.service import (
    ResearchJournal,
    assessment,
    declaration_from_dict,
    experiment_from_dict,
    grading_policy,
)


class ReplayRepository:
    """A verifier, not an import path into eligible local reliability."""

    def __init__(self, expected):
        self.expected, self.prefix = expected, ()

    def entries(self):
        return self.prefix

    def transact(self, at, build):
        expected = self.expected[len(self.prefix)]
        pending = build(self.prefix)
        if (
            at != expected.recorded_at
            or pending.entry_id != expected.entry_id
            or pending.kind != expected.kind
            or pending.experiment_id != expected.experiment_id
            or pending.key != expected.key
            or pending.payload_json != expected.payload_json
        ):
            raise ValueError("Journal semantic replay mismatch")
        self.prefix += (expected,)
        return expected


def replay_entries(entries):
    verify(entries)
    repository = ReplayRepository(entries)
    at = [None]
    journal = ResearchJournal(repository, clock=lambda: at[0], _replaying=True)
    for entry in entries:
        at[0] = entry.recorded_at
        value = entry.payload
        if entry.kind == "experiment":
            journal.open_experiment(experiment_from_dict(value["experiment"]))
        elif entry.kind == "prospective_registration":
            c = value["commitment"]
            journal.register_forecast(
                entry.experiment_id,
                assessment(c["assessment"]),
                event_from_dict(c["baseline"]),
                policy=grading_policy(c["policy"]),
                regime=c["regime"],
                fixture_only=c["fixture_only"],
            )
        elif entry.kind == "directional_grade":
            g = outcome(value["outcome"])
            journal.grade_directional(
                entry.experiment_id, g.commitment.commitment_id, g.terminal, g.quality
            )
        elif entry.kind == "signal":
            journal.signal(
                entry.experiment_id,
                inputs_from_dict(value["input"]),
                backfilled=value["backfilled"],
            )
        elif entry.kind == "intent":
            journal.intent(entry.experiment_id, value["signal_entry_id"])
        elif entry.kind == "paper":
            journal.paper(
                entry.experiment_id,
                value["checkpoint"],
                value["request_id"],
                intent_entry_id=value["intent_entry_id"],
            )
        elif entry.kind == "exit_declaration":
            journal.declare_exit(
                entry.experiment_id,
                value["paper_receipt_id"],
                declaration_from_dict(value["declaration"]),
            )
        elif entry.kind == "trade_outcome":
            g = value["outcome"]
            if not isinstance(g["correction_evidence_ids"], list):
                raise ValueError("Correction evidence array required")
            journal.finalize(
                entry.experiment_id,
                g["paper_receipt_id"],
                declaration_entry_id=value["declaration_entry_id"],
                supersedes=g["supersedes"],
                correction_reason=g["correction_reason"],
                correction_evidence_ids=tuple(g["correction_evidence_ids"]),
            )
        elif entry.kind == "failure":
            data = shape(value["failure"], FailureRecord)
            data["status"], data["stage"] = (
                FailureStatus(data["status"]),
                ValidationStage(data["stage"]),
            )
            if not isinstance(data["evidence_ids"], list):
                raise ValueError("Failure evidence array required")
            data["evidence_ids"] = tuple(data["evidence_ids"])
            journal.failure(entry.experiment_id, FailureRecord(**data))
        elif entry.kind == "backtest":
            from vision.research.backtest import inputs_from_dict as backtest_input

            journal.backtest(entry.experiment_id, backtest_input(value["input"]))
        elif entry.kind == "backtest_suite":
            from vision.research.audit import suite_from_dict

            journal.backtest_suite(entry.experiment_id, suite_from_dict(value["definition"]))
        elif entry.kind == "strategy_artifact":
            from vision.journal.repository import canonical
            from vision.strategies.dsl import parse
            from vision.strategies.runtime import dataset_from_dict

            journal.register_strategy(
                entry.experiment_id,
                parse(canonical(value["artifact"])),
                dataset_from_dict(value["dataset"]),
            )
        elif entry.kind == "strategy_lifecycle":
            journal.strategy_lifecycle(
                entry.experiment_id,
                value["strategy_id"],
                entry.key,
                value["to"],
                value["reason"],
                tuple(value["evidence_ids"]),
            )
        elif entry.kind == "strategy_result":
            import json

            from vision.strategies.dsl import parse
            from vision.strategies.runtime import confirmation_from_dict, dataset_from_dict

            c = value["compilation"]
            journal.strategy_result(
                entry.experiment_id,
                parse(c["artifact_json"]),
                dataset_from_dict(json.loads(c["dataset_json"])),
                confirmation_from_dict(value["confirmation"]),
            )
    return repository.entries()


def replay(value, *, expected_head=None):
    try:
        entries = entries_from_export(value)
        if expected_head is not None and verify(entries) != expected_head:
            raise ValueError("Expected durable journal head mismatch")
        replay_entries(entries)
        latest = {}
        for entry in entries:
            if entry.kind == "trade_outcome":
                latest[entry.payload["outcome"]["root_id"]] = entry.payload["outcome"]
        return {
            "head": verify(entries),
            "entries": len(entries),
            "experiments": [e.payload for e in entries if e.kind == "experiment"],
            "signal_ids": [e.key for e in entries if e.kind == "signal"],
            "intent_ids": [e.key for e in entries if e.kind == "intent"],
            "paper_heads": [e.payload["checkpoint"]["head"] for e in entries if e.kind == "paper"],
            "trade_outcomes": list(latest.values()),
            "prospective_registration_ids": [
                e.key for e in entries if e.kind == "prospective_registration"
            ],
            "directional_outcome_ids": [e.key for e in entries if e.kind == "directional_grade"],
            "failures": [e.payload["failure"] for e in entries if e.kind == "failure"],
            "backtest_runs": [e.payload["run"] for e in entries if e.kind == "backtest"],
            "backtest_suites": [e.payload["report"] for e in entries if e.kind == "backtest_suite"],
            "strategy_artifacts": [e.payload for e in entries if e.kind == "strategy_artifact"],
            "strategy_lifecycle": [e.payload for e in entries if e.kind == "strategy_lifecycle"],
            "strategy_results": [
                e.payload["result"] for e in entries if e.kind == "strategy_result"
            ],
        }
    except (TypeError, KeyError, IndexError, OverflowError) as error:
        raise ValueError("Invalid research export structure") from error
