"""Market Data Hub: instrument registry -> normalization -> health -> bounded bus."""

import os
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from vision.config import load_settings
from vision.core.bus.events import EventBus
from vision.core.contracts import CanonicalMarketEvent, InstrumentSpec
from vision.market_data.adapters.binance import (
    INTERVALS,
    SOURCE,
    BinanceNormalizer,
    BinanceREST,
    BinanceWebSocket,
    Subscription,
)
from vision.market_data.health.gate import (
    DataHealthGate,
    HealthDecision,
    HealthStatus,
    stream_key,
)


class MarketDataHub:
    def __init__(
        self,
        *,
        gate: DataHealthGate | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        load_settings(os.environ)
        self.gate = gate if gate is not None else DataHealthGate()
        self.bus = bus if bus is not None else EventBus()
        self.clock = clock
        self.instruments: dict[str, InstrumentSpec] = {}
        self.sequence_steps: dict[str, int | None] = {}
        self.accepted = 0
        self.rejected: Counter[str] = Counter()
        self.quarantine: deque[tuple[str, str]] = deque(maxlen=128)

    def register(self, instrument: InstrumentSpec, subscription: Subscription) -> None:
        if instrument.instrument_id != f"BINANCE:SPOT:{subscription.symbol}":
            raise ValueError("Instrument and Binance subscription must agree")
        additions = {}
        for kind in subscription.kinds:
            suffix = f":{INTERVALS[subscription.interval]}" if kind == "bar" else ""
            key = f"{SOURCE}:{instrument.instrument_id}:{kind}{suffix}"
            step = {"trade": 1, "quote": None, "bar": INTERVALS[subscription.interval] * 1000}[kind]
            additions[key] = step
        if len(self.sequence_steps.keys() | additions.keys()) > self.gate.policy.max_streams:
            raise ValueError("Subscription capacity exceeded")
        self.instruments[instrument.instrument_id] = instrument
        self.sequence_steps.update(additions)
        for key in additions:
            self.gate.register(key)

    def ingest(self, event: CanonicalMarketEvent) -> HealthDecision:
        key = stream_key(event)
        if event.instrument_id not in self.instruments or key not in self.sequence_steps:
            decision = HealthDecision(False, HealthStatus.DEGRADED, ("unregistered_stream",))
        else:
            decision = self.gate.assess(event, self.clock(), sequence_step=self.sequence_steps[key])
        if decision.accepted:
            # A full bus raises without advancing the continuity/dedup acceptance cursor.
            self.bus.publish(event)
            self.gate.commit(event)
            self.accepted += 1
        else:
            for reason in decision.reasons:
                self.rejected[reason] += 1
                self.quarantine.append((event.event_id, reason))
        return decision

    def malformed(self, keys: tuple[str, ...] | None = None) -> None:
        self.rejected["malformed"] += 1
        for key in self.gate.streams if keys is None else keys:
            self.gate.streams[key].last_rejection = "malformed"

    def snapshot(self, rest: BinanceREST, subscription: Subscription, *, limit: int = 100) -> list:
        """Admit recent REST trades/latest finalized bar, rejecting old history as live data."""
        if "quote" in subscription.kinds:
            raise ValueError("REST snapshot supports trades and closed bars; quotes use WebSocket")
        self.register(rest.instrument(subscription.symbol), subscription)
        normalizer = BinanceNormalizer(subscription)
        if "trade" in subscription.kinds:
            rows = rest.trades(subscription.symbol, limit=limit)
            received = self.clock()
            for row in rows:
                try:
                    event = normalizer.trade(row, received)
                except (ValueError, TypeError, KeyError, OverflowError):
                    self.malformed()
                    continue
                self.ingest(event)
        if "bar" in subscription.kinds:
            rows = rest.bars(subscription, limit=2)
            received = self.clock()
            closed = []
            for row in rows:
                try:
                    event = normalizer.rest_bar(row, received)
                except (ValueError, TypeError, OverflowError):
                    self.malformed()
                    continue
                if event is not None:
                    closed.append(event)
            if closed:
                self.ingest(max(closed, key=lambda event: event.sequence))
        return self.bus.drain()

    async def stream(self, adapter: BinanceWebSocket) -> AsyncIterator[CanonicalMarketEvent]:
        expected = f"BINANCE:SPOT:{adapter.subscription.symbol}"
        if expected not in self.instruments:
            raise ValueError("Register the instrument before streaming")
        keys = tuple(
            f"{SOURCE}:{expected}:{kind}"
            + (f":{INTERVALS[adapter.subscription.interval]}" if kind == "bar" else "")
            for kind in adapter.subscription.kinds
        )
        if not set(keys) <= self.sequence_steps.keys():
            raise ValueError("Register all requested streams before streaming")
        adapter.on_connection = lambda connected: (
            None if connected else self.gate.disconnected(keys)
        )
        adapter.on_malformed = lambda: self.malformed(keys)
        iterator = adapter.events()
        try:
            async for event in iterator:
                self.ingest(event)
                for published in self.bus.drain():
                    yield published
        finally:
            await iterator.aclose()

    def status(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": dict(self.rejected),
            "streams": self.gate.snapshot(self.clock()),
            "live_trading_enabled": False,
            "mt5_execution_enabled": False,
        }
