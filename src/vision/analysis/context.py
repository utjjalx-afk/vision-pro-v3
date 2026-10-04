"""Pure context selection and owning-loop Phase-2 quality capture."""

from datetime import timedelta

from vision.analysis.contracts import Evidence, LaneContext, LanePolicy
from vision.core.contracts import BarPayload, EventType, TimestampBasis
from vision.core.state.portfolio import DataQuality


def quality_reasons(context):
    reasons = []
    if context.quality.divergent:
        reasons.append("DATA_DIVERGENT")
    age = context.as_of - context.quality.as_of
    if age > context.policy.max_quality_age:
        reasons.append("STALE_QUALITY")
    states = dict(context.quality.states)
    state = states.get(context.instrument_id)
    if state not in {"healthy", "degraded"}:
        reasons.append(f"DATA_{state.upper()}" if state else "MISSING_QUALITY")
    return tuple(reasons)


def select_events(context, kind):
    return tuple(event for event in context.events if event.event_type is kind)


def stream_reasons(context, events, *, bars=False):
    if not events:
        return ("MISSING_INPUT",)
    if len({(event.source, event.source_epoch) for event in events}) != 1:
        return ("SOURCE_TRANSITION_REQUIRES_NEW_WINDOW",)
    if any(event.continuity == "unverified" for event in events):
        return ("UNVERIFIED_CONTINUITY",)
    if events[0].event_type is EventType.TRADE and events[0].source == "binance.spot":
        if any(b.sequence != a.sequence + 1 for a, b in zip(events, events[1:], strict=False)):
            return ("TRADE_GAP",)
    if any(
        a.sequence >= b.sequence or a.source_ts > b.source_ts or a.received_ts > b.received_ts
        for a, b in zip(events, events[1:], strict=False)
    ):
        return ("OUT_OF_ORDER",)
    extra = timedelta(seconds=events[-1].payload.interval_seconds) if bars else timedelta(0)
    if (
        context.as_of - events[-1].source_ts > context.policy.max_age + extra
        or context.as_of - events[-1].received_ts > context.policy.max_age
    ):
        return ("STALE_INPUT",)
    if bars:
        if len({event.payload.interval_seconds for event in events}) != 1:
            return ("MIXED_BAR_INTERVAL",)
        for event in events:
            payload = event.payload
            if (
                not isinstance(payload, BarPayload)
                or payload.open_ts is None
                or payload.open_ts + timedelta(seconds=payload.interval_seconds) > context.as_of
                or event.source_ts < payload.open_ts
                or event.source_ts > payload.open_ts + timedelta(seconds=payload.interval_seconds)
            ):
                return ("UNVERIFIED_CLOSED_BAR",)
        if any(
            b.payload.open_ts - a.payload.open_ts != extra
            for a, b in zip(events, events[1:], strict=False)
        ):
            return ("BAR_GAP",)
    return ()


def evidence(events, role):
    return tuple(
        Evidence(
            item.event_id,
            item.source_epoch,
            getattr(item, "source", None) or item.provider,
            item.source_ts,
            item.received_ts,
            role,
            getattr(item, "fixture_only", False),
        )
        for item in events
    )


def confidence(context, events):
    # Completeness indicator only. This is not calibrated predictive probability.
    degraded = dict(context.quality.states).get(context.instrument_id) == "degraded"
    receipt = any(
        getattr(item, "timestamp_basis", None) is TimestampBasis.RECEIPT for item in events
    )
    from decimal import Decimal

    return Decimal("0.5") if degraded or receipt else Decimal(1)


def capture_from_hub(hub, instrument_id, events, *, observations=(), policy=None):
    """Capture on hub's owning loop. Inputs must come from caller's admitted history.

    This function captures health, not provider payloads. Historical replay should
    construct an explicit LaneContext with the quality recorded at that time.
    """
    if instrument_id not in hub.instruments and instrument_id not in hub.market_instruments:
        raise ValueError("Registered hub instrument required")
    at = hub.clock()
    statuses = hub.gate.snapshot(at)
    relevant = [value for key, value in sorted(statuses.items()) if f":{instrument_id}:" in key]
    state = (
        "warming_up"
        if not relevant
        else next(
            (value for value in relevant if value not in {"healthy", "degraded"}),
            "degraded" if "degraded" in relevant else "healthy",
        )
    )
    selector = getattr(hub, "failover", None)
    quality = DataQuality(
        at, ((instrument_id, state),), bool(selector and selector.divergent), "phase2-lane-context"
    )
    return LaneContext(
        instrument_id, at, tuple(events), tuple(observations), quality, policy or LanePolicy()
    )
