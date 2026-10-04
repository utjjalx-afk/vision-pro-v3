"""Immutable synthesis snapshots and decisions; no executable order fields."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from vision.analysis.contracts import LaneAssessment, wire
from vision.analysis.synthesizer.reliability import ReliabilityPolicy, ReliabilityRevision
from vision.core.contracts import _identifier, _utc
from vision.core.instruments import InstrumentRecord, digest
from vision.core.state.portfolio import DataQuality
from vision.outcomes.prospective import duration, fraction, validate_history


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass(frozen=True, slots=True)
class SynthesisPolicy:
    minimum_lanes: int = 2
    minimum_support: Decimal = Decimal("0.1")
    minimum_direction: Decimal = Decimal("0.25")
    maximum_disagreement: Decimal = Decimal("0.2")
    maximum_lane_share: Decimal = Decimal("0.6")
    max_age: timedelta = timedelta(seconds=5)
    max_spec_age: timedelta = timedelta(hours=24)

    def __post_init__(self):
        if type(self.minimum_lanes) is not int or not 2 <= self.minimum_lanes <= 4:
            raise ValueError("At least two independently eligible lanes required")
        for value in (
            self.minimum_support,
            self.minimum_direction,
            self.maximum_disagreement,
            self.maximum_lane_share,
        ):
            fraction(value)
        if (
            self.minimum_support <= 0
            or self.minimum_direction <= 0
            or not Decimal("0.5") <= self.maximum_lane_share < 1
        ):
            raise ValueError("Positive support/direction and a strict dominance cap required")
        duration(self.max_age)
        duration(self.max_spec_age)


@dataclass(frozen=True, slots=True)
class SynthesisInput:
    record: InstrumentRecord
    as_of: datetime
    assessments: tuple[LaneAssessment, ...]
    outcomes: tuple
    quality: DataQuality
    reliability_policy: ReliabilityPolicy = ReliabilityPolicy()
    policy: SynthesisPolicy = SynthesisPolicy()
    regime: str | None = None

    def __post_init__(self):
        _utc(self.as_of)
        if (
            not isinstance(self.record, InstrumentRecord)
            or not isinstance(self.quality, DataQuality)
            or not isinstance(self.policy, SynthesisPolicy)
            or not isinstance(self.reliability_policy, ReliabilityPolicy)
        ):
            raise ValueError("Typed synthesis metadata/policy required")
        if type(self.assessments) is not tuple or not 1 <= len(self.assessments) <= 4:
            raise ValueError("Bounded immutable lane assessments required")
        if any(not isinstance(item, LaneAssessment) for item in self.assessments):
            raise ValueError("LaneAssessment required")
        if len({item.lane for item in self.assessments}) != len(self.assessments):
            raise ValueError("Duplicate lane cannot cast multiple contributions")
        if len({(item.timestamp, item.context_lineage) for item in self.assessments}) != 1:
            raise ValueError("Synchronized shared lane context required")
        for item in self.assessments:
            if item.instrument_id != self.record.spec.instrument_id or item.timestamp > self.as_of:
                raise ValueError("Mixed instrument/future assessment")
            for value in (item.score, item.confidence):
                if value is not None and (
                    len(value.as_tuple().digits) > 1024 or abs(value.as_tuple().exponent) > 512
                ):
                    raise ValueError("Bounded derived assessment values required")
            if any(
                e.source_ts > item.timestamp or e.received_ts > item.timestamp
                for e in item.evidence
            ):
                raise ValueError("Lookahead evidence")
        if self.quality.as_of > self.as_of or self.record.provenance.observed_at > self.as_of:
            raise ValueError("Future quality/spec metadata")
        validate_history(self.outcomes)
        if any(g.graded_at > self.market_timestamp for g in self.outcomes):
            raise ValueError("Reliability outcomes must be known by the market timestamp")
        if self.regime is not None:
            _identifier(self.regime)

    @property
    def market_timestamp(self):
        return self.assessments[0].timestamp

    @property
    def lineage_id(self):
        return digest(wire(self))


@dataclass(frozen=True, slots=True)
class Contribution:
    lane_id: str
    evidence_hash: str
    reliability: ReliabilityRevision
    data_quality_factor: Decimal | None
    weight: Decimal | None
    signed_contribution: Decimal | None
    exclusion: str | None

    def __post_init__(self):
        for value in (self.lane_id, self.evidence_hash):
            _identifier(value)
        if not isinstance(self.reliability, ReliabilityRevision):
            raise ValueError("Pinned reliability revision required")
        if self.exclusion is not None:
            _identifier(self.exclusion)
            if any(
                value is not None
                for value in (self.data_quality_factor, self.weight, self.signed_contribution)
            ):
                raise ValueError("Excluded lane cannot cast a contribution")
        elif (
            self.data_quality_factor != Decimal(1)
            or self.weight is None
            or self.signed_contribution is None
            or not 0 < self.weight <= 1
            or not -self.weight <= self.signed_contribution <= self.weight
        ):
            raise ValueError("Invalid eligible contribution")


@dataclass(frozen=True, slots=True)
class SynthesisDecision:
    decision_id: str
    direction: Direction
    instrument_id: str
    symbol: str
    timestamp: datetime
    market_timestamp: datetime
    score: Decimal | None
    confidence: Decimal | None
    contributions: tuple[Contribution, ...]
    reasons: tuple[str, ...]
    data_quality: DataQuality
    input_lineage: str
    source_lineage: str
    spec_revision: str
    expires_at: datetime
    algorithm_version: str = "phase6-v1"

    def __post_init__(self):
        for value in (
            self.decision_id,
            self.instrument_id,
            self.symbol,
            self.input_lineage,
            self.source_lineage,
            self.spec_revision,
        ):
            _identifier(value)
        for value in (self.timestamp, self.market_timestamp, self.expires_at):
            _utc(value)
        if (
            not isinstance(self.direction, Direction)
            or not isinstance(self.data_quality, DataQuality)
            or type(self.contributions) is not tuple
            or type(self.reasons) is not tuple
            or self.algorithm_version != "phase6-v1"
            or self.market_timestamp > self.timestamp
        ):
            raise ValueError("Immutable decision provenance required")
        if self.direction is Direction.WAIT:
            if not self.reasons:
                raise ValueError("WAIT requires reasons")
        elif (
            self.reasons
            or self.score is None
            or self.confidence is None
            or self.timestamp >= self.expires_at
            or self.confidence <= 0
            or (self.direction is Direction.LONG) != (self.score > 0)
            or self.score == 0
        ):
            raise ValueError("Directional decision requires current eligible support")
        if self.score is not None and (not self.score.is_finite() or not -1 <= self.score <= 1):
            raise ValueError("Invalid decision score")
        if self.confidence is not None and (
            not self.confidence.is_finite() or not 0 <= self.confidence <= 1
        ):
            raise ValueError("Invalid support indicator")

    def to_dict(self):
        return wire(self)
