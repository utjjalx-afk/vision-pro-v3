"""Versioned, bounded session calendars; closure is distinct from transport health."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from zoneinfo import ZoneInfo

import tzdata

from vision.analysis.contracts import wire
from vision.core.contracts import BarPayload, _identifier, _utc
from vision.core.instruments import digest
from vision.market_data.health.gate import (
    DataHealthGate,
    HealthDecision,
    HealthStatus,
    stream_key,
)


class SessionState(StrEnum):
    OPEN = "OPEN"
    MARKET_CLOSED = "MARKET_CLOSED"
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"


@lru_cache(maxsize=16)
def timezone_rules(name):
    if not isinstance(name, str) or name not in {"America/New_York", "UTC", "Europe/London"}:
        raise ValueError("Unsupported explicit calendar timezone")
    with files("tzdata.zoneinfo").joinpath(*name.split("/")).open("rb") as handle:
        return ZoneInfo.from_file(handle, key=name)


@dataclass(frozen=True)
class SessionCalendar:
    """Half-open local-minute windows, explicit dated overrides and coverage attestation.

    No default holiday calendar. Caller must verify all exceptions in the bounded
    coverage period. The reference/revision identify that attestation, not market truth.
    Windows crossing midnight must be split into two local dates.
    """

    calendar_id: str
    reference: str
    timezone: str
    valid_from: datetime
    valid_until: datetime
    weekly: tuple[tuple[int, int, int], ...]
    overrides: tuple[tuple[str, tuple[tuple[int, int], ...]], ...]
    exceptions_verified: bool
    timezone_rules_version: str

    def __post_init__(self):
        _identifier(self.calendar_id)
        _identifier(self.reference)
        _utc(self.valid_from)
        _utc(self.valid_until)
        if not timedelta(0) < self.valid_until - self.valid_from <= timedelta(days=370):
            raise ValueError("Calendar coverage must be positive and bounded")
        timezone_rules(self.timezone)
        if self.timezone_rules_version != tzdata.__version__:
            raise ValueError("Replay requires the exact packaged timezone rules version")
        if self.exceptions_verified is not True:
            raise ValueError("Explicit holiday/exception verification is required")
        if type(self.weekly) is not tuple or not 1 <= len(self.weekly) <= 28:
            raise ValueError("Immutable bounded weekly schedule required")
        if type(self.overrides) is not tuple or len(self.overrides) > 370:
            raise ValueError("Immutable bounded calendar overrides required")
        for window in self.weekly:
            if type(window) is not tuple or len(window) != 3:
                raise ValueError("Invalid weekly window")
            day, start, end = window
            if type(day) is not int or not 0 <= day <= 6:
                raise ValueError("Invalid weekday")
            self._window(start, end)
        for day in range(7):
            self._overlap(tuple((a, b) for d, a, b in self.weekly if d == day))
        names = []
        for item in self.overrides:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("Invalid dated override")
            name, windows = item
            if date.fromisoformat(name).isoformat() != name or type(windows) is not tuple:
                raise ValueError("Explicit local date and immutable windows required")
            names.append(name)
            if len(windows) > 4:
                raise ValueError("Override window bound exceeded")
            for window in windows:
                if type(window) is not tuple or len(window) != 2:
                    raise ValueError("Invalid override window")
                self._window(*window)
            self._overlap(windows)
        if len(names) != len(set(names)):
            raise ValueError("Duplicate date override")

    @staticmethod
    def _window(start, end):
        if any(type(v) is not int for v in (start, end)) or not 0 <= start < end <= 1440:
            raise ValueError("Invalid local-minute window")

    @staticmethod
    def _overlap(windows):
        ordered = sorted(windows)
        if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:], strict=False)):
            raise ValueError("Overlapping session windows")

    @property
    def revision(self):
        return digest(wire(self))

    def state(self, at):
        _utc(at)
        if not self.valid_from <= at < self.valid_until:
            return SessionState.CALENDAR_UNKNOWN
        local = at.astimezone(timezone_rules(self.timezone))
        minute = local.hour * 60 + local.minute
        windows = dict(self.overrides).get(local.date().isoformat())
        if windows is None:
            windows = tuple((a, b) for day, a, b in self.weekly if day == local.weekday())
        return (
            SessionState.OPEN
            if any(a <= minute < b for a, b in windows)
            else SessionState.MARKET_CLOSED
        )

    def expected_opens(self, start, end, interval, *, bound=10000):
        """Expected candle opens including start, excluding end; never fabricate candles."""
        _utc(start)
        _utc(end)
        if type(interval) is not int or interval not in {60, 300, 900, 3600}:
            raise ValueError("Unsupported fixed UTC candle interval")
        step = timedelta(seconds=interval)
        if end < start or (end - start) % step or (end - start) // step > bound:
            raise ValueError("Recovery/calendar scan exceeds bounded aligned range")
        result = []
        at = start
        while at < end:
            state = self.state(at)
            if state is SessionState.CALENDAR_UNKNOWN:
                raise ValueError("Calendar coverage unknown")
            if state is SessionState.OPEN:
                result.append(at)
            at += step
        return tuple(result)


def calendar_from_dict(value):
    from vision.core.codec import utc_string

    if type(value) is not dict or set(value) != set(SessionCalendar.__dataclass_fields__):
        raise ValueError("Exact calendar fields required")
    data = dict(value)
    data["valid_from"] = utc_string(data["valid_from"])
    data["valid_until"] = utc_string(data["valid_until"])
    data["weekly"] = tuple(tuple(v) for v in data["weekly"])
    data["overrides"] = tuple(
        (day, tuple(tuple(w) for w in windows)) for day, windows in data["overrides"]
    )
    return SessionCalendar(**data)


class SessionHealthGate(DataHealthGate):
    def __init__(self, policy=None):
        super().__init__(policy)
        self.calendars = {}

    def register_session(self, key, calendar):
        if not isinstance(calendar, SessionCalendar):
            raise ValueError("Explicit verified calendar required")
        if key in self.calendars and self.calendars[key].revision != calendar.revision:
            raise ValueError("Calendar change requires a new hub/replay epoch")
        self.register(key)
        self.calendars[key] = calendar

    def assess(self, event, now, *, sequence_step=None):
        key = stream_key(event)
        calendar = self.calendars.get(key)
        if calendar is None:
            return super().assess(event, now, sequence_step=sequence_step)
        state = calendar.state(now)
        if state is not SessionState.OPEN:
            status = HealthStatus(state.value.lower())
            return HealthDecision(False, status, (state.value.lower(),))
        if calendar.state(event.source_ts) is SessionState.CALENDAR_UNKNOWN:
            self._state(key).last_rejection = "source_calendar_unknown"
            return HealthDecision(
                False, HealthStatus.CALENDAR_UNKNOWN, ("source_calendar_unknown",)
            )
        if isinstance(event.payload, BarPayload):
            payload = event.payload
            if (
                payload.open_ts is None
                or payload.price_basis not in {"bid", "ask"}
                or calendar.state(payload.open_ts) is not SessionState.OPEN
                or payload.volume_basis != "price_count"
                or event.sequence
                != (payload.open_ts - datetime(1970, 1, 1, tzinfo=payload.open_ts.tzinfo))
                // timedelta(milliseconds=1)
                or event.sequence % (payload.interval_seconds * 1000)
                or payload.open_ts + timedelta(seconds=payload.interval_seconds) != event.source_ts
            ):
                self._state(key).last_rejection = "invalid_session_bar"
                return HealthDecision(False, HealthStatus.DEGRADED, ("invalid_session_bar",))
            basic = super().assess(event, now, sequence_step=None)
            if not basic.accepted:
                return basic
            last = self._state(key).last_event
            if last is not None and event.sequence > last.sequence:
                try:
                    expected = calendar.expected_opens(
                        last.payload.open_ts + timedelta(seconds=payload.interval_seconds),
                        payload.open_ts,
                        payload.interval_seconds,
                    )
                except ValueError:
                    self._state(key).last_rejection = "gap_calendar_unknown"
                    return HealthDecision(
                        False, HealthStatus.CALENDAR_UNKNOWN, ("gap_calendar_unknown",)
                    )
                if expected:
                    self._state(key).gap = True
                    return HealthDecision(False, HealthStatus.GAP, ("missing_open_session_bars",))
            # Closed session intervals intentionally do not create sequence gaps.
            return basic
        if calendar.state(event.source_ts) is not SessionState.OPEN:
            self._state(key).last_rejection = "quote_outside_session"
            return HealthDecision(False, HealthStatus.DEGRADED, ("quote_outside_session",))
        return super().assess(event, now, sequence_step=sequence_step)

    def snapshot(self, now):
        result = super().snapshot(now)
        for key, calendar in self.calendars.items():
            session = calendar.state(now)
            if session is not SessionState.OPEN:
                result[key] = session.value.lower()
        return result
