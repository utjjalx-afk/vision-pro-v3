import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from vision.analysis.context import capture_from_hub
from vision.analysis.contracts import (
    Availability,
    Bias,
    Lane,
    LaneContext,
    LanePolicy,
    Observation,
    wire,
)
from vision.analysis.flow.lane import assess as flow
from vision.analysis.macro.lane import assess as macro
from vision.analysis.narrative.lane import assess as narrative
from vision.analysis.replay import context_from_dict, replay
from vision.analysis.runner import assess_all
from vision.analysis.technical.lane import assess as technical
from vision.core.contracts import BarPayload, EventType, QuotePayload, TimestampBasis, TradePayload
from vision.core.state.portfolio import DataQuality


@pytest.fixture
def context(now, market_event):
    bars = []
    for i in range(5):
        close = Decimal(100 + i)
        opening = now - timedelta(seconds=(5 - i) * 60)
        closing = opening + timedelta(seconds=60)
        bars.append(
            replace(
                market_event,
                event_id=f"bar-{i}",
                event_type=EventType.BAR,
                sequence=i,
                source_ts=closing,
                received_ts=closing,
                payload=BarPayload(close, close + 1, close - 1, close, Decimal(5), 60, opening),
                continuity="contiguous",
            )
        )
    trades = tuple(
        replace(
            market_event,
            event_id=f"trade-{i}",
            sequence=i,
            source_ts=now - timedelta(seconds=2 - i),
            received_ts=now - timedelta(seconds=2 - i),
            payload=TradePayload(Decimal(104), Decimal(q), maker),
            continuity="contiguous",
        )
        for i, (q, maker) in enumerate((("3", False), ("1", True)))
    )
    quotes = tuple(
        replace(
            market_event,
            event_id=f"quote-{i}",
            event_type=EventType.QUOTE,
            sequence=i,
            source_ts=now - timedelta(seconds=1 - i),
            received_ts=now - timedelta(seconds=1 - i),
            payload=QuotePayload(Decimal(103), Decimal(104), Decimal(b), Decimal(a)),
            continuity="snapshot",
        )
        for i, (b, a) in enumerate((("3", "2"), ("5", "1")))
    )
    return LaneContext(
        market_event.instrument_id,
        now,
        tuple(bars) + trades + quotes,
        (),
        DataQuality(now, ((market_event.instrument_id, "healthy"),)),
    )


def feature(result, name):
    return next(item for item in result.features if item.name == name)


def observation(context, kind="macro", value="0.2", **changes):
    defaults = dict(
        event_id=f"fixture-{kind}",
        provider="synthetic.replay",
        source_epoch=2,
        instrument_id=context.instrument_id,
        kind=kind,
        metric="fixture_signal",
        value=Decimal(value),
        unit="signed_signal",
        source_ts=context.as_of,
        received_ts=context.as_of,
        fixture_only=True,
    )
    defaults.update(changes)
    return Observation(**defaults)


def test_technical_features_known_vector(context):
    result = technical(context)
    assert result.status is Availability.AVAILABLE and result.bias is Bias.BULLISH
    assert feature(result, "window_return").value == Decimal("0.04")
    with localcontext() as ctx:
        ctx.prec = 256
        assert result.score == Decimal("0.04") / Decimal("1.04")
        assert feature(result, "last_return").value == Decimal(1) / Decimal(103)
    assert len(result.evidence) == 5
    assert all(item.market_event_id.startswith("bar-") for item in result.evidence)


def test_flow_known_vectors(context):
    result = flow(context)
    assert result.status is Availability.AVAILABLE and result.score == Decimal("0.5")
    assert feature(result, "window_cvd").value == 2
    assert feature(result, "trade_imbalance").value == Decimal("0.5")
    assert feature(result, "best_level_ofi").value == 3
    with localcontext() as ctx:
        ctx.prec = 256
        assert feature(result, "book_imbalance").value == Decimal(4) / Decimal(6)
    assert feature(result, "funding").value is None
    assert feature(result, "open_interest").reason == "MISSING_OBSERVATION"


@pytest.mark.parametrize(
    "price,expected", [("99", Bias.BEARISH), ("100", Bias.NEUTRAL), ("105", Bias.BULLISH)]
)
def test_technical_bias_is_descriptor(context, price, expected):
    bars = list(context.events[:5])
    close = Decimal(price)
    bars[-1] = replace(
        bars[-1],
        payload=replace(
            bars[-1].payload,
            close=close,
            high=max(close, Decimal(105)),
            low=min(close, Decimal(99)),
        ),
    )
    assert technical(replace(context, events=tuple(bars))).bias is expected


