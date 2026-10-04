"""Closed-bar trend/momentum/volatility descriptors; no forecast or trading edge claim."""

from decimal import Decimal

from vision.analysis.assessment import assessment
from vision.analysis.context import (
    confidence,
    evidence,
    quality_reasons,
    select_events,
    stream_reasons,
)
from vision.analysis.contracts import Feature, Lane, arithmetic
from vision.core.contracts import EventType


def assess(context):
    events = select_events(context, EventType.BAR)[-context.policy.min_bars :]
    reasons = quality_reasons(context) + stream_reasons(context, events, bars=True)
    if len(events) < context.policy.min_bars:
        reasons += ("INSUFFICIENT_BARS",)
    ev = evidence(events, "closed_bar")
    if reasons:
        return assessment(
            context, Lane.TECHNICAL, evidence=ev, reasons=tuple(dict.fromkeys(reasons))
        )
    with arithmetic():
        closes = [event.payload.close for event in events]
        returns = [(b - a) / a for a, b in zip(closes, closes[1:], strict=False)]
        trend = (closes[-1] - closes[0]) / closes[0]
        momentum = returns[-1]
        volatility = sum((abs(value) for value in returns), Decimal(0)) / len(returns)
        score = trend / (1 + abs(trend))
        features = (
            Feature("window_return", trend, "fraction"),
            Feature("last_return", momentum, "fraction"),
            Feature("mean_absolute_return", volatility, "fraction"),
        )
        return assessment(
            context,
            Lane.TECHNICAL,
            score=score,
            confidence=confidence(context, events),
            evidence=ev,
            features=features,
        )
