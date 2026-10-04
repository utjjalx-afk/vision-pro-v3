"""Unsized candidate boundary: strategy eligibility and risk remain separate."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from vision.analysis.contracts import wire
from vision.analysis.synthesizer.contracts import Direction, SynthesisDecision
from vision.core.contracts import _identifier, _utc
from vision.core.instruments import digest


@dataclass(frozen=True, slots=True)
class TradeIntent:
    intent_id: str
    decision_id: str
    symbol: str
    instrument_id: str
    direction: Direction
    confidence: Decimal
    timestamp: datetime
    market_timestamp: datetime
    thesis_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    reliability_revisions: tuple[str, ...]
    expires_at: datetime
    invalidation_conditions: tuple[str, ...]
    input_lineage: str
    source_lineage: str
    spec_revision: str
    candidate_only: bool = True

    def __post_init__(self):
        for value in (
            self.intent_id,
            self.decision_id,
            self.symbol,
            self.instrument_id,
            self.input_lineage,
            self.source_lineage,
            self.spec_revision,
        ):
            _identifier(value)
        for value in (self.timestamp, self.market_timestamp, self.expires_at):
            _utc(value)
        if (
            not isinstance(self.direction, Direction)
            or not isinstance(self.confidence, Decimal)
            or not self.confidence.is_finite()
            or not 0 < self.confidence <= 1
            or not self.market_timestamp <= self.timestamp < self.expires_at
        ):
            raise ValueError("Current typed candidate support required")
        if (
            self.direction not in {Direction.LONG, Direction.SHORT}
            or self.candidate_only is not True
        ):
            raise ValueError("Unsized LONG/SHORT candidate required")
        if any(
            type(value) is not tuple
            for value in (
                self.thesis_ids,
                self.evidence_hashes,
                self.reliability_revisions,
                self.invalidation_conditions,
            )
        ):
            raise ValueError("Immutable intent lineage required")
        if (
            len(self.thesis_ids) < 2
            or not self.evidence_hashes
            or not self.reliability_revisions
            or not self.invalidation_conditions
        ):
            raise ValueError("Candidate requires multiple eligible theses and complete lineage")
        for values in (
            self.thesis_ids,
            self.evidence_hashes,
            self.reliability_revisions,
            self.invalidation_conditions,
        ):
            for value in values:
                _identifier(value)

    def to_dict(self):
        return wire(self)


def candidate(decision):
    if not isinstance(decision, SynthesisDecision):
        raise ValueError("SynthesisDecision required")
    if decision.direction is Direction.WAIT:
        return None
    identity = digest({"decision": decision.decision_id, "version": "phase6-intent-v1"})
    return TradeIntent(
        identity,
        decision.decision_id,
        decision.symbol,
        decision.instrument_id,
        decision.direction,
        decision.confidence,
        decision.timestamp,
        decision.market_timestamp,
        tuple(c.lane_id for c in decision.contributions if c.exclusion is None),
        tuple(c.evidence_hash for c in decision.contributions),
        tuple(c.reliability.revision_id for c in decision.contributions),
        decision.expires_at,
        (
            "assessment_expired",
            "data_unhealthy_or_divergent",
            "source_epoch_changed",
            "instrument_revision_changed",
            "strategy_ineligible",
            "risk_veto",
        ),
        decision.input_lineage,
        decision.source_lineage,
        decision.spec_revision,
    )
