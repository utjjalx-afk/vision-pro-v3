"""Build immutable assessments with deterministic identities, without lane weights."""

from vision.analysis.contracts import Availability, Bias, LaneAssessment, wire
from vision.core.instruments import digest


def assessment(
    context,
    lane,
    *,
    score=None,
    confidence=None,
    evidence=(),
    features=(),
    reasons=(),
    fixture_only=False,
):
    status = Availability.UNAVAILABLE if reasons else Availability.AVAILABLE
    if reasons:
        score = confidence = bias = None
    else:
        bias = Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL
    identity = digest(
        {
            "context": context.lineage_id,
            "lane": lane.value,
            "version": "phase5-v1",
            "score": wire(score),
            "confidence": wire(confidence),
            "features": wire(features),
            "reasons": wire(reasons),
            "fixture_only": fixture_only,
        }
    )
    return LaneAssessment(
        identity,
        lane,
        context.instrument_id,
        context.as_of,
        status,
        bias,
        score,
        confidence,
        evidence,
        features,
        context.quality,
        reasons,
        context.lineage_id,
        fixture_only,
    )