@pytest.mark.parametrize("lane", [technical, flow, macro, narrative])
@pytest.mark.parametrize("state", ["stale", "disconnected", "gap", "warming_up"])
def test_unhealthy_quality_blocks_every_lane(context, lane, state):
    result = lane(
        replace(context, quality=replace(context.quality, states=((context.instrument_id, state),)))
    )
    assert result.status is Availability.UNAVAILABLE
    assert result.score is result.bias is result.confidence is None
    assert f"DATA_{state.upper()}" in result.reasons
    assert dict(result.data_quality.states)[context.instrument_id] == state


@pytest.mark.parametrize("lane", [technical, flow, macro, narrative])
def test_divergence_propagates(context, lane):
    result = lane(replace(context, quality=replace(context.quality, divergent=True)))
    assert result.status is Availability.UNAVAILABLE and "DATA_DIVERGENT" in result.reasons
    assert result.data_quality.divergent


@pytest.mark.parametrize("lane", [technical, flow, macro, narrative])
def test_missing_inputs_are_not_neutral(context, lane):
    result = lane(replace(context, events=()))
    assert result.status is Availability.UNAVAILABLE
    assert result.bias is result.score is result.confidence is None


def test_degraded_and_receipt_confidence_not_probability(context):
    degraded = replace(
        context, quality=replace(context.quality, states=((context.instrument_id, "degraded"),))
    )
    assert technical(degraded).confidence == flow(degraded).confidence == Decimal("0.5")
    quotes = tuple(
        replace(item, timestamp_basis=TimestampBasis.RECEIPT)
        for item in context.events
        if item.event_type is EventType.QUOTE
    )
    assert flow(replace(context, events=quotes)).confidence == Decimal("0.5")


@pytest.mark.parametrize("kind,lane", [("macro", macro), ("narrative", narrative)])
def test_fixture_interface_explicit_provenance_zero_predictive_confidence(context, kind, lane):
    item = observation(context, kind)
    result = lane(replace(context, observations=(item,)))
    assert result.status is Availability.AVAILABLE and result.fixture_only
    assert result.score == item.value and result.confidence == 0
    assert result.evidence[0].market_event_id == item.event_id
    assert result.evidence[0].provider == item.provider
    assert result.evidence[0].source_epoch == item.source_epoch
    assert result.evidence[0].fixture_only


@pytest.mark.parametrize("kind", ["macro", "narrative"])
def test_no_live_or_llm_macro_narrative_facts(context, kind):
    with pytest.raises(ValueError):
        observation(context, kind, fixture_only=False)


def test_ambiguous_fixture_no_arbitrary_weights(context):
    first = observation(context)
    second = replace(first, event_id="another", value=Decimal("-0.3"))
    result = macro(replace(context, observations=(first, second)))
    assert result.status is Availability.UNAVAILABLE
    assert "AMBIGUOUS_FIXTURE_SIGNALS" in result.reasons


def test_stale_fixture(context):
    item = observation(context, source_ts=context.as_of - timedelta(hours=2))
    assert "STALE_OBSERVATION" in macro(replace(context, observations=(item,))).reasons


def test_funding_oi_interfaces_dont_infer_direction(context):
    funding = observation(context, "funding", "-0.001", unit="rate_fraction", fixture_only=False)
    oi = observation(context, "open_interest", "100", unit="contracts", fixture_only=False)
    inputs = replace(context, observations=(funding, oi))
    result = flow(inputs)
    assert feature(result, "funding").value == Decimal("-0.001")
    assert feature(result, "open_interest").value == 100
    assert feature(result, "open_interest").unit == "contracts"
    assert result.score == flow(context).score
    unavailable = flow(replace(inputs, events=()))
    assert unavailable.status is Availability.UNAVAILABLE


def test_unknown_aggressor_cannot_fabricate_cvd(context):
    trades = tuple(
        replace(item, payload=replace(item.payload, buyer_is_maker=None))
        for item in context.events
        if item.event_type is EventType.TRADE
    )
    result = flow(replace(context, events=trades))
    assert result.status is Availability.UNAVAILABLE
    assert feature(result, "window_cvd").reason == "UNKNOWN_AGGRESSOR"
    assert feature(result, "window_cvd").value is None


def test_quote_fallback_explicit_partial_features(context):
    quotes = tuple(item for item in context.events if item.event_type is EventType.QUOTE)
    result = flow(replace(context, events=quotes))
    assert result.score == feature(result, "book_imbalance").value
    assert feature(result, "window_cvd").reason == "MISSING_INPUT"


