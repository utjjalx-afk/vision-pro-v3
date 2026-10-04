"""Append-only lifecycle metadata and failure linkage; no deployment grants."""

from vision.failure_memory.models import FailureRecord, FailureStatus, ValidationStage
from vision.strategies.dsl import Lifecycle, text


def effective_lifecycle(entries, strategy_id):
    registrations = [
        e
        for e in entries
        if e.kind == "strategy_artifact" and e.payload["strategy_id"] == strategy_id
    ]
    if not registrations:
        raise ValueError("Strategy must be durably registered")
    transitions = [
        e
        for e in entries
        if e.kind == "strategy_lifecycle" and e.payload["strategy_id"] == strategy_id
    ]
    if transitions:
        latest = transitions[-1]
        return Lifecycle(latest.payload["to"]), latest.entry_id
    return Lifecycle(registrations[0].payload["artifact"]["lifecycle"]), registrations[0].entry_id


def allowed_transition(before, after):
    transitions = {
        Lifecycle.CANDIDATE: {
            Lifecycle.TESTING,
            Lifecycle.PARKED,
            Lifecycle.REJECTED,
            Lifecycle.BLOCKED,
        },
        Lifecycle.TESTING: {Lifecycle.PARKED, Lifecycle.REJECTED, Lifecycle.BLOCKED},
        Lifecycle.PARKED: {Lifecycle.CANDIDATE, Lifecycle.REJECTED, Lifecycle.BLOCKED},
        Lifecycle.BLOCKED: {Lifecycle.CANDIDATE, Lifecycle.REJECTED},
    }
    if after not in transitions.get(before, set()):
        raise ValueError("Unsupported lifecycle transition; deployment promotion is unavailable")


def evidence(reason, ids):
    text(reason)
    if type(ids) is not tuple or not 1 <= len(ids) <= 256:
        raise ValueError("Immutable bounded lifecycle evidence required")
    for item in ids:
        text(item)


def record_strategy(journal, experiment_id, artifact, dataset, confirmation=None):
    from vision.journal.service import FailureMemory

    journal.register_strategy(experiment_id, artifact, dataset)
    entry = journal.strategy_result(experiment_id, artifact, dataset, confirmation)
    status = entry.payload["result"]["status"]
    if status != "PASS":
        failure = {
            "BLOCKED": FailureStatus.BLOCKED,
            "FAIL": FailureStatus.REJECTED,
            "INCONCLUSIVE": FailureStatus.PARKED,
        }[status]
        FailureMemory(journal).record(
            experiment_id,
            FailureRecord(
                entry.key,
                failure,
                ValidationStage.STRATEGY if status == "BLOCKED" else ValidationStage.VALIDATION,
                "DSL runtime status: " + status,
                (entry.entry_id,),
            ),
        )
    return entry
