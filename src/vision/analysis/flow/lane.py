"""Window CVD and best-level OFI; no full-depth or futures feed is implied."""

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
    with arithmetic():
        return _assess(context)


def _assess(context):
    trades = select_events(context, EventType.TRADE)
    quotes = select_events(context, EventType.QUOTE)
    features, direction = [], None
    ev = evidence(trades, "aggressor_trade") + evidence(quotes, "best_level_quote")
    trade_reason = stream_reasons(context, trades)
    if not trade_reason and any(item.payload.buyer_is_maker is None for item in trades):
        trade_reason = ("UNKNOWN_AGGRESSOR",)
    if not trade_reason:
        cvd = sum(
            (
                -item.payload.quantity if item.payload.buyer_is_maker else item.payload.quantity
                for item in trades
            ),
            Decimal(0),
        )
        volume = sum((item.payload.quantity for item in trades), Decimal(0))
        if volume:
            direction = cvd / volume
            features += [
                Feature("window_cvd", cvd, "canonical_quantity"),
                Feature("trade_imbalance", direction, "fraction"),
            ]
        else:
            trade_reason = ("ZERO_TRADE_VOLUME",)
    if trade_reason:
        features += [
            Feature("window_cvd", None, "canonical_quantity", trade_reason[0]),
            Feature("trade_imbalance", None, "fraction", trade_reason[0]),
        ]
    quote_reason = stream_reasons(context, quotes)
    if not quote_reason:
        latest = quotes[-1].payload
        total = latest.bid_quantity + latest.ask_quantity
        book = (latest.bid_quantity - latest.ask_quantity) / total if total else None
        features.append(
            Feature("book_imbalance", book, "fraction", None if total else "ZERO_BOOK_QUANTITY")
        )
        if direction is None:
            direction = book
        if len(quotes) >= 2:
            ofi = Decimal(0)
            for a, b in zip(quotes, quotes[1:], strict=False):
                a, b = a.payload, b.payload
                ofi += (
                    (b.bid_quantity if b.bid >= a.bid else 0)
                    - (a.bid_quantity if b.bid <= a.bid else 0)
                    - (b.ask_quantity if b.ask <= a.ask else 0)
                    + (a.ask_quantity if b.ask >= a.ask else 0)
                )
            features.append(Feature("best_level_ofi", ofi, "canonical_quantity"))
        else:
            features.append(
                Feature("best_level_ofi", None, "canonical_quantity", "INSUFFICIENT_QUOTES")
            )
    else:
        features += [
            Feature("book_imbalance", None, "fraction", quote_reason[0]),
            Feature("best_level_ofi", None, "canonical_quantity", quote_reason[0]),
        ]
    for kind in ("funding", "open_interest"):
        items = tuple(item for item in context.observations if item.kind == kind)
        ev += evidence(items, kind)
        reason = "MISSING_OBSERVATION"
        if items:
            latest = items[-1]
            valid = (
                len({(item.provider, item.source_epoch, item.unit) for item in items}) == 1
                and all(
                    a.source_ts < b.source_ts and a.received_ts <= b.received_ts
                    for a, b in zip(items, items[1:], strict=False)
                )
                and context.as_of - latest.source_ts <= context.policy.observation_age
                and context.as_of - latest.received_ts <= context.policy.observation_age
            )
            if valid:
                features.append(Feature(kind, latest.value, latest.unit))
                continue
            reason = "INVALID_OBSERVATION_WINDOW"
        features.append(Feature(kind, None, "unavailable", reason))
    reasons = quality_reasons(context)
    if len({(item.source, item.source_epoch) for item in trades + quotes}) > 1:
        reasons += ("SOURCE_TRANSITION_REQUIRES_NEW_WINDOW",)
    if direction is None:
        reasons += ("NO_DIRECTIONAL_FLOW_INPUT",)
    fixture = any(item.fixture_only for item in ev)
    return assessment(
        context,
        Lane.FLOW,
        score=direction,
        confidence=confidence(context, trades + quotes),
        evidence=ev,
        features=tuple(features),
        reasons=reasons,
        fixture_only=fixture,
    )