def test_zero_volume_and_book_fail_closed(context):
    events = tuple(
        replace(item, payload=replace(item.payload, quantity=Decimal(0)))
        if item.event_type is EventType.TRADE
        else replace(
            item, payload=replace(item.payload, bid_quantity=Decimal(0), ask_quantity=Decimal(0))
        )
        for item in context.events
        if item.event_type in {EventType.TRADE, EventType.QUOTE}
    )
    result = flow(replace(context, events=events))
    assert result.status is Availability.UNAVAILABLE
    assert feature(result, "trade_imbalance").reason == "ZERO_TRADE_VOLUME"
    assert feature(result, "book_imbalance").value is None


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("epoch", "SOURCE_TRANSITION_REQUIRES_NEW_WINDOW"),
        ("provider", "SOURCE_TRANSITION_REQUIRES_NEW_WINDOW"),
        ("order", "OUT_OF_ORDER"),
        ("gap", "BAR_GAP"),
        ("interval", "MIXED_BAR_INTERVAL"),
        ("open", "UNVERIFIED_CLOSED_BAR"),
        ("continuity", "UNVERIFIED_CONTINUITY"),
    ],
)
def test_technical_window_integrity(context, mutation, reason):
    events = list(context.events[:5])
    if mutation == "epoch":
        events[-1] = replace(events[-1], source_epoch=1)
    elif mutation == "provider":
        events[-1] = replace(events[-1], source="other.spot")
    elif mutation == "order":
        events[-1] = replace(events[-1], sequence=0)
    elif mutation == "gap":
        events[0] = replace(
            events[0],
            payload=replace(
                events[0].payload, open_ts=events[0].payload.open_ts - timedelta(seconds=60)
            ),
            source_ts=events[0].source_ts - timedelta(seconds=60),
            received_ts=events[0].received_ts - timedelta(seconds=60),
        )
    elif mutation == "interval":
        events[0] = replace(events[0], payload=replace(events[0].payload, interval_seconds=120))
    elif mutation == "open":
        events[-1] = replace(events[-1], payload=replace(events[-1].payload, open_ts=None))
    else:
        events[-1] = replace(events[-1], continuity="unverified")
    result = technical(replace(context, events=tuple(events)))
    assert result.status is Availability.UNAVAILABLE and reason in result.reasons


def test_window_reset_after_source_epoch(context):
    events = tuple(replace(item, source_epoch=3) for item in context.events)
    assert technical(replace(context, events=events)).status is Availability.AVAILABLE
    assert all(
        item.source_epoch == 3 for item in technical(replace(context, events=events)).evidence
    )


def test_stale_source_and_quality(context):
    stale = replace(context, as_of=context.as_of + timedelta(seconds=100))
    assert "STALE_INPUT" in technical(stale).reasons
    assert "STALE_QUALITY" in flow(stale).reasons
    missing = replace(context, quality=replace(context.quality, states=()))
    assert "MISSING_QUALITY" in technical(missing).reasons


@pytest.mark.parametrize(
    "mutation",
    [
        "future",
        "instrument",
        "duplicate",
        "raw",
        "mutable",
        "receipt",
        "observation_type",
        "event_type",
    ],
)
def test_context_rejects_bad_input(context, mutation):
    with pytest.raises(ValueError):
        if mutation == "future":
            replace(context, as_of=context.as_of - timedelta(seconds=1))
        elif mutation == "instrument":
            replace(context, events=(replace(context.events[0], instrument_id="OTHER"),))
        elif mutation == "duplicate":
            replace(context, events=(context.events[0], context.events[0]))
        elif mutation == "raw":
            replace(context, events=({"provider": "payload"},))
        elif mutation == "mutable":
            replace(context, events=list(context.events))
        elif mutation == "receipt":
            replace(
                context,
                events=(
                    replace(
                        context.events[-1],
                        received_ts=context.events[-1].source_ts - timedelta(seconds=1),
                    ),
                ),
            )
        elif mutation == "observation_type":
            replace(context, observations=(context.events[0],))
        else:
            replace(context, events=(observation(context),))


def test_context_and_assessment_immutable(context):
    with pytest.raises(FrozenInstanceError):
        context.events = ()
    result = technical(context)
    with pytest.raises(FrozenInstanceError):
        result.score = Decimal(0)
    with pytest.raises(FrozenInstanceError):
        result.evidence[0].source_epoch = 8
    with pytest.raises(ValueError):
        replace(result, status=Availability.UNAVAILABLE)


