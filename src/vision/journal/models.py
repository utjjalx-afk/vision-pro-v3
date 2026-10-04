"""Immutable research metadata, exit declarations and executed paper outcomes."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from vision.core.contracts import _identifier, _utc
from vision.core.instruments import digest
from vision.execution.paper.models import PaperCosts
from vision.journal.repository import canonical


class OutcomeState(StrEnum):
    TARGET_HIT = "TARGET_HIT"
    STOP_HIT = "STOP_HIT"
    EXPIRED = "EXPIRED"
    NO_RESULT = "NO_RESULT"
    MANUAL_EXIT = "MANUAL_EXIT"
    SYSTEM_EXIT = "SYSTEM_EXIT"


@dataclass(frozen=True, slots=True)
class Experiment:
    experiment_id: str
    family_id: str
    config_json: str
    commit_sha: str
    spec_revisions: tuple[str, ...]
    source_provenance: tuple[tuple[str, int], ...]
    regime: str | None
    costs: PaperCosts
    created_at: datetime

    def __post_init__(self):
        _identifier(self.experiment_id)
        _identifier(self.family_id)
        _utc(self.created_at)
        if not isinstance(self.commit_sha, str) or not re.fullmatch(
            r"[0-9a-f]{40}", self.commit_sha
        ):
            raise ValueError("Explicit Git commit SHA required")
        if (
            not isinstance(self.costs, PaperCosts)
            or type(self.spec_revisions) is not tuple
            or type(self.source_provenance) is not tuple
        ):
            raise ValueError("Immutable experiment economics/provenance required")
        if not self.spec_revisions or not self.source_provenance:
            raise ValueError("Experiment requires pinned spec/source provenance")
        if len(self.spec_revisions) > 256 or len(self.source_provenance) > 256:
            raise ValueError("Bounded experiment provenance required")
        for revision in self.spec_revisions:
            if not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise ValueError("Spec revision SHA-256 required")
        for pair in self.source_provenance:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("Provider/epoch pair required")
            _identifier(pair[0])
            if type(pair[1]) is not int or pair[1] < 0:
                raise ValueError("Explicit source epoch required")
        if self.regime is not None:
            _identifier(self.regime)
        if not isinstance(self.config_json, str) or not isinstance(
            json.loads(self.config_json), dict
        ):
            raise ValueError("Canonical experiment configuration object required")
        if canonical(json.loads(self.config_json)) != self.config_json:
            raise ValueError("Canonical configuration required")

    @property
    def config_hash(self):
        return digest(json.loads(self.config_json))

    @property
    def fingerprint(self):
        from vision.analysis.contracts import wire

        return digest(
            {
                "config": self.config_hash,
                "commit": self.commit_sha,
                "specs": self.spec_revisions,
                "sources": self.source_provenance,
                "regime": self.regime,
                "costs": wire(self.costs),
            }
        )


@dataclass(frozen=True, slots=True)
class ExitDeclaration:
    position_id: str
    exit_request_id: str
    state: OutcomeState
    reason: str
    evidence_ids: tuple[str, ...]
    target_price: Decimal | None = None
    expires_at: datetime | None = None

    def __post_init__(self):
        for value in (self.position_id, self.exit_request_id, self.reason):
            _identifier(value)
        if self.state not in {
            OutcomeState.TARGET_HIT,
            OutcomeState.EXPIRED,
            OutcomeState.MANUAL_EXIT,
            OutcomeState.SYSTEM_EXIT,
        }:
            raise ValueError("Explicit supported exit declaration required")
        if (
            not isinstance(self.state, OutcomeState)
            or type(self.evidence_ids) is not tuple
            or not 1 <= len(self.evidence_ids) <= 256
        ):
            raise ValueError("Immutable exit evidence required")
        for value in self.evidence_ids:
            _identifier(value)
        if self.state is OutcomeState.TARGET_HIT:
            if (
                not isinstance(self.target_price, Decimal)
                or not self.target_price.is_finite()
                or self.target_price <= 0
            ):
                raise ValueError("Explicit target price required")
        elif self.target_price is not None:
            raise ValueError("Unexpected target price")
        if self.state is OutcomeState.EXPIRED:
            _utc(self.expires_at)
        elif self.expires_at is not None:
            raise ValueError("Unexpected expiry")


@dataclass(frozen=True, slots=True)
class TradeLineage:
    assessment_ids: tuple[str, ...]
    synthesis_id: str | None
    intent_id: str | None
    risk_decision_id: str
    order_id: str
    fill_id: str | None
    position_id: str | None
    exit_id: str | None
    exit_order_id: str | None
    outcome_id: str


@dataclass(frozen=True, slots=True)
class TradeOutcome:
    outcome_id: str
    root_id: str
    state: OutcomeState
    currency: str
    realized_pnl: Decimal | None
    fees: Decimal | None
    slippage_cost: Decimal | None
    initial_risk: Decimal | None
    r_multiple: Decimal | None
    finalized_at: datetime
    lineage: TradeLineage
    paper_receipt_id: str
    supersedes: str | None
    correction_reason: str | None
    correction_evidence_ids: tuple[str, ...]
