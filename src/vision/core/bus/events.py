"""Bounded FIFO handoff; health acceptance commits only after publication succeeds."""

from collections import deque

from vision.core.contracts import CanonicalMarketEvent


class BackpressureError(RuntimeError):
    """A consumer must drain the bus before another event can be accepted."""


class EventBus:
    def __init__(self, capacity: int = 1024):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Bus capacity must be positive")
        self.capacity = capacity
        self._queue: deque[CanonicalMarketEvent] = deque()

    def publish(self, event: CanonicalMarketEvent) -> None:
        if not isinstance(event, CanonicalMarketEvent):
            raise ValueError("Only canonical events can be published")
        if len(self._queue) >= self.capacity:
            raise BackpressureError("Canonical event bus is full")
        self._queue.append(event)

    def drain(self) -> list[CanonicalMarketEvent]:
        events = list(self._queue)
        self._queue.clear()
        return events

    def __len__(self) -> int:
        return len(self._queue)
