"""Market Data Hub: instrument registry -> normalization -> health -> bounded bus."""

import asyncio
import os
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from vision.config import load_settings
from vision.core.bus.events import EventBus
from vision.core.contracts import BarPayload, CanonicalMarketEvent, InstrumentSpec
from vision.core.instruments import InstrumentRegistry
from vision.core.state.portfolio import PortfolioState
from vision.market_data.adapters.binance import (
    INTERVALS,
    SOURCE,
    BinanceNormalizer,
    BinanceREST,
    BinanceWebSocket,
    MarketDataError,
    Subscription,
    epoch_ms,
)
from vision.market_data.adapters.bybit import BybitNormalizer, BybitREST
from vision.market_data.health.gate import (
    DataHealthGate,
    HealthDecision,
    HealthStatus,
    stream_key,
)
from vision.market_data.sessions import SessionCalendar, SessionHealthGate


class MarketDataHub:
    def __init__(
        self,
        *,
        gate: DataHealthGate | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        load_settings(os.environ)
        self.gate = gate if gate is not None else SessionHealthGate()
        self.bus = bus if bus is not None else EventBus()
        self.clock = clock
        self.registry = InstrumentRegistry()
        self.portfolio = PortfolioState(self.registry, clock=clock)
        self.instruments: dict[str, InstrumentSpec] = {}
        self.market_instruments = {}
        self.sequence_steps: dict[str, int | None] = {}
        self.epochs: dict[str, int] = {}
        self.recovered: deque[CanonicalMarketEvent] = deque(maxlen=1000)
        self.accepted = 0
        self.rejected: Counter[str] = Counter()
        self.quarantine: deque[tuple[str, str]] = deque(maxlen=128)

    def register_market(self, metadata, calendar, *, granularity, record=None):
        """Register read-only FX/metals pricing without inventing execution economics."""
        from vision.market_data.adapters.oanda import GRANULARITIES, MarketMetadata

        if not isinstance(metadata, MarketMetadata) or granularity not in GRANULARITIES:
            raise ValueError("Explicit supported market metadata required")
        if not isinstance(calendar, SessionCalendar):
            raise ValueError("Explicit session calendar required")
        if not isinstance(self.gate, SessionHealthGate):
            raise ValueError("FX/metals require session-aware health")
        seconds = GRANULARITIES[granularity]
        prefix = f"{metadata.source}:{metadata.instrument_id}"
        additions = {
            f"{prefix}:quote": None,
            f"{prefix}:bar:{seconds}:bid": seconds * 1000,
            f"{prefix}:bar:{seconds}:ask": seconds * 1000,
        }
        if len(self.sequence_steps.keys() | additions.keys()) > self.gate.policy.max_streams:
            raise ValueError("Subscription capacity exceeded")
        previous = self.market_instruments.get(metadata.instrument_id)
        if previous is not None and metadata.observed_at < previous.observed_at:
            raise ValueError("Market metadata observation cannot regress")
        for key in additions:
            if (
                key in self.gate.calendars
                and self.gate.calendars[key].revision != calendar.revision
            ):
                raise ValueError("Calendar revision requires new hub")
        if record is not None:
            self.registry.register(metadata.verify_record(record))
        elif previous is not None and metadata.metadata_digest != previous.metadata_digest:
            self.registry.invalidate(metadata.instrument_id)
        self.market_instruments[metadata.instrument_id] = metadata
        self.sequence_steps.update(additions)
        for key in additions:
            self.gate.register_session(key, calendar)

    def register(
        self, instrument: InstrumentSpec, subscription: Subscription, *, record=None
    ) -> None:
        venue = instrument.venue.removesuffix("_SPOT")
        if (
            venue not in {"BINANCE", "BYBIT"}
            or instrument.instrument_id != f"{venue}:SPOT:{subscription.symbol}"
        ):
            raise ValueError("Instrument and public Spot subscription must agree")
        source = f"{venue.lower()}.spot"
        if venue == "BYBIT":
            BybitNormalizer(subscription)
        additions = {}
        for kind in subscription.kinds:
            suffix = f":{INTERVALS[subscription.interval]}" if kind == "bar" else ""
            key = f"{source}:{instrument.instrument_id}:{kind}{suffix}"
            step = {"trade": 1, "quote": None, "bar": INTERVALS[subscription.interval] * 1000}[kind]
            additions[key] = None if venue == "BYBIT" and kind == "trade" else step
        if len(self.sequence_steps.keys() | additions.keys()) > self.gate.policy.max_streams:
            raise ValueError("Subscription capacity exceeded")
        if record is not None:
            if record.spec != instrument or record.provenance.source != source:
                raise ValueError("Instrument record and subscription provenance disagree")
            self.registry.register(record)
        self.instruments[instrument.instrument_id] = instrument
        self.sequence_steps.update(additions)
        for key in additions:
            self.gate.register(key)

    def ingest(self, event: CanonicalMarketEvent) -> HealthDecision:
        key = stream_key(event)
        if event.delivery_kind != "live":
            return HealthDecision(False, HealthStatus.DEGRADED, ("backfill_is_not_live",))
        if (
            event.instrument_id not in (self.instruments.keys() | self.market_instruments.keys())
            or key not in self.sequence_steps
        ):
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
        try:
            self.portfolio.marks.admit(event, decision)
        except ValueError:
            self.portfolio.marks._health[event.instrument_id] = "degraded"
        return decision

    def recover_market_bar(self, event, history):
        """Atomic same-source/calendar recovery; history stays outside the live bus."""
        from vision.core.bus.events import BackpressureError

        key = stream_key(event)
        state = self.gate.streams.get(key)
        if (
            event.instrument_id not in self.market_instruments
            or not isinstance(event.payload, BarPayload)
            or state is None
            or not state.gap
            or state.last_event is None
            or event.delivery_kind != "live"
            or type(history) is not tuple
            or len(history) > 1000
        ):
            raise ValueError(
                "Registered pending session gap and bounded immutable history required"
            )
        calendar = self.gate.calendars[key]
        expected = calendar.expected_opens(
            state.last_event.payload.open_ts + timedelta(seconds=event.payload.interval_seconds),
            event.payload.open_ts,
            event.payload.interval_seconds,
        )
        if not expected or tuple(bar.payload.open_ts for bar in history) != expected:
            raise ValueError("Recovery missing, duplicated or closed-session candles")
        candidate = deepcopy(self.gate)
        candidate.streams[key].gap = False
        for bar in history:
            if (
                stream_key(bar) != key
                or bar.source_epoch != event.source_epoch
                or bar.delivery_kind != "backfill"
                or bar.source_epoch < state.last_event.source_epoch
                or bar.payload.open_ts + timedelta(seconds=bar.payload.interval_seconds)
                != bar.source_ts
                or bar.sequence != epoch_ms(bar.payload.open_ts)
                or bar.source_ts > bar.received_ts
                or bar.received_ts > self.clock()
            ):
                raise ValueError("Recovery provenance mismatch")
            candidate.commit(replace(bar, received_ts=state.last_event.received_ts))
        decision = candidate.assess(event, self.clock(), sequence_step=self.sequence_steps[key])
        if not decision.accepted:
            raise ValueError("Recovered live candle remains unhealthy")
        if len(self.bus) >= self.bus.capacity:
            raise BackpressureError("Canonical event bus is full")
        self.bus.publish(event)
        candidate.commit(event)
        self.gate = candidate
        self.recovered.extend(history)
        self.accepted += 1
        return decision

    def malformed(self, keys: tuple[str, ...] | None = None) -> None:
        self.rejected["malformed"] += 1
        for key in self.gate.streams if keys is None else keys:
            self.gate.streams[key].last_rejection = "malformed"

    def snapshot(self, rest: BinanceREST, subscription: Subscription, *, limit: int = 100) -> list:
        """Admit recent REST trades/latest finalized bar, rejecting old history as live data."""
        if "quote" in subscription.kinds:
            raise ValueError("REST snapshot supports trades and closed bars; quotes use WebSocket")
        if isinstance(rest, BinanceREST):
            from vision.market_data.instruments import refresh_public_spec

            record = refresh_public_spec(self.registry, rest, subscription.symbol, clock=self.clock)
            self.register(record.spec, subscription, record=record)
        else:
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

    async def stream(
        self, adapter: BinanceWebSocket, *, rest=None
    ) -> AsyncIterator[CanonicalMarketEvent]:
        source = "bybit.spot" if isinstance(adapter.normalizer, BybitNormalizer) else SOURCE
        venue = "BYBIT" if source == "bybit.spot" else "BINANCE"
        expected = f"{venue}:SPOT:{adapter.subscription.symbol}"
        if expected not in self.instruments:
            raise ValueError("Register the instrument before streaming")
        keys = tuple(
            f"{source}:{expected}:{kind}"
            + (f":{INTERVALS[adapter.subscription.interval]}" if kind == "bar" else "")
            for kind in adapter.subscription.kinds
        )
        if not set(keys) <= self.sequence_steps.keys():
            raise ValueError("Register all requested streams before streaming")

        connection_epoch = 0

        def connection(connected):
            nonlocal connection_epoch
            if connected:
                self.epochs[source] = self.epochs.get(source, 0) + 1
                connection_epoch = self.epochs[source]
            else:
                self.gate.disconnected(keys)

        adapter.on_connection = connection
        adapter.on_malformed = lambda: self.malformed(keys)
        iterator = adapter.events()
        try:
            async for event in iterator:
                event = replace(event, source_epoch=connection_epoch)
                decision = self.ingest(event)
                if (
                    isinstance(event.payload, BarPayload)
                    and rest is not None
                    and decision.status is HealthStatus.GAP
                ):
                    try:
                        state = self.gate.streams[stream_key(event)]
                        step = self.sequence_steps[stream_key(event)]
                        start = state.last_event.sequence + step
                        count, remainder = divmod(event.sequence - start, step)
                        if remainder or not 1 <= count <= 1000:
                            raise ValueError("Gap exceeds bounded recovery")
                        rows = await asyncio.to_thread(
                            rest.bars,
                            adapter.subscription,
                            start=start,
                            end=event.sequence - 1,
                            limit=count,
                        )
                        self.recover_bar(event, rest, adapter.subscription, rows=rows)
                    except (MarketDataError, ValueError):
                        self.rejected["backfill_failed"] += 1
                for published in self.bus.drain():
                    yield published
        finally:
            await iterator.aclose()

    def recover_bar(self, event, rest, subscription, *, max_bars=1000, rows=None):
        """Atomic bounded same-venue recovery. Historical bars never enter the live bus."""
        key = stream_key(event)
        state = self.gate.streams.get(key)
        if (
            not isinstance(event.payload, BarPayload)
            or state is None
            or not state.gap
            or state.last_event is None
        ):
            raise ValueError("Recovery requires a registered candle gap")
        if (
            event.delivery_kind != "live"
            or event.payload.open_ts is None
            or epoch_ms(event.payload.open_ts) != event.sequence
        ):
            raise ValueError("Recovery requires an aligned pending live candle")
        expected_source = "bybit.spot" if isinstance(rest, BybitREST) else "binance.spot"
        if event.source_epoch < state.last_event.source_epoch:
            raise ValueError("Recovery cannot regress a source epoch")
        if (
            event.source != expected_source
            or event.instrument_id != state.last_event.instrument_id
            or event.payload.interval_seconds != INTERVALS[subscription.interval]
            or subscription.symbol != self.instruments[event.instrument_id].symbol
        ):
            raise ValueError("Recovery must use the original venue and interval")
        step = self.sequence_steps[key]
        start = state.last_event.sequence + step
        end = event.sequence - step
        count, remainder = divmod(event.sequence - start, step)
        if (
            type(max_bars) is not int
            or not 1 <= max_bars <= 1000
            or remainder
            or not 1 <= count <= max_bars
        ):
            raise ValueError("Gap exceeds bounded recovery or has invalid alignment")
        if rows is None:
            rows = rest.bars(subscription, start=start, end=event.sequence - 1, limit=count)
        normalizer = (
            BybitNormalizer(subscription)
            if expected_source == "bybit.spot"
            else BinanceNormalizer(subscription)
        )
        received = self.clock()
        bars = [normalizer.rest_bar(row, received) for row in rows]
        if any(bar is None for bar in bars):
            raise ValueError("Incomplete candle recovery")
        bars.sort(key=lambda bar: bar.sequence)
        if [bar.sequence for bar in bars] != list(range(start, end + 1, step)):
            raise ValueError("Recovery is missing or duplicates candles")
        for bar in bars:
            if bar.payload.open_ts is None or epoch_ms(bar.payload.open_ts) != bar.sequence:
                raise ValueError("Recovery timestamps disagree")
        # Validate against an isolated gate before touching the live acceptance cursor.
        candidate = deepcopy(self.gate)
        candidate.streams[key].gap = False
        for bar in bars:
            # Recovery advances history, not the live arrival-time watermark.
            candidate.commit(
                replace(
                    bar,
                    source_epoch=event.source_epoch,
                    delivery_kind="backfill",
                    received_ts=state.last_event.received_ts,
                )
            )
        decision = candidate.assess(event, self.clock(), sequence_step=step)
        if not decision.accepted:
            raise ValueError("Recovered stream is not fresh and continuous")
        self.bus.publish(event)  # Backpressure leaves the original gap latched.
        candidate.commit(event)
        self.gate = candidate
        self.recovered.extend(
            replace(bar, source_epoch=event.source_epoch, delivery_kind="backfill") for bar in bars
        )
        self.accepted += 1
        self.portfolio.marks.admit(event, decision)
        return decision

    def status(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": dict(self.rejected),
            "streams": self.gate.snapshot(self.clock()),
            "live_trading_enabled": False,
            "mt5_execution_enabled": False,
            "paper_trading_enabled": False,
            "agents_enabled": False,
            "source_epochs": dict(self.epochs),
            "instrument_spec_revisions": {
                record.spec.instrument_id: record.revision for record in self.registry.records()
            },
            "cached_marks": len(self.portfolio.marks._marks),
            "recovered_bars": len(self.recovered),
            "market_metadata": {
                key: {
                    "revision": value.revision,
                    "canonical": value.canonical.key,
                    "pip_size": str(value.pip_size),
                    "display_quantum": str(value.display_quantum),
                    "execution_spec_ready": self.registry.get(key) is not None,
                }
                for key, value in sorted(self.market_instruments.items())
            },
            "calendar_revisions": {
                key: value.revision
                for key, value in sorted(getattr(self.gate, "calendars", {}).items())
            },
            "failover": self.failover.status() if hasattr(self, "failover") else None,
        }
