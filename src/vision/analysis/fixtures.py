"""Macro/narrative interface exercised only by explicitly labelled replay signals."""

from vision.analysis.assessment import assessment
from vision.analysis.context import evidence, quality_reasons
from vision.analysis.contracts import Feature


def assess_fixture(context, lane):
    items = tuple(item for item in context.observations if item.kind == lane.value)
    reasons = quality_reasons(context)
    if not items:
        reasons += ("NO_CANONICAL_FIXTURE",)
    elif len(items) != 1:
        reasons += ("AMBIGUOUS_FIXTURE_SIGNALS",)
    elif (
        context.as_of - items[0].source_ts > context.policy.observation_age
        or context.as_of - items[0].received_ts > context.policy.observation_age
    ):
        reasons += ("STALE_OBSERVATION",)
    features = tuple(Feature(item.metric, item.value, item.unit) for item in items)
    # Declared fixture signal is replayed, not inferred or calibrated.
    from decimal import Decimal

    return assessment(
        context,
        lane,
        score=items[0].value if len(items) == 1 else None,
        confidence=Decimal(0),
        evidence=evidence(items, "declared_replay_signal"),
        features=features,
        reasons=reasons,
        fixture_only=True,
    )
