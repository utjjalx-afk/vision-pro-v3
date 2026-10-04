"""Prospective directional benchmark grading; no execution or trading-edge claims."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock

from vision.analysis.contracts import Availability, LaneAssessment, arithmetic, number, wire
from vision.core.contracts import CanonicalMarketEvent, EventType, TimestampBasis, _identifier, _utc
from vision.core.instruments import digest
from vision.core.state.portfolio import DataQuality

MARKET_ROLES = frozenset({"trade", "closed_bar", "best_level_quote", "aggressor_trade"})


def duration(value):
    if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(days=7):
        raise ValueError("Bounded positive duration required")


def fraction(value):
    number(value)
    if not 0 <= value <= 1:
        raise ValueError("Fraction must be between zero and one")


def healthy(quality, instrument, at, ttl):
    return (
        isinstance(quality, DataQuality)
        and not quality.divergent
        and dict(quality.states).get(instrument) == "healthy"
        and timedelta(0) <= at - quality.as_of <= ttl
    )


def trade(event):
    if (
        not isinstance(event, CanonicalMarketEvent)
        or event.event_type is not EventType.TRADE
        or event.delivery_kind != "live"
        or event.continuity == "unverified"
        or event.timestamp_basis is not TimestampBasis.EXCHANGE
        or event.payload.quantity <= 0
        or event.source_ts > event.received_ts
    ):
        raise ValueError("Live canonical trade benchmark with verified continuity required")
    for value in (event.payload.price, event.payload.quantity):
        number(value)


@dataclass(frozen=True, slots=True)
class GradingPolicy:
    horizon: timedelta = timedelta(minutes=1)
    max_age: timedelta = timedelta(seconds=5)
    terminal_tolerance: timedelta = timedelta(seconds=5)
    minimum_move: Decimal = Decimal("0.001")

    def __post_init__(self):
        for value in (self.horizon, self.max_age, self.terminal_tolerance):
            duration(value)
        fraction(self.minimum_move)


@dataclass(frozen=True, slots=True)
class ForecastCommitment:
    assessment: LaneAssessment
    baseline: CanonicalMarketEvent
    registered_at: datetime
    policy: GradingPolicy
    regime: str | None = None
    fixture_only: bool = False

    def __post_init__(self):
        _utc(self.registered_at)
        if not isinstance(self.assessment, LaneAssessment) or not isinstance(
            self.policy, GradingPolicy
        ):
            raise ValueError("Typed prospective forecast required")
        if type(self.fixture_only) is not bool:
            raise ValueError("Explicit fixture designation required")
        if self.regime is not None:
            _identifier(self.regime)
        item = self.assessment
        if (
            item.status is not Availability.AVAILABLE
            or item.score == 0
            or item.confidence <= 0
            or item.fixture_only
        ):
            raise ValueError(
                "Prospective grading requires an available non-fixture directional assessment"
            )
        trade(self.baseline)
        if (
            self.baseline.instrument_id != item.instrument_id
            or not timedelta(0) <= self.registered_at - item.timestamp <= self.policy.max_age
            or not timedelta(0)
            <= self.registered_at - self.baseline.source_ts
            <= self.policy.max_age
            or not timedelta(0)
            <= self.registered_at - self.baseline.received_ts
            <= self.policy.max_age
            or not healthy(
                item.data_quality, item.instrument_id, self.registered_at, self.policy.max_age
            )
        ):
            raise ValueError("Forecast requires contemporaneous healthy data")
        if not item.evidence or any(
            e.source_ts > item.timestamp or e.received_ts > item.timestamp for e in item.evidence
        ):
            raise ValueError("Forecast requires non-lookahead assessment evidence")
        if any(
            (e.provider != self.baseline.source or e.source_epoch != self.baseline.source_epoch)
            for e in item.evidence
            if e.role in MARKET_ROLES
        ):
            raise ValueError("Benchmark and lane evidence must share provider/epoch")
        synthetic = self.baseline.source.startswith("synthetic.") or any(
            e.fixture_only or e.provider.startswith("synthetic.") for e in item.evidence
        )
        if synthetic and not self.fixture_only:
            raise ValueError("Synthetic outcomes must be explicitly fixtures")

    @property
    def target_at(self):
        return self.registered_at + self.policy.horizon

    @property
    def commitment_id(self):
        return digest(wire(self))


@dataclass(frozen=True, slots=True)
class GradedOutcome:
    commitment: ForecastCommitment
    terminal: CanonicalMarketEvent
    graded_at: datetime
    quality: DataQuality

    def __post_init__(self):
        _utc(self.graded_at)
        if not isinstance(self.commitment, ForecastCommitment):
            raise ValueError("Prospective commitment required")
        trade(self.terminal)
        c, t = self.commitment, self.terminal
        if (
            t.instrument_id != c.baseline.instrument_id
            or t.source != c.baseline.source
            or t.source_epoch != c.baseline.source_epoch
            or t.sequence <= c.baseline.sequence
            or t.event_id == c.baseline.event_id
            or not c.target_at <= t.source_ts <= c.target_at + c.policy.terminal_tolerance
            or not timedelta(0) <= self.graded_at - t.received_ts <= c.policy.max_age
            or t.source_ts > t.received_ts
            or t.received_ts - t.source_ts > c.policy.max_age
            or not healthy(self.quality, t.instrument_id, self.graded_at, c.policy.max_age)
        ):
            raise ValueError(
                "Outcome requires healthy on-time future evidence from the pinned source"
            )

    @property
    def benchmark_return(self):
        with arithmetic():
            return (
                self.terminal.payload.price - self.commitment.baseline.payload.price
            ) / self.commitment.baseline.payload.price

    @property
    def reward(self):
        with arithmetic():
            signed = self.benchmark_return * (1 if self.commitment.assessment.score > 0 else -1)
            threshold = self.commitment.policy.minimum_move
            return (
                Decimal(1)
                if signed > threshold
                else Decimal(0)
                if signed < -threshold
                else Decimal("0.5")
            )

    @property
    def outcome_id(self):
        return digest(wire(self))


def validate_history(outcomes):
    if type(outcomes) is not tuple or len(outcomes) > 10000:
        raise ValueError("Bounded immutable outcome history required")
    identities, terminal_ids, baseline_ids, windows = set(), set(), set(), {}
    for outcome in outcomes:
        if not isinstance(outcome, GradedOutcome):
            raise ValueError("Typed prospective outcomes required")
        c = outcome.commitment
        key = (c.assessment.lane, c.assessment.instrument_id)
        identity = (c.assessment.lane, c.assessment.assessment_id)
        terminal = (*key, outcome.terminal.source, outcome.terminal.event_id)
        baseline = (*key, c.baseline.source, c.baseline.event_id)
        if identity in identities or terminal in terminal_ids or baseline in baseline_ids:
            raise ValueError("Duplicate forecast/benchmark cannot inflate samples")
        prior = windows.get(key)
        if prior is not None and (
            c.registered_at < prior.target_at or c.assessment.timestamp < prior.target_at
        ):
            raise ValueError("Overlapping/reordered outcome windows cannot inflate samples")
        identities.add(identity)
        terminal_ids.add(terminal)
        baseline_ids.add(baseline)
        windows[key] = c


class ProspectiveLedger:
    """Single-process registrations precede grades; clock injection supports replay research.

    Export immutable records for replay. This ledger does not authenticate caller data
    or provide durable registration receipts; it is not a production Outcome service.
    """

    def __init__(self, *, clock=lambda: datetime.now(UTC)):
        self.clock = clock
        self._commitments = {}
        self._outcomes = {}
        self._last_at = None
        self._lock = RLock()

    def _time(self):
        at = self.clock()
        _utc(at)
        if self._last_at is not None and at < self._last_at:
            raise ValueError("Prospective clock regression")
        return at

    def register(self, assessment, baseline, *, policy=None, regime=None, fixture_only=False):
        with self._lock:
            policy = policy or GradingPolicy()
            if not isinstance(assessment, LaneAssessment):
                raise ValueError("LaneAssessment required")
            at = self._time()
            key = assessment.assessment_id
            previous = self._commitments.get(key)
            if previous is not None:
                if (
                    previous.assessment != assessment
                    or previous.baseline != baseline
                    or previous.policy != policy
                    or previous.regime != regime
                    or previous.fixture_only != fixture_only
                ):
                    raise ValueError("Forecast identity collision")
                return previous
            if len(self._commitments) >= 10000:
                raise ValueError("Prospective ledger capacity exceeded")
            commitment = ForecastCommitment(assessment, baseline, at, policy, regime, fixture_only)
            for prior in self._commitments.values():
                if (
                    prior.assessment.lane == assessment.lane
                    and prior.assessment.instrument_id == assessment.instrument_id
                    and (at < prior.target_at or assessment.timestamp < prior.target_at)
                ):
                    raise ValueError("Prospective windows must not overlap")
            self._commitments[key] = commitment
            self._last_at = at
            return commitment

    def grade(self, commitment_id, terminal, quality):
        with self._lock:
            at = self._time()
            commitment = next(
                (c for c in self._commitments.values() if c.commitment_id == commitment_id), None
            )
            if commitment is None:
                raise ValueError("Unregistered forecast cannot be graded")
            previous = self._outcomes.get(commitment_id)
            if previous is not None:
                if previous.terminal != terminal or previous.quality != quality:
                    raise ValueError("Outcome cannot be revised after grading")
                return previous
            outcome = GradedOutcome(commitment, terminal, at, quality)
            values = tuple(
                sorted(
                    (*self._outcomes.values(), outcome),
                    key=lambda g: (g.commitment.registered_at, g.commitment.assessment.lane.value),
                )
            )
            validate_history(values)
            self._outcomes[commitment_id] = outcome
            self._last_at = at
            return outcome

    def snapshot(self, as_of):
        _utc(as_of)
        with self._lock:
            return tuple(
                sorted(
                    (g for g in self._outcomes.values() if g.graded_at <= as_of),
                    key=lambda g: (g.commitment.registered_at, g.commitment.assessment.lane.value),
                )
            )
