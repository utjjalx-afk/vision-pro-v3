"""Source-qualified FX/metals selection framework and unimplemented conversion boundary."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from vision.analysis.contracts import arithmetic
from vision.core.contracts import QuotePayload, _utc
from vision.market_data.failover import FailoverPolicy
from vision.market_data.health.gate import stream_key
from vision.market_data.sessions import SessionState


class FXConversionProvider(Protocol):
    """Future rates must supply their own provenance/as_of; no USD/USDT or inverse guesses."""

    def convert(self, amount: Decimal, base: str, quote: str, *, as_of): ...


@dataclass(frozen=True)
class ConversionUnavailable:
    reason: str = "FX_CONVERSION_REQUIRED"


class NoFXConversion:
    def convert(self, amount, base, quote, *, as_of):
        _utc(as_of)
        return ConversionUnavailable()


class FXSourceSelector:
    """No automatic cross-environment/provider substitution; explicit qualified pair only.

    OANDA practice and live are NOT independent sources and may never be paired.
    A future second provider can join only after its adapter and calendar are registered.
    Quote agreement qualifies pricing only, never position/spec/contract migration.
    """

    def __init__(self, hub, primary, standby, *, policy=None):
        a, b = hub.market_instruments.get(primary), hub.market_instruments.get(standby)
        if (
            a is None
            or b is None
            or a.canonical != b.canonical
            or a.source.split(".")[0] == b.source.split(".")[0]
        ):
            raise ValueError("Failover requires independent registered canonical price providers")
        self.hub, self.primary, self.standby = hub, primary, standby
        self.active = primary
        self.policy = policy or FailoverPolicy()
        self.quotes = {}
        self.evidence_at = None
        self.divergent = False
        self.selection_epoch = 0
        self.last_source_epochs = None

    def observe(self, event):
        state = self.hub.gate.streams.get(stream_key(event))
        now = self.hub.clock()
        if (
            event.instrument_id not in {self.primary, self.standby}
            or not isinstance(event.payload, QuotePayload)
            or event.delivery_kind != "live"
            or state is None
            or state.last_event != event
            or self.hub.gate.snapshot(now).get(stream_key(event)) != "healthy"
            or not timedelta(0) <= now - event.received_ts <= self.policy.comparison_skew
        ):
            return None
        self.quotes[event.instrument_id] = event
        if len(self.quotes) == 2:
            a, b = self.quotes[self.primary], self.quotes[self.standby]
            epochs = (a.source_epoch, b.source_epoch)
            if self.last_source_epochs is not None and self.last_source_epochs != epochs:
                self.evidence_at = None
            self.last_source_epochs = epochs
            if abs(a.source_ts - b.source_ts) <= self.policy.comparison_skew and all(
                timedelta(0) <= now - q.received_ts <= self.policy.comparison_skew
                and timedelta(0) <= now - q.source_ts <= self.hub.gate.policy.max_source_age
                for q in (a, b)
            ):
                with arithmetic():
                    mid_a = (a.payload.bid + a.payload.ask) / 2
                    mid_b = (b.payload.bid + b.payload.ask) / 2
                    difference = abs(mid_a - mid_b) / min(mid_a, mid_b) * 10000
                if difference > self.policy.max_divergence_bps:
                    self.divergent = True
                elif not self.divergent:
                    self.evidence_at = now
        return self.select(event)

    def usable(self, instrument):
        keys = [key for key in self.hub.sequence_steps if f":{instrument}:" in key]
        statuses = self.hub.gate.snapshot(self.hub.clock())
        now = self.hub.clock()
        return bool(keys) and all(
            statuses.get(key) == "healthy"
            and self.hub.gate.streams[key].last_event is not None
            and timedelta(0)
            <= now - self.hub.gate.streams[key].last_event.source_ts
            <= self.hub.gate.policy.max_source_age
            + (
                timedelta(seconds=self.hub.gate.streams[key].last_event.payload.interval_seconds)
                if hasattr(self.hub.gate.streams[key].last_event.payload, "interval_seconds")
                else timedelta(0)
            )
            for key in keys
        )

    def select(self, event):
        now = self.hub.clock()
        state = self.hub.gate.streams.get(stream_key(event))
        if (
            event.instrument_id not in {self.primary, self.standby}
            or event.delivery_kind != "live"
            or state is None
            or state.last_event != event
        ):
            return None
        # Closure or unknown primary calendar is never a reason to migrate sources.
        primary_keys = [key for key in self.hub.sequence_steps if f":{self.primary}:" in key]
        if not primary_keys or any(
            self.hub.gate.calendars.get(key) is None
            or self.hub.gate.calendars[key].state(now) is not SessionState.OPEN
            for key in primary_keys
        ):
            return None
        if self.last_source_epochs is not None and len(self.quotes) == 2:
            current_epochs = tuple(
                self.hub.gate.streams[stream_key(self.quotes[instrument])].last_event.source_epoch
                for instrument in (self.primary, self.standby)
            )
            if current_epochs != self.last_source_epochs:
                self.evidence_at = None
        if self.divergent:
            return None
        if not self.usable(self.active):
            if (
                self.active != self.primary
                or not self.usable(self.standby)
                or self.evidence_at is None
                or now - self.evidence_at > self.policy.evidence_ttl
            ):
                return None
            self.active = self.standby
            self.selection_epoch += 1
        return (
            event
            if (
                event.instrument_id == self.active
                and state is not None
                and state.last_event == event
                and self.usable(self.active)
            )
            else None
        )

    def status(self):
        return {
            "active_instrument": self.active,
            "divergent": self.divergent,
            "selection_epoch": self.selection_epoch,
            "evidence_at": self.evidence_at.isoformat() if self.evidence_at else None,
            "execution_migration_enabled": False,
        }
