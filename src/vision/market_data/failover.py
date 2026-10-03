"""Controlled one-way Spot source selection with fresh cross-venue evidence."""

import asyncio
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from vision.core.contracts import QuotePayload
from vision.market_data.adapters.binance import MarketDataError
from vision.market_data.health.gate import stream_key


@dataclass(frozen=True)
class FailoverPolicy:
    max_divergence_bps: Decimal = Decimal("50")
    comparison_skew: timedelta = timedelta(seconds=2)
    evidence_ttl: timedelta = timedelta(seconds=30)

    def __post_init__(self):
        if (
            not isinstance(self.max_divergence_bps, Decimal)
            or not self.max_divergence_bps.is_finite()
            or not 0 < self.max_divergence_bps <= 10000
        ):
            raise ValueError("Invalid divergence threshold")
        if self.comparison_skew <= timedelta(0) or self.evidence_ttl <= timedelta(0):
            raise ValueError("Invalid failover timing policy")


class ControlledFailover:
    """Retains actual venue identities; never synthesizes Binance trades from Bybit."""

    def __init__(self, hub, primary, standby, *, policy=None):
        if primary not in hub.instruments or standby not in hub.instruments:
            raise ValueError("Register both instruments before failover")
        a, b = hub.instruments[primary], hub.instruments[standby]
        if (
            a.venue != "BINANCE_SPOT"
            or b.venue != "BYBIT_SPOT"
            or any(
                getattr(a, field) != getattr(b, field)
                for field in (
                    "symbol",
                    "asset_class",
                    "base_currency",
                    "quote_currency",
                    "contract_size",
                )
            )
        ):
            raise ValueError("Failover requires equivalent Spot economics on distinct venues")

        def kinds(instrument):
            return {
                key.split(f":{instrument}:")[1]
                for key in hub.sequence_steps
                if f":{instrument}:" in key
            }

        if kinds(primary) != kinds(standby) or "quote" not in kinds(primary):
            raise ValueError("Failover requires matching streams and quote comparison")
        self.hub = hub
        self.primary, self.standby = primary, standby
        self.active = primary
        self.policy = policy or FailoverPolicy()
        self.quotes = {}
        self.evidence_at = None
        self.divergent = False
        self.reason = "warming_up"
        self.selection_epoch = 0
        self.transitions = deque(maxlen=128)

    def usable(self, instrument):
        states = [state for key, state in self.hub.gate.streams.items() if f":{instrument}:" in key]
        now = self.hub.clock()
        return bool(states) and all(
            state.last_event is not None
            and not state.disconnected
            and not state.gap
            and state.last_rejection is None
            and now - state.last_event.received_ts
            <= self.hub.gate.policy.idle_timeout
            + (
                timedelta(seconds=state.last_event.payload.interval_seconds)
                if hasattr(state.last_event.payload, "interval_seconds")
                else timedelta(0)
            )
            for state in states
        )

    def observe(self, event):
        if event.instrument_id not in {self.primary, self.standby} or event.delivery_kind != "live":
            return None
        state = self.hub.gate.streams.get(stream_key(event))
        if state is None or state.last_event is None:
            return None
        if event.source_epoch != state.last_event.source_epoch:
            return None
        if (
            not timedelta(0)
            <= self.hub.clock() - event.received_ts
            <= self.hub.gate.policy.max_receive_age
        ):
            return None
        if isinstance(event.payload, QuotePayload):
            self.quotes[event.instrument_id] = event
            if self.primary in self.quotes and self.standby in self.quotes:
                a, b = self.quotes[self.primary], self.quotes[self.standby]
                now = self.hub.clock()
                if abs(a.received_ts - b.received_ts) <= self.policy.comparison_skew and all(
                    timedelta(0) <= now - quote.received_ts <= self.hub.gate.policy.max_receive_age
                    for quote in (a, b)
                ):
                    mid_a = (a.payload.bid + a.payload.ask) / 2
                    mid_b = (b.payload.bid + b.payload.ask) / 2
                    divergence = abs(mid_a - mid_b) / min(mid_a, mid_b) * 10000
                    if divergence > self.policy.max_divergence_bps:
                        self.divergent = True  # Explicit restart/review required; no oscillation.
                        self.reason = "divergence_latched"
                    elif not self.divergent:
                        self.evidence_at = now
        if self.divergent:
            return None
        if not self.usable(self.active):
            if (
                self.active != self.primary
                or not self.usable(self.standby)
                or self.evidence_at is None
                or self.hub.clock() - self.evidence_at > self.policy.evidence_ttl
            ):
                self.reason = "no_fresh_verified_source"
                return None
            self.active = self.standby
            self.selection_epoch += 1
            self.transitions.append(
                {
                    "from": self.primary,
                    "to": self.standby,
                    "selection_epoch": self.selection_epoch,
                    "at": self.hub.clock().isoformat(),
                }
            )
        self.reason = "active"
        return event if event.instrument_id == self.active else None

    def status(self):
        return {
            "active_instrument": self.active,
            "selection_epoch": self.selection_epoch,
            "reason": self.reason,
            "divergent": self.divergent,
            "transitions": list(self.transitions),
        }


async def controlled_stream(hub, primary, standby, primary_rest, standby_rest):
    """Bounded dual subscriptions; cancellation closes both connections and producers."""
    selector = ControlledFailover(
        hub,
        f"BINANCE:SPOT:{primary.subscription.symbol}",
        f"BYBIT:SPOT:{standby.subscription.symbol}",
    )
    hub.failover = selector
    queue = asyncio.Queue(maxsize=64)

    async def produce(adapter, rest):
        try:
            async with aclosing(hub.stream(adapter, rest=rest)) as stream:
                async for event in stream:
                    await queue.put(event)
        except (MarketDataError, ValueError):
            pass  # Stream callback marks the failed source disconnected.
        await queue.put(None)  # Cancellation must not block on a full terminal queue.

    tasks = [
        asyncio.create_task(produce(primary, primary_rest)),
        asyncio.create_task(produce(standby, standby_rest)),
    ]
    finished = 0
    try:
        while finished < 2:
            event = await queue.get()
            if event is None:
                finished += 1
                continue
            selected = selector.observe(event)
            if selected is not None:
                yield selected
        raise MarketDataError("Both public sources exhausted; failover is closed")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
