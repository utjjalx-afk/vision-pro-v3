"""Immutable research governance records, independent of reliability and PnL."""

from dataclasses import dataclass
from enum import StrEnum

from vision.core.contracts import _identifier


class FailureStatus(StrEnum):
    REJECTED = "REJECTED"
    PARKED = "PARKED"
    BLOCKED = "BLOCKED"


class ValidationStage(StrEnum):
    DATA = "DATA"
    FEATURES = "FEATURES"
    SYNTHESIS = "SYNTHESIS"
    STRATEGY = "STRATEGY"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    OUTCOME = "OUTCOME"
    VALIDATION = "VALIDATION"


@dataclass(frozen=True, slots=True)
class FailureRecord:
    failure_id: str
    status: FailureStatus
    stage: ValidationStage
    reason: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self):
        _identifier(self.failure_id)
        _identifier(self.reason)
        if not isinstance(self.status, FailureStatus) or not isinstance(
            self.stage, ValidationStage
        ):
            raise ValueError("Explicit failure status/stage required")
        if type(self.evidence_ids) is not tuple or not 1 <= len(self.evidence_ids) <= 256:
            raise ValueError("Immutable bounded failure evidence required")
        for value in self.evidence_ids:
            _identifier(value)
