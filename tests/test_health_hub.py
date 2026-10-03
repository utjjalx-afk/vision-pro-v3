from dataclasses import replace
from datetime import timedelta

import pytest

from vision.core.bus.events import BackpressureError, EventBus
from vision.market_data.health.gate import DataHealthGate, HealthPolicy, HealthStatus, stream_key
from vision.market_data.hub import MarketDataHub


def setup_hub(now, binance_instrument, subscription, **kwargs):
    hub = MarketDataHub(clock=lambda: now, **kwargs)
    hub.register(binance_instrument, subscription)
    return hub


def test_only_healthy_events_reach_bus(now, binance_instrument, subscription, market_event):
    hub = setup_hub(now, binance_instrument, subscription)
    assert set(hub.status()["streams"].values()) == {"warming_up"}
    assert hub.ingest(market_event).accepted
    assert hub.bus.drain() == [market_event]
    assert not hub.ingest(market_event).accepted
    assert hub.bus.drain() == []
    assert hub.rejected == {"duplicate": 1}
    assert hub.gate.snapshot(now)[stream_key(market_event)] == "healthy"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"source_ts": -10}, "stale_source"),
        ({"received_ts": -10}, "stale_receipt"),
        ({"source_ts": 10}, "future_timestamp"),
        ({"received_ts": 10}, "future_timestamp"),
    ],
)
def test_stale_and_future_rejected(changes, reason, now, market_event):
    event = replace(
        market_event, **{key: now + timedelta(seconds=delta) for key, delta in changes.items()}
    )
    gate = DataHealthGate()
    assert gate.assess(event, now).reasons == (reason,)


def test_gap_latches_until_explicit_epoch_reset(
    now, binance_instrument, subscription, market_event
):
    hub = setup_hub(now, binance_instrument, subscription)
    hub.ingest(market_event)
    gap = replace(market_event, event_id="synthetic-102", sequence=102)
    assert hub.ingest(gap).status is HealthStatus.GAP
    missing = replace(market_event, event_id="synthetic-101", sequence=101)
    assert hub.ingest(missing).reasons == ("sequence_gap_requires_reset",)
    hub.gate.disconnected()
    assert not hub.ingest(gap).accepted  # Reconnecting must not hide missing history.
    hub.gate.reset(stream_key(market_event))
    assert hub.ingest(gap).accepted


def test_quote_update_ids_may_jump(now, binance_instrument, subscription, normalizer, raw_quote):
    hub = setup_hub(now, binance_instrument, subscription)
    first = normalizer.message(raw_quote, now)
    raw_quote["u"] = 900
    second = normalizer.message(raw_quote, now)
    assert hub.ingest(first).accepted
    assert hub.ingest(second).accepted
    assert hub.gate.snapshot(now)[stream_key(second)] == "degraded"


def test_sequences_are_isolated_by_kind(
    now, binance_instrument, subscription, market_event, normalizer, raw_quote
):
    hub = setup_hub(now, binance_instrument, subscription)
    hub.ingest(market_event)
    raw_quote["u"] = 99999
    assert hub.ingest(normalizer.message(raw_quote, now)).accepted
    assert hub.ingest(replace(market_event, event_id="synthetic-next", sequence=101)).accepted


def test_backpressure_does_not_advance_health_cursor(
    now, binance_instrument, subscription, market_event
):
    hub = setup_hub(now, binance_instrument, subscription, bus=EventBus(1))
    hub.ingest(market_event)
    next_event = replace(market_event, event_id="synthetic-next", sequence=101)
    with pytest.raises(BackpressureError):
        hub.ingest(next_event)
    assert hub.accepted == 1
    assert hub.bus.drain() == [market_event]
    assert hub.ingest(next_event).accepted


def test_idle_staleness_and_disconnection(now, market_event):
    gate = DataHealthGate()
    gate.commit(market_event)
    key = stream_key(market_event)
    assert gate.snapshot(now + timedelta(seconds=20))[key] == "stale"
    gate.disconnected()
    assert gate.snapshot(now)[key] == "disconnected"


def test_unknown_instrument_never_published(now, market_event):
    hub = MarketDataHub(clock=lambda: now)
    assert hub.ingest(market_event).reasons == ("unregistered_stream",)
    assert len(hub.bus) == 0


def test_bounded_dedup_still_rejects_old_sequences(now, market_event):
    gate = DataHealthGate(HealthPolicy(dedup_capacity=2))
    for offset in range(3):
        event = replace(market_event, event_id=f"synthetic-{offset}", sequence=100 + offset)
        assert gate.assess(event, now, sequence_step=1).accepted
        gate.commit(event)
    assert gate.assess(replace(market_event, event_id="synthetic-0"), now).reasons == (
        "out_of_order",
    )


def test_closed_bar_freshness_tracks_interval(now, normalizer, raw_bar):
    gate = DataHealthGate()
    event = normalizer.message(raw_bar, now)
    assert gate.assess(event, now + timedelta(seconds=30)).reasons == ("stale_receipt",)
    rest_received = now + timedelta(seconds=30)
    rest_event = replace(event, received_ts=rest_received)
    assert gate.assess(rest_event, rest_received).accepted
    assert not gate.assess(
        replace(event, received_ts=now + timedelta(seconds=90)), now + timedelta(seconds=90)
    ).accepted


def test_subscription_capacity_rejection_is_atomic(now, binance_instrument, subscription):
    hub = MarketDataHub(gate=DataHealthGate(HealthPolicy(max_streams=1)), clock=lambda: now)
    with pytest.raises(ValueError, match="capacity"):
        hub.register(binance_instrument, subscription)
    assert not hub.instruments
    assert not hub.gate.streams


def test_source_time_out_of_order_without_sequence_gap(now, market_event):
    gate = DataHealthGate()
    gate.commit(market_event)
    backwards = replace(
        market_event,
        sequence=101,
        event_id="synthetic-next",
        source_ts=market_event.source_ts - timedelta(milliseconds=1),
    )
    assert gate.assess(backwards, now, sequence_step=1).reasons == ("out_of_order",)


def test_continuity_is_isolated_by_instrument(now, normalizer, raw_trade):
    gate = DataHealthGate()
    first = normalizer.message(raw_trade, now)
    gate.commit(first)
    second = replace(first, instrument_id="BINANCE:SPOT:ETHUSDT", sequence=900)
    assert gate.assess(second, now, sequence_step=1).accepted


def test_bar_gap_is_latched(now, normalizer, raw_bar):
    gate = DataHealthGate()
    first = normalizer.message(raw_bar, now)
    gate.commit(first)
    later = now + timedelta(seconds=120)
    gap = replace(
        first,
        event_id="synthetic-skipped-bar",
        sequence=first.sequence + 120000,
        source_ts=later,
        received_ts=later,
    )
    assert gate.assess(gap, later, sequence_step=60000).status is HealthStatus.GAP


def test_disconnect_is_scoped_to_adapter(now, binance_instrument, subscription, market_event):
    hub = setup_hub(now, binance_instrument, subscription)
    hub.ingest(market_event)
    unrelated = "synthetic:other-stream"
    hub.gate.register(unrelated)
    hub.gate.disconnected((unrelated,))
    assert hub.gate.snapshot(now)[stream_key(market_event)] == "healthy"
    assert hub.gate.snapshot(now)[unrelated] == "disconnected"


def test_hub_respects_execution_guards(monkeypatch):
    monkeypatch.setenv("VISION_LIVE_TRADING_ENABLED", "true")
    with pytest.raises(ValueError, match="must be false"):
        MarketDataHub()
