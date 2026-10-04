"""Prospective support with sample gates and neutral shrinkage."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from vision.analysis.contracts import Lane, arithmetic, number, wire
from vision.core.contracts import _utc
from vision.core.instruments import digest
from vision.outcomes.prospective import GradingPolicy, validate_history


class ReliabilityState(StrEnum):
    UNPROVEN = "UNPROVEN"
    NO_SUPPORT = "NO_SUPPORT"
    ELIGIBLE = "ELIGIBLE"


@dataclass(frozen=True, slots=True)
class ReliabilityPolicy:
    minimum_samples: int = 30
    regime_minimum_samples: int = 50
    prior_strength: Decimal = Decimal(20)
    grading_policy: GradingPolicy = GradingPolicy()

    def __post_init__(self):
        if not isinstance(self.grading_policy, GradingPolicy):
            raise ValueError("Pinned prospective grading protocol required")
        for value in (self.minimum_samples, self.regime_minimum_samples):
            if type(value) is not int or not 10 <= value <= 10000:
                raise ValueError("Minimum samples must be an integer from 10 through 10000")
        number(self.prior_strength)
        if not 10 <= self.prior_strength <= 10000:
            raise ValueError("Neutral prior strength must be at least ten samples")


@dataclass(frozen=True, slots=True)
class ReliabilityRevision:
    revision_id: str
    lane: Lane
    instrument_id: str
    regime: str | None
    as_of: datetime
    state: ReliabilityState
    samples: int
    posterior_success: Decimal
    eligible_reliability: Decimal | None
    outcome_ids: tuple[str, ...]

    def __post_init__(self):
        _utc(self.as_of)
        if (
            not isinstance(self.lane, Lane)
            or not isinstance(self.state, ReliabilityState)
            or type(self.outcome_ids) is not tuple
            or type(self.samples) is not int
            or self.samples != len(self.outcome_ids)
            or self.samples < 0
            or not isinstance(self.posterior_success, Decimal)
            or not self.posterior_success.is_finite()
            or not 0 <= self.posterior_success <= 1
        ):
            raise ValueError("Immutable reliability evidence required")
        if self.state is ReliabilityState.ELIGIBLE:
            if self.eligible_reliability is None or not 0 < self.eligible_reliability <= 1:
                raise ValueError("Positive eligible reliability required")
        elif self.eligible_reliability is not None:
            raise ValueError("Unproven/unsupported reliability cannot contribute")


def revision(outcomes, lane, instrument_id, as_of, *, regime=None, policy=None):
    policy = policy or ReliabilityPolicy()
    _utc(as_of)
    validate_history(outcomes)
    if not isinstance(lane, Lane) or not isinstance(policy, ReliabilityPolicy):
        raise ValueError("Typed reliability policy and lane required")
    selected = tuple(
        g
        for g in outcomes
        if g.graded_at <= as_of
        and g.commitment.assessment.lane is lane
        and g.commitment.assessment.instrument_id == instrument_id
        and not g.commitment.fixture_only
        and g.commitment.policy == policy.grading_policy
        and (regime is None or g.commitment.regime == regime)
    )
    with arithmetic():
        n = len(selected)
        posterior = (sum((g.reward for g in selected), Decimal(0)) + policy.prior_strength / 2) / (
            n + policy.prior_strength
        )
        minimum = policy.regime_minimum_samples if regime is not None else policy.minimum_samples
        state = (
            ReliabilityState.UNPROVEN
            if n < minimum
            else ReliabilityState.ELIGIBLE
            if posterior > Decimal("0.5")
            else ReliabilityState.NO_SUPPORT
        )
        eligible = (
            max(Decimal(0), 2 * posterior - 1) if state is ReliabilityState.ELIGIBLE else None
        )
        ids = tuple(g.outcome_id for g in selected)
        identity = digest(
            {
                "lane": lane.value,
                "instrument": instrument_id,
                "regime": regime,
                "as_of": _utc(as_of),
                "policy": wire(policy),
                "outcomes": list(ids),
                "version": "phase6-reliability-v1",
            }
        )
        return ReliabilityRevision(
            identity, lane, instrument_id, regime, as_of, state, n, posterior, eligible, ids
        )
