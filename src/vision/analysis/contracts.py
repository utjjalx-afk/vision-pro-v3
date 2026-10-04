"""Independent research contracts; no lane, order or LLM control dependencies."""

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum

from vision.core.contracts import CanonicalMarketEvent, _identifier, _utc
from vision.core.instruments import InstrumentRecord, digest
from vision.core.state.portfolio import DataQuality


class Lane(StrEnum):
    TECHNICAL = "technical"
    FLOW = "flow"
    MACRO = "macro"
    NARRATIVE = "narrative"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class Bias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


def number(value):
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or len(value.as_tuple().digits) > 64
        or abs(value.as_tuple().exponent) > 100
    ):
        raise ValueError("Bounded finite Decimal required")


@contextmanager
def arithmetic():
    # Ratios intentionally round under a fixed context, independent of the caller.
    with localcontext(Context(prec=256, rounding=ROUND_HALF_EVEN, Emax=4096, Emin=-4096)):
        yield


def wire(value):
    if isinstance(value, InstrumentRecord):
        return value.to_dict()
    if isinstance(value, CanonicalMarketEvent):
        return value.to_dict()
    if isinstance(value, timedelta):
        return value // timedelta(microseconds=1)
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [wire(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class Observation:
    """Canonical supplemental observation. Fixture signals are never live facts.

    Funding uses rate fractions; OI uses explicit base/contract quantity units.
    Macro/narrative fixtures declare a signed research signal, not inferred news.
    """

    event_id: str
    provider: str
    source_epoch: int
    instrument_id: str
    kind: str
    metric: str
    value: Decimal
    unit: str
    source_ts: datetime
    received_ts: datetime
    fixture_only: bool = False

    def __post_init__(self):
        for value in (self.event_id, self.provider, self.instrument_id, self.metric, self.unit):
            _identifier(value)
        _utc(self.source_ts)
        _utc(self.received_ts)
        number(self.value)
        if type(self.source_epoch) is not int or self.source_epoch < 0:
            raise ValueError("Explicit observation epoch required")
        if type(self.fixture_only) is not bool:
            raise ValueError("Explicit fixture flag required")
        if self.kind not in {"macro", "narrative", "funding", "open_interest"}:
            raise ValueError("Unknown canonical observation kind")
        if self.kind in {"macro", "narrative"}:
            if not self.fixture_only or self.unit != "signed_signal" or not -1 <= self.value <= 1:
                raise ValueError("Macro/narrative support labelled research fixtures only")
        elif self.kind == "funding":
            if self.unit != "rate_fraction" or not -1 <= self.value <= 1:
                raise ValueError("Funding requires an explicit rate fraction")
        elif self.value < 0 or self.unit not in {"base_quantity", "contracts"}:
            raise ValueError("OI requires a nonnegative quantity and explicit unit")


@dataclass(frozen=True, slots=True)
class LanePolicy:
    min_bars: int = 5
    max_events: int = 512
    max_age: timedelta = timedelta(seconds=5)
    max_quality_age: timedelta = timedelta(seconds=5)
    observation_age: timedelta = timedelta(hours=1)

    def __post_init__(self):
        if type(self.min_bars) is not int or not 3 <= self.min_bars <= 512:
            raise ValueError("Bar window must be between 3 and 512")
        if type(self.max_events) is not int or not self.min_bars <= self.max_events <= 2048:
            raise ValueError("Bounded context capacity required")
        for value in (self.max_age, self.max_quality_age, self.observation_age):
            if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(days=7):
                raise ValueError("Bounded positive age policy required")


@dataclass(frozen=True, slots=True)
class LaneContext:
    instrument_id: str
    as_of: datetime
    events: tuple[CanonicalMarketEvent, ...]
    observations: tuple[Observation, ...]
    quality: DataQuality
    policy: LanePolicy = LanePolicy()

    def __post_init__(self):
        _identifier(self.instrument_id)
        _utc(self.as_of)
        if not isinstance(self.policy, LanePolicy) or not isinstance(self.quality, DataQuality):
            raise ValueError("Explicit context policy and quality required")
        if type(self.events) is not tuple or type(self.observations) is not tuple:
            raise ValueError("Immutable canonical inputs required")
        if len(self.events) > self.policy.max_events or len(self.observations) > 256:
            raise ValueError("Context capacity exceeded")
        if any(not isinstance(item, CanonicalMarketEvent) for item in self.events):
            raise ValueError("Canonical market events required")
        if any(not isinstance(item, Observation) for item in self.observations):
            raise ValueError("Canonical observations required")
        identities = set()
        for item in self.events + self.observations:
            if not isinstance(item, (CanonicalMarketEvent, Observation)):
                raise ValueError("Canonical inputs only")
            if item.instrument_id != self.instrument_id:
                raise ValueError("Mixed instrument contexts are forbidden")
            provider = item.source if isinstance(item, CanonicalMarketEvent) else item.provider
            identity = (provider, item.event_id)
            if identity in identities:
                raise ValueError("Duplicate context event")
            identities.add(identity)
            if item.source_ts > self.as_of or item.received_ts > self.as_of:
                raise ValueError("Future/lookahead context input")
            if item.source_ts > item.received_ts:
                raise ValueError("Receipt precedes source timestamp")
            if isinstance(item, CanonicalMarketEvent):
                for field in fields(item.payload):
                    value = getattr(item.payload, field.name)
                    if isinstance(value, Decimal):
                        number(value)
        if self.quality.as_of > self.as_of:
            raise ValueError("Future quality snapshot")

    @property
    def lineage_id(self):
        return digest(wire(self))


@dataclass(frozen=True, slots=True)
class Evidence:
    market_event_id: str
    source_epoch: int
    provider: str
    source_ts: datetime
    received_ts: datetime
    role: str
    fixture_only: bool

    def __post_init__(self):
        for value in (self.market_event_id, self.provider, self.role):
            _identifier(value)
        _utc(self.source_ts)
        _utc(self.received_ts)
        if (
            type(self.source_epoch) is not int
            or self.source_epoch < 0
            or type(self.fixture_only) is not bool
            or self.source_ts > self.received_ts
        ):
            raise ValueError("Invalid evidence provenance")


@dataclass(frozen=True, slots=True)
class Feature:
    name: str
    value: Decimal | None
    unit: str
    reason: str | None = None

    def __post_init__(self):
        _identifier(self.name)
        _identifier(self.unit)
        if self.value is None:
            _identifier(self.reason)
        elif (
            not isinstance(self.value, Decimal)
            or not self.value.is_finite()
            or self.reason is not None
        ):
            raise ValueError("Feature requires finite Decimal or an unavailable reason")


@dataclass(frozen=True, slots=True)
class LaneAssessment:
    assessment_id: str
    lane: Lane
    instrument_id: str
    timestamp: datetime
    status: Availability
    bias: Bias | None
    score: Decimal | None
    confidence: Decimal | None
    evidence: tuple[Evidence, ...]
    features: tuple[Feature, ...]
    data_quality: DataQuality
    reasons: tuple[str, ...]
    context_lineage: str
    fixture_only: bool
    algorithm_version: str = "phase5-v1"

    def __post_init__(self):
        _utc(self.timestamp)
        for value in (self.assessment_id, self.instrument_id, self.context_lineage):
            _identifier(value)
        if (
            not isinstance(self.data_quality, DataQuality)
            or type(self.fixture_only) is not bool
            or self.algorithm_version != "phase5-v1"
        ):
            raise ValueError("Explicit assessment provenance required")
        if not isinstance(self.lane, Lane) or not isinstance(self.status, Availability):
            raise ValueError("Explicit lane/status required")
        if any(type(value) is not tuple for value in (self.evidence, self.features, self.reasons)):
            raise ValueError("Immutable assessment fields required")
        if any(not isinstance(item, Evidence) for item in self.evidence) or any(
            not isinstance(item, Feature) for item in self.features
        ):
            raise ValueError("Typed immutable evidence/features required")
        for reason in self.reasons:
            _identifier(reason)
        if self.status is Availability.UNAVAILABLE:
            if any(value is not None for value in (self.bias, self.score, self.confidence)):
                raise ValueError("Unavailable cannot fabricate neutral scores")
            if not self.reasons:
                raise ValueError("Unavailable requires reasons")
        else:
            if not isinstance(self.bias, Bias):
                raise ValueError("Available requires bias")
            for value in (self.score, self.confidence):
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise ValueError("Finite assessment values required")
            if not -1 <= self.score <= 1 or not 0 <= self.confidence <= 1:
                raise ValueError("Assessment values out of range")
            expected = (
                Bias.BULLISH if self.score > 0 else Bias.BEARISH if self.score < 0 else Bias.NEUTRAL
            )
            if self.bias is not expected:
                raise ValueError("Bias and score disagree")

    def to_dict(self):
        return wire(self)
