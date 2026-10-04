"""Reliability-gated contributions and fail-closed WAIT; never an order submission."""

from decimal import Decimal

from vision.analysis.contracts import Availability, arithmetic, wire
from vision.analysis.synthesizer.contracts import (
    Contribution,
    Direction,
    SynthesisDecision,
    SynthesisInput,
)
from vision.analysis.synthesizer.reliability import ReliabilityState, revision
from vision.core.instruments import digest
from vision.outcomes.prospective import MARKET_ROLES, healthy


def synthesize(inputs):
    if not isinstance(inputs, SynthesisInput):
        raise ValueError("SynthesisInput required")
    with arithmetic():
        return _synthesize(inputs)


def _synthesize(inputs):
    instrument, at, policy = inputs.record.spec.instrument_id, inputs.as_of, inputs.policy
    reasons, contributions = [], []
    if not healthy(inputs.quality, instrument, at, policy.max_age):
        reasons.append("POOR_DATA_QUALITY")
    if at - inputs.market_timestamp >= policy.max_age:
        reasons.append("STALE_ASSESSMENTS")
    if at - inputs.record.provenance.observed_at > policy.max_spec_age:
        reasons.append("STALE_INSTRUMENT_SPEC")
    market_sources = {
        (e.provider, e.source_epoch)
        for item in inputs.assessments
        if item.status is Availability.AVAILABLE
        for e in item.evidence
        if e.role in MARKET_ROLES
    }
    if len(market_sources) > 1:
        reasons.append("SOURCE_TRANSITION")
    for item in sorted(inputs.assessments, key=lambda item: item.lane.value):
        reliability = revision(
            inputs.outcomes,
            item.lane,
            instrument,
            inputs.market_timestamp,
            regime=inputs.regime,
            policy=inputs.reliability_policy,
        )
        exclusion = None
        if item.status is Availability.UNAVAILABLE:
            exclusion = "UNAVAILABLE"
        elif item.fixture_only or any(
            e.fixture_only or e.provider.startswith("synthetic.") for e in item.evidence
        ):
            exclusion = "FIXTURE_ONLY"
        elif not item.evidence:
            exclusion = "MISSING_EVIDENCE"
        elif not healthy(inputs.quality, instrument, at, policy.max_age) or not healthy(
            item.data_quality, instrument, at, policy.max_age
        ):
            exclusion = "POOR_LANE_QUALITY"
            reasons.append("POOR_DATA_QUALITY")
        elif reliability.state is not ReliabilityState.ELIGIBLE:
            exclusion = reliability.state.value
        elif item.confidence <= 0:
            exclusion = "ZERO_CONFIDENCE"
        weight = None if exclusion else item.confidence * reliability.eligible_reliability
        signed = None if exclusion else item.score * weight
        contributions.append(
            Contribution(
                item.assessment_id,
                digest(wire(item.evidence)),
                reliability,
                None if exclusion else Decimal(1),
                weight,
                signed,
                exclusion,
            )
        )
    included = tuple(c for c in contributions if c.exclusion is None)
    score = support = None
    if len(included) < policy.minimum_lanes:
        reasons.append("INSUFFICIENT_ELIGIBLE_LANES")
    if included:
        weights = sum((c.weight for c in included), Decimal(0))
        signed = sum((c.signed_contribution for c in included), Decimal(0))
        score = signed / weights
        support = weights / len(included)
        if support < policy.minimum_support:
            reasons.append("LOW_SUPPORT")
        if max(c.weight for c in included) / weights > policy.maximum_lane_share:
            reasons.append("LANE_DOMINANCE")
        positive = sum((max(c.signed_contribution, Decimal(0)) for c in included), Decimal(0))
        negative = sum((max(-c.signed_contribution, Decimal(0)) for c in included), Decimal(0))
        mass = positive + negative
        if (
            mass
            and max(abs(c.signed_contribution) for c in included) / mass > policy.maximum_lane_share
        ):
            reasons.append("LANE_DOMINANCE")
        if mass and min(positive, negative) / mass > policy.maximum_disagreement:
            reasons.append("DISAGREEMENT")
        if abs(score) < policy.minimum_direction:
            reasons.append("LOW_DIRECTIONAL_SUPPORT")
    reasons = tuple(dict.fromkeys(reasons))
    direction = Direction.WAIT if reasons else Direction.LONG if score > 0 else Direction.SHORT
    source = digest(
        [wire(item.evidence) for item in sorted(inputs.assessments, key=lambda a: a.lane.value)]
    )
    identity = digest(
        {
            "input": inputs.lineage_id,
            "version": "phase6-v1",
            "direction": direction.value,
            "contributions": wire(tuple(contributions)),
            "reasons": wire(reasons),
            "score": wire(score),
            "confidence": wire(support),
        }
    )
    return SynthesisDecision(
        identity,
        direction,
        instrument,
        inputs.record.canonical.key,
        at,
        inputs.market_timestamp,
        score,
        support,
        tuple(contributions),
        reasons,
        inputs.quality,
        inputs.lineage_id,
        source,
        inputs.record.revision,
        inputs.market_timestamp + policy.max_age,
    )