def test_independent_lanes_no_cross_assessment_input(context):
    result = assess_all(context)
    assert tuple(item.lane for item in result) == tuple(Lane)
    assert result == tuple(lane(context) for lane in (technical, flow, macro, narrative))
    fixture = replace(context, observations=(observation(context, value="-0.8"),))
    # Input lineage changes, but irrelevant data cannot alter technical/flow descriptors.
    for lane in (technical, flow):
        assert lane(context).features == lane(fixture).features
        assert lane(context).score == lane(fixture).score
    with pytest.raises(ValueError):
        assess_all(result)


def test_replay_exact_ids_and_decimal_context(context):
    payload = json.loads(json.dumps(wire(context)))
    expected = assess_all(context)
    with localcontext() as ctx:
        ctx.prec = 3
        actual = replay(payload, expected_lineage=context.lineage_id)
    assert actual == expected
    assert context_from_dict(payload) == context
    assert all(json.loads(json.dumps(item.to_dict())) for item in actual)
    with pytest.raises(ValueError):
        replay(payload, expected_lineage="wrong")


def test_signed_observation_replay(context):
    context = replace(context, observations=(observation(context, value="-0.25"),))
    assert replay(wire(context)) == assess_all(context)


@pytest.mark.parametrize("value", [None, 3, {}, {"extra": "field"}])
def test_malformed_replay_rejected(value):
    with pytest.raises(ValueError):
        replay(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("events", {}),
        ("observations", {}),
        ("instrument_id", 5),
        ("policy", None),
        ("quality", None),
    ],
)
def test_strict_replay_shapes(context, field, value):
    payload = wire(context)
    payload[field] = value
    with pytest.raises(ValueError):
        replay(payload)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_bars": 2},
        {"max_events": 0},
        {"max_age": timedelta(0)},
        {"max_quality_age": timedelta(days=8)},
        {"min_bars": True},
    ],
)
def test_bounded_policy(kwargs):
    with pytest.raises(ValueError):
        LanePolicy(**kwargs)


def test_context_capacity_and_numeric_bounds(context):
    with pytest.raises(ValueError):
        replace(context, policy=LanePolicy(max_events=5))
    with pytest.raises(ValueError):
        replace(
            context,
            events=(
                replace(
                    context.events[-1],
                    payload=QuotePayload(
                        Decimal("1e101"), Decimal("1e102"), Decimal(1), Decimal(1)
                    ),
                ),
            ),
        )


def test_hub_quality_divergence_gap_and_disconnect(context, binance_instrument, subscription):
    from types import SimpleNamespace

    from vision.market_data.hub import MarketDataHub

    hub = MarketDataHub(clock=lambda: context.as_of)
    hub.register(binance_instrument, subscription)
    hub.failover = SimpleNamespace(divergent=True)
    captured = capture_from_hub(hub, context.instrument_id, context.events)
    assert captured.quality.divergent
    assert technical(captured).status is Availability.UNAVAILABLE
    hub.failover.divergent = False
    event = next(item for item in context.events if item.event_type is EventType.TRADE)
    assert hub.ingest(event).accepted
    hub.gate.disconnected()
    captured = capture_from_hub(hub, context.instrument_id, context.events)
    assert dict(captured.quality.states)[context.instrument_id] == "disconnected"
    assert "DATA_DISCONNECTED" in flow(captured).reasons


@pytest.mark.parametrize(
    "bid,ask,expected",
    [
        (99, 102, -4),
        (99, 103, -2),
        (99, 104, -1),
        (100, 102, 0),
        (100, 103, 2),
        (100, 104, 3),
        (101, 102, 3),
        (101, 103, 5),
        (101, 104, 6),
    ],
)
def test_best_level_ofi_price_and_size_change_vectors(context, bid, ask, expected):
    quotes = [item for item in context.events if item.event_type is EventType.QUOTE]
    quotes[0] = replace(
        quotes[0], payload=QuotePayload(Decimal(100), Decimal(103), Decimal(3), Decimal(2))
    )
    quotes[1] = replace(
        quotes[1], payload=QuotePayload(Decimal(bid), Decimal(ask), Decimal(4), Decimal(1))
    )
    assert feature(flow(replace(context, events=tuple(quotes))), "best_level_ofi").value == expected


def test_caller_decimal_traps_cannot_change_replay(context):
    from decimal import Inexact, Rounded

    expected = assess_all(context)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert replay(wire(context)) == expected


