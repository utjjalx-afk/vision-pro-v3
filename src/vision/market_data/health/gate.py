"""Deterministic live-data admission with stream-local continuity and bounded memory."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from vision.core.contracts import BarPayload, CanonicalMarketEvent, TimestampBasis, _utc


class HealthStatus(StrEnum):
    WARMING_UP = "warming_up"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    GAP = "gap"


@dataclass(frozen=True)
class HealthPolicy:
    max_source_age: timedelta = timedelta(seconds=5)
    max_receive_age: timedelta = timedelta(seconds=5)
    future_tolerance: timedelta = timedelta(seconds=2)
    idle_timeout: timedelta = timedelta(seconds=15)
    dedup_capacity: int = 10000
    max_streams: int = 64

    def __post_init__(self):
        for value in (
            self.max_source_age,
            self.max_receive_age,
            self.future_tolerance,
            self.idle_timeout,
        ):
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError("Health time limits must be positive durations")
        for value in (self.dedup_capacity, self.max_streams):
            if type(value) is not int or value < 1:
                raise ValueError("Health capacities must be positive integers")


@dataclass(frozen=True)
class HealthDecision:
    accepted: bool
    status: HealthStatus
    reasons: tuple[str, ...] = ()


@dataclass
class StreamState:
    last_event: CanonicalMarketEvent | None = None
    disconnected: bool = False
    gap: bool = False
    last_rejection: str | None = None


def stream_key(event: CanonicalMarketEvent) -> str:
    interval = f":{event.payload.interval_seconds}" if isinstance(event.payload, BarPayload) else ""
    return f"{event.source}:{event.instrument_id}:{event.event_type.value}{interval}"


class DataHealthGate:
    def __init__(self, policy: HealthPolicy | None = None):
        self.policy = policy or HealthPolicy()
        self.streams: dict[str, StreamState] = {}
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    def _state(self, key: str) -> StreamState:
        if key not in self.streams:
            if len(self.streams) >= self.policy.max_streams:
                raise ValueError("Health stream capacity exhausted")
            self.streams[key] = StreamState()
        return self.streams[key]

    def register(self, key: str) -> None:
        self._state(key)

    def assess(
        self, event: CanonicalMarketEvent, now: datetime, *, sequence_step: int | None = None
    ) -> HealthDecision:
        _utc(now)
        if sequence_step is not None and (type(sequence_step) is not int or sequence_step < 1):
            raise ValueError("Continuity step must be a positive integer when specified")
        key = stream_key(event)
        state = self._state(key)
        last = state.last_event
        rejection = None
        status = HealthStatus.DEGRADED
        receive_age = now - event.received_ts
        source_age = now - event.source_ts
        source_limit = self.policy.max_source_age
        if isinstance(event.payload, BarPayload):
            source_limit += timedelta(seconds=event.payload.interval_seconds)
        if state.gap:
            rejection, status = "sequence_gap_requires_reset", HealthStatus.GAP
        elif (
            receive_age < -self.policy.future_tolerance
            or source_age < -self.policy.future_tolerance
        ):
            rejection = "future_timestamp"
        elif receive_age > self.policy.max_receive_age:
            rejection, status = "stale_receipt", HealthStatus.STALE
        elif event.timestamp_basis is TimestampBasis.EXCHANGE and (
            source_age > source_limit or event.received_ts - event.source_ts > source_limit
        ):
            rejection, status = "stale_source", HealthStatus.STALE
        elif (key, event.event_id) in self._seen:
            rejection = "duplicate"
        elif last is not None and (
            event.sequence <= last.sequence
            or event.source_ts < last.source_ts
            or event.received_ts < last.received_ts
        ):
            rejection = "out_of_order"
        elif last is not None:
            if sequence_step is not None and event.sequence != last.sequence + sequence_step:
                state.gap = True
                rejection, status = "sequence_gap", HealthStatus.GAP
        if rejection:
            if rejection != "duplicate":
                state.last_rejection = rejection
            return HealthDecision(False, status, (rejection,))
        if event.timestamp_basis is TimestampBasis.RECEIPT:
            return HealthDecision(True, HealthStatus.DEGRADED, ("exchange_timestamp_unavailable",))
        return HealthDecision(True, HealthStatus.HEALTHY)

    def commit(self, event: CanonicalMarketEvent) -> None:
        key = stream_key(event)
        state = self._state(key)
        state.last_event = event
        state.disconnected = False
        state.last_rejection = None
        self._seen[(key, event.event_id)] = None
        while len(self._seen) > self.policy.dedup_capacity:
            self._seen.popitem(last=False)

    def disconnected(self, keys: tuple[str, ...] | None = None) -> None:
        for key in self.streams if keys is None else keys:
            if key in self.streams:
                self.streams[key].disconnected = True

    def reset(self, key: str) -> None:
        """Explicit new continuity epoch, not a claim that lost history was repaired."""
        if key in self.streams:
            self.streams[key] = StreamState()
        for identity in list(self._seen):
            if identity[0] == key:
                del self._seen[identity]

    def snapshot(self, now: datetime) -> dict[str, str]:
        _utc(now)
        result = {}
        for key, state in self.streams.items():
            last = state.last_event
            if state.gap:
                status = HealthStatus.GAP
            elif state.disconnected:
                status = HealthStatus.DISCONNECTED
            elif last is None:
                status = HealthStatus.WARMING_UP
            elif now - last.received_ts > self.policy.idle_timeout + (
                timedelta(seconds=last.payload.interval_seconds)
                if isinstance(last.payload, BarPayload)
                else timedelta(0)
            ):
                status = HealthStatus.STALE
            elif state.last_rejection or last.timestamp_basis is TimestampBasis.RECEIPT:
                status = HealthStatus.DEGRADED
            else:
                status = HealthStatus.HEALTHY
            result[key] = status.value
        return result