def test_flow_epoch_boundary_and_trade_gap(context):
    events = tuple(
        replace(item, source_epoch=1) if item.event_type is EventType.QUOTE else item
        for item in context.events
    )
    assert "SOURCE_TRANSITION_REQUIRES_NEW_WINDOW" in flow(replace(context, events=events)).reasons
    trades = [item for item in context.events if item.event_type is EventType.TRADE]
    trades[1] = replace(trades[1], sequence=3)
    result = flow(replace(context, events=tuple(trades)))
    assert result.status is Availability.UNAVAILABLE
    assert feature(result, "window_cvd").reason == "TRADE_GAP"
    assert len(result.evidence) == 2


def test_bad_hub_stream_dominates_degraded(context, binance_instrument, subscription):
    from vision.market_data.hub import MarketDataHub

    hub = MarketDataHub(clock=lambda: context.as_of)
    hub.register(binance_instrument, subscription)
    keys = sorted(hub.gate.streams)
    hub.gate.streams[keys[0]].last_event = next(
        item for item in context.events if item.event_type is EventType.QUOTE
    )
    hub.gate.streams[keys[0]].last_rejection = "malformed"
    hub.gate.streams[keys[-1]].gap = True
    result = capture_from_hub(hub, context.instrument_id, context.events)
    assert dict(result.quality.states)[context.instrument_id] not in {"healthy", "degraded"}
    assert flow(result).status is Availability.UNAVAILABLE


@pytest.mark.parametrize("change", ["epoch", "unit", "stale", "order"])
def test_funding_oi_invalid_history_is_unavailable_feature(context, change):
    first = observation(
        context,
        "open_interest",
        "100",
        unit="contracts",
        source_ts=context.as_of - timedelta(seconds=1),
        received_ts=context.as_of - timedelta(seconds=1),
    )
    last = replace(
        first,
        event_id="oi-latest",
        value=Decimal(120),
        source_ts=context.as_of,
        received_ts=context.as_of,
    )
    if change == "epoch":
        last = replace(last, source_epoch=4)
    elif change == "unit":
        last = replace(last, unit="base_quantity")
    elif change == "stale":
        first = replace(first, source_ts=context.as_of - timedelta(hours=3))
        last = replace(last, source_ts=context.as_of - timedelta(hours=2))
    else:
        first, last = last, first
    result = flow(replace(context, observations=(first, last)))
    assert feature(result, "open_interest").value is None
    assert feature(result, "open_interest").reason == "INVALID_OBSERVATION_WINDOW"


def test_lane_code_has_no_control_or_provider_dependency():
    import ast
    from pathlib import Path

    import vision.analysis

    forbidden = (
        "vision.execution",
        "vision.orders",
        "vision.intents",
        "vision.risk",
        "vision.analysis.synthesizer",
        "vision.market_data.adapters",
        "openai",
        "requests",
        "urllib",
        "websockets",
    )
    for path in Path(vision.analysis.__file__).parent.rglob("*.py"):
        if "synthesizer" in path.relative_to(Path(vision.analysis.__file__).parent).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert all(not module.startswith(forbidden) for module in modules), path.name


def test_offline_lane_replay_cli(context, tmp_path):
    import os
    import subprocess
    import sys

    path = tmp_path / "lanes.json"
    path.write_text(json.dumps(wire(context)), encoding="utf-8")
    environment = {
        **os.environ,
        **dict.fromkeys(
            (
                "VISION_LIVE_TRADING_ENABLED",
                "VISION_MT5_EXECUTION_ENABLED",
                "VISION_PAPER_TRADING_ENABLED",
                "VISION_AGENTS_ENABLED",
            ),
            "false",
        ),
    }
    result = subprocess.run(
        [sys.executable, "-m", "vision", "lanes-replay", str(path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == [item.to_dict() for item in assess_all(context)]
    path.write_text('{"provider": "raw"}', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "vision", "lanes-replay", str(path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "Lane replay rejected" in result.stderr
    assert "Traceback" not in result.stderr


def test_committed_synthetic_fixture_replays_without_market_or_clock():
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).parent / "fixtures/lanes/research_context.json").read_text(encoding="utf-8")
    )
    first = replay(payload)
    assert first == replay(payload)
    assert all(item.status is Availability.AVAILABLE for item in first)
    assert feature(first[0], "window_return").value == Decimal("0.04")
    assert feature(first[1], "window_cvd").value == 2
    assert first[2].score == Decimal("-0.25") and first[3].score == Decimal("0.1")
    assert first[2].fixture_only and first[3].fixture_only
    assert first[2].confidence == first[3].confidence == 0
    assert all(
        item.provider.startswith("synthetic.") for result in first for item in result.evidence
    )
