"""Read-only synchronized portfolio valuation; no fills, cash ledger or execution."""

from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, DecimalException, Inexact, localcontext
from enum import StrEnum

from vision.core.contracts import (
    EventType,
    QuotePayload,
    TimestampBasis,
    TradePayload,
    _identifier,
    _positive_decimal,
    _utc,
)
from vision.core.instruments import InstrumentRegistry, QuantityUnit, digest


def number(value):
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or len(value.as_tuple().digits) > 64
        or abs(value.as_tuple().exponent) > 100
    ):
        raise ValueError("Bounded finite exact Decimal required")


def wire(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [wire(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    return value


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class Readiness(StrEnum):
    READY = "READY"
    STALE_MARKS = "STALE_MARKS"
    MISSING_MARKS = "MISSING_MARKS"
    INSTRUMENT_SPEC_INCOMPLETE = "INSTRUMENT_SPEC_INCOMPLETE"
    FX_CONVERSION_REQUIRED = "FX_CONVERSION_REQUIRED"
    DATA_DIVERGENT = "DATA_DIVERGENT"


@dataclass(frozen=True, slots=True)
class Position:
    position_id: str
    instrument_id: str
    spec_revision: str
    side: Side
    quantity: Decimal
    quantity_unit: QuantityUnit
    entry_price: Decimal
    opened_at: datetime
    input_id: str

    def __post_init__(self):
        for value in (self.position_id, self.instrument_id, self.spec_revision, self.input_id):
            _identifier(value)
        if not isinstance(self.side, Side) or not isinstance(self.quantity_unit, QuantityUnit):
            raise ValueError("Explicit side and quantity unit required")
        for value in (self.quantity, self.entry_price):
            number(value)
            _positive_decimal(value)
        _utc(self.opened_at)


@dataclass(frozen=True, slots=True)
class Mark:
    instrument_id: str
    spec_revision: str
    price: Decimal
    source_ts: datetime
    received_ts: datetime
    event_id: str
    source: str
    source_epoch: int
    sequence: int
    timestamp_basis: TimestampBasis
    continuity: str
    health: str
    event_type: EventType
    provider_sequence: str

    def __post_init__(self):
        for value in (self.instrument_id, self.spec_revision, self.event_id, self.source):
            _identifier(value)
        number(self.price)
        _positive_decimal(self.price)
        _utc(self.source_ts)
        _utc(self.received_ts)
        if any(type(value) is not int or value < 0 for value in (self.source_epoch, self.sequence)):
            raise ValueError("Nonnegative integer provenance required")
        if not isinstance(self.timestamp_basis, TimestampBasis) or self.continuity not in {
            "snapshot",
            "contiguous",
            "unverified",
        }:
            raise ValueError("Explicit mark provenance required")
        if self.health not in {"healthy", "degraded"}:
            raise ValueError("Only admitted marks may be cached")
        if self.event_type not in {EventType.TRADE, EventType.QUOTE} or not isinstance(
            self.event_type, EventType
        ):
            raise ValueError("Current trade or quote mark required")
        _identifier(self.provider_sequence)
        if self.timestamp_basis is TimestampBasis.RECEIPT and self.source_ts != self.received_ts:
            raise ValueError("Receipt provenance timestamps must agree")


@dataclass(frozen=True, slots=True)
class PortfolioPolicy:
    max_age: timedelta = timedelta(seconds=5)
    max_skew: timedelta = timedelta(seconds=2)
    max_spec_age: timedelta = timedelta(hours=24)
    allow_receipt_marks: bool = False

    def __post_init__(self):
        if (
            any(
                not isinstance(v, timedelta) or v <= timedelta(0)
                for v in (self.max_age, self.max_skew, self.max_spec_age)
            )
            or type(self.allow_receipt_marks) is not bool
        ):
            raise ValueError("Explicit positive portfolio timing policy required")

    def to_dict(self):
        return {
            "max_age_us": self.max_age // timedelta(microseconds=1),
            "max_skew_us": self.max_skew // timedelta(microseconds=1),
            "max_spec_age_us": self.max_spec_age // timedelta(microseconds=1),
            "allow_receipt_marks": self.allow_receipt_marks,
        }


@dataclass(frozen=True, slots=True)
class DataQuality:
    as_of: datetime
    states: tuple[tuple[str, str], ...]
    divergent: bool = False
    lineage: str = "standalone-admitted-marks"

    def __post_init__(self):
        _utc(self.as_of)
        _identifier(self.lineage)
        if (
            type(self.divergent) is not bool
            or type(self.states) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2 for pair in self.states)
        ):
            raise ValueError("Immutable explicit data quality required")
        if len({key for key, _ in self.states}) != len(self.states):
            raise ValueError("Duplicate instrument health")
        for key, state in self.states:
            _identifier(key)
            if state not in {"healthy", "degraded", "stale", "disconnected", "gap", "warming_up"}:
                raise ValueError("Unknown health state")


@dataclass(frozen=True, slots=True)
class PortfolioInputs:
    as_of: datetime
    reporting_currency: str
    positions: tuple[Position, ...]
    marks: tuple[Mark, ...]
    records: tuple
    quality: DataQuality
    policy: PortfolioPolicy
    generation: int

    def __post_init__(self):
        from vision.core.instruments import InstrumentRecord

        _utc(self.as_of)
        if (
            not isinstance(self.reporting_currency, str)
            or not self.reporting_currency.isalnum()
            or self.reporting_currency != self.reporting_currency.upper()
        ):
            raise ValueError("Explicit reporting currency required")
        for values, cls in (
            (self.positions, Position),
            (self.marks, Mark),
            (self.records, InstrumentRecord),
        ):
            if type(values) is not tuple or any(not isinstance(v, cls) for v in values):
                raise ValueError("Immutable typed portfolio inputs required")
        if (
            len({p.position_id for p in self.positions}) != len(self.positions)
            or len({m.instrument_id for m in self.marks}) != len(self.marks)
            or len({r.spec.instrument_id for r in self.records}) != len(self.records)
        ):
            raise ValueError("Duplicate portfolio inputs")
        if (
            not isinstance(self.policy, PortfolioPolicy)
            or not isinstance(self.quality, DataQuality)
            or self.quality.as_of != self.as_of
        ):
            raise ValueError("Health and inputs must share an atomic as_of")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("Nonnegative generation required")

    def to_dict(self):
        return {field.name: wire(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class PositionValuation:
    position_id: str
    currency: str
    unrealized_pnl: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal

    def __post_init__(self):
        _identifier(self.position_id)
        validate_totals(self)


@dataclass(frozen=True, slots=True)
class CurrencyTotals:
    currency: str
    unrealized_pnl: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal

    def __post_init__(self):
        validate_totals(self)


def validate_totals(value):
    _identifier(value.currency)
    for amount in (value.unrealized_pnl, value.gross_exposure, value.net_exposure):
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise ValueError("Exact finite valuation totals required")
    if value.gross_exposure < 0 or value.gross_exposure < value.net_exposure.copy_abs():
        raise ValueError("Gross exposure must cover absolute net exposure")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    inputs: PortfolioInputs
    readiness: Readiness
    issues: tuple[Readiness, ...]
    reasons: tuple[str, ...]
    valuations: tuple[PositionValuation, ...]
    currency_totals: tuple[CurrencyTotals, ...]
    unrealized_pnl: Decimal | None
    gross_exposure: Decimal | None
    net_exposure: Decimal | None
    lineage_id: str

    def __post_init__(self):
        if not isinstance(self.inputs, PortfolioInputs) or not isinstance(
            self.readiness, Readiness
        ):
            raise ValueError("Typed immutable snapshot required")
        for values, cls in (
            (self.issues, Readiness),
            (self.valuations, PositionValuation),
            (self.currency_totals, CurrencyTotals),
            (self.reasons, str),
        ):
            if type(values) is not tuple or any(not isinstance(value, cls) for value in values):
                raise ValueError("Immutable snapshot collections required")
        amounts = (self.unrealized_pnl, self.gross_exposure, self.net_exposure)
        if self.readiness is Readiness.READY:
            if self.issues or any(
                not isinstance(value, Decimal) or not value.is_finite() for value in amounts
            ):
                raise ValueError("Ready snapshot requires exact totals and no issues")
        elif (
            not self.issues
            or self.issues[0] is not self.readiness
            or any(value is not None for value in amounts)
        ):
            raise ValueError("Blocked snapshot must withhold reporting totals")
        if self.lineage_id != digest(self.inputs.to_dict()):
            raise ValueError("Snapshot lineage must match frozen inputs")

    @property
    def as_of(self):
        return self.inputs.as_of

    def to_dict(self):
        return {field.name: wire(getattr(self, field.name)) for field in fields(self)}


def evaluate(inputs: PortfolioInputs) -> PortfolioSnapshot:
    """Pure deterministic replay; reporting totals exist only when every check passes."""
    records = {r.spec.instrument_id: r for r in inputs.records}
    marks = {m.instrument_id: m for m in inputs.marks}
    health = dict(inputs.quality.states)
    reasons = []
    issues = set()
    if inputs.quality.divergent:
        issues.add(Readiness.DATA_DIVERGENT)
        reasons.append("phase2_divergence_latched")
    used_marks = []
    for position in inputs.positions:
        record, mark = records.get(position.instrument_id), marks.get(position.instrument_id)
        if (
            record is None
            or record.revision != position.spec_revision
            or record.quantity_unit != position.quantity_unit
            or record.valuation_model != "linear_quote"
            or not timedelta(0)
            <= inputs.as_of - record.provenance.observed_at
            <= inputs.policy.max_spec_age
        ):
            issues.add(Readiness.INSTRUMENT_SPEC_INCOMPLETE)
            reasons.append(f"spec_unavailable_or_incompatible:{position.position_id}")
        elif record.spec.quote_currency != inputs.reporting_currency:
            issues.add(Readiness.FX_CONVERSION_REQUIRED)
            reasons.append(
                f"conversion_required:{record.spec.quote_currency}:{inputs.reporting_currency}"
            )
        if position.opened_at > inputs.as_of:
            issues.add(Readiness.INSTRUMENT_SPEC_INCOMPLETE)
            reasons.append(f"position_after_as_of:{position.position_id}")
        if mark is None:
            issues.add(Readiness.MISSING_MARKS)
            reasons.append(f"missing_mark:{position.instrument_id}")
            continue
        used_marks.append(mark)
        if (
            record is None
            or mark.spec_revision != position.spec_revision
            or mark.source != record.provenance.source
        ):
            issues.add(Readiness.INSTRUMENT_SPEC_INCOMPLETE)
            reasons.append(f"mark_spec_mismatch:{position.position_id}")
        state = health.get(position.instrument_id)
        receipt_allowed = (
            inputs.policy.allow_receipt_marks and mark.timestamp_basis is TimestampBasis.RECEIPT
        )
        if state != "healthy" or mark.health == "degraded" and not receipt_allowed:
            issues.add(Readiness.STALE_MARKS)
            reasons.append(f"health_not_ready:{position.instrument_id}")
        if mark.timestamp_basis is TimestampBasis.RECEIPT and not receipt_allowed:
            issues.add(Readiness.STALE_MARKS)
            reasons.append(f"receipt_timestamp_requires_policy:{position.instrument_id}")
        if any(
            not timedelta(0) <= inputs.as_of - ts <= inputs.policy.max_age
            for ts in (mark.source_ts, mark.received_ts)
        ):
            issues.add(Readiness.STALE_MARKS)
            reasons.append(f"mark_stale_or_future:{position.instrument_id}")
    if used_marks and any(
        max(getattr(m, field) for m in used_marks) - min(getattr(m, field) for m in used_marks)
        > inputs.policy.max_skew
        for field in ("source_ts", "received_ts")
    ):
        issues.add(Readiness.STALE_MARKS)
        reasons.append("mark_timestamp_skew")
    priority = (
        Readiness.DATA_DIVERGENT,
        Readiness.INSTRUMENT_SPEC_INCOMPLETE,
        Readiness.MISSING_MARKS,
        Readiness.STALE_MARKS,
        Readiness.FX_CONVERSION_REQUIRED,
    )
    valuations, totals = [], []
    pnl = gross = net = None
    if not issues - {Readiness.FX_CONVERSION_REQUIRED}:
        try:
            with localcontext(Context(prec=1024, Emin=-4096, Emax=4096)) as context:
                context.traps[Inexact] = True
                by_currency = {}
                for position in inputs.positions:
                    record, mark = records[position.instrument_id], marks[position.instrument_id]
                    number(record.spec.contract_size)
                    # Every multiplier comes from the pinned instrument record.
                    units = position.quantity * record.spec.contract_size
                    sign = Decimal(1) if position.side is Side.LONG else Decimal(-1)
                    value = PositionValuation(
                        position.position_id,
                        record.spec.quote_currency,
                        (mark.price - position.entry_price) * units * sign,
                        mark.price * units,
                        mark.price * units * sign,
                    )
                    valuations.append(value)
                    prior = by_currency.get(value.currency, (Decimal(0), Decimal(0), Decimal(0)))
                    by_currency[value.currency] = tuple(
                        a + b
                        for a, b in zip(
                            prior,
                            (value.unrealized_pnl, value.gross_exposure, value.net_exposure),
                            strict=True,
                        )
                    )
                totals = [
                    CurrencyTotals(currency, *values)
                    for currency, values in sorted(by_currency.items())
                ]
                if not issues:
                    pnl, gross, net = by_currency.get(
                        inputs.reporting_currency, (Decimal(0), Decimal(0), Decimal(0))
                    )
        except (DecimalException, ValueError):
            issues.add(Readiness.INSTRUMENT_SPEC_INCOMPLETE)
            reasons.append("unsupported_numeric_range")
            valuations, totals = [], []
    ordered = tuple(state for state in priority if state in issues)
    return PortfolioSnapshot(
        inputs,
        ordered[0] if ordered else Readiness.READY,
        ordered,
        tuple(sorted(set(reasons))),
        tuple(valuations),
        tuple(totals),
        pnl,
        gross,
        net,
        digest(inputs.to_dict()),
    )


class MarkCache:
    def __init__(self, registry: InstrumentRegistry, *, capacity=256):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Positive mark capacity required")
        self.registry, self.capacity = registry, capacity
        self.lock = registry.lock
        self._marks = {}
        self._health = {}

    def put(self, mark: Mark):
        if not isinstance(mark, Mark):
            raise ValueError("Immutable Mark required")
        with self.lock:
            record = self.registry.get(mark.instrument_id)
            if (
                record is None
                or record.revision != mark.spec_revision
                or record.provenance.source != mark.source
            ):
                raise ValueError("Mark metadata provenance mismatch")
            previous = self._marks.get(mark.instrument_id)
            if previous is not None and (
                mark.received_ts < previous.received_ts
                or mark.source_ts < previous.source_ts
                or mark.source_epoch < previous.source_epoch
                or mark.event_id == previous.event_id
            ):
                raise ValueError("Mark replay or provenance regression")
            if (
                previous is not None
                and mark.event_type == previous.event_type
                and mark.sequence <= previous.sequence
            ):
                raise ValueError("Mark stream sequence cannot regress")
            if previous is None and len(self._marks) >= self.capacity:
                raise ValueError("Mark cache capacity exceeded")
            self._marks[mark.instrument_id] = mark
            self._health[mark.instrument_id] = (
                "healthy" if mark.timestamp_basis is TimestampBasis.RECEIPT else mark.health
            )

    def admit(self, event, decision):
        with self.lock:
            record = self.registry.get(event.instrument_id)
            if record is None:
                return False
            if not decision.accepted or event.delivery_kind != "live":
                if decision.reasons == ("duplicate",):
                    return False
                self._health[event.instrument_id] = (
                    "gap" if decision.status.value == "gap" else "degraded"
                )
                return False
            previous = self._marks.get(event.instrument_id)
            if (
                previous is not None
                and previous.event_type != event.event_type
                and event.source_ts < previous.source_ts
            ):
                return False
            if isinstance(event.payload, TradePayload):
                price = event.payload.price
            elif isinstance(event.payload, QuotePayload):
                with localcontext(Context(prec=1024)) as context:
                    context.traps[Inexact] = True
                    price = (event.payload.bid + event.payload.ask) / 2
            else:
                return False  # Finalized bars are historical, not current marks.
            self.put(
                Mark(
                    event.instrument_id,
                    record.revision,
                    price,
                    event.source_ts,
                    event.received_ts,
                    event.event_id,
                    event.source,
                    event.source_epoch,
                    event.sequence,
                    event.timestamp_basis,
                    event.continuity,
                    decision.status.value,
                    event.event_type,
                    event.provider_sequence,
                )
            )
            return True

    def put_many(self, marks):
        if not isinstance(marks, (tuple, list)) or len(marks) > self.capacity:
            raise ValueError("Bounded mark batch required")
        with self.lock:
            previous, health = dict(self._marks), dict(self._health)
            try:
                for mark in marks:
                    self.put(mark)
            except Exception:
                self._marks, self._health = previous, health
                raise


class PortfolioState:
    def __init__(self, registry, *, policy=None, clock=lambda: datetime.now(UTC), capacity=256):
        if (
            not isinstance(registry, InstrumentRegistry)
            or type(capacity) is not int
            or capacity < 1
        ):
            raise ValueError("Registry and positive position capacity required")
        self.registry, self.lock, self.clock = registry, registry.lock, clock
        self.policy = policy or PortfolioPolicy()
        self.capacity = capacity
        self.marks = MarkCache(registry, capacity=capacity)
        self._positions = ()
        self.generation = 0

    def replace_positions(self, positions):
        positions = tuple(positions)
        if (
            len(positions) > self.capacity
            or any(not isinstance(p, Position) for p in positions)
            or len({p.position_id for p in positions}) != len(positions)
        ):
            raise ValueError("Bounded unique immutable positions required")
        with self.lock:
            self._positions = tuple(sorted(positions, key=lambda p: p.position_id))
            self.generation += 1

    def snapshot(self, reporting_currency, *, as_of=None, quality=None):
        with self.lock:
            as_of = self.clock() if as_of is None else as_of
            quality = quality or DataQuality(as_of, tuple(sorted(self.marks._health.items())))
            inputs = PortfolioInputs(
                as_of,
                reporting_currency,
                self._positions,
                tuple(self.marks._marks[key] for key in sorted(self.marks._marks)),
                self.registry.records(),
                quality,
                self.policy,
                self.generation,
            )
            return evaluate(inputs)

    def snapshot_from_hub(self, hub, reporting_currency, *, as_of=None):
        """Capture synchronously on the hub's owning event loop; no await/interleaving."""
        with self.lock:
            as_of = self.clock() if as_of is None else as_of
            states = hub.gate.snapshot(as_of)
            health = []
            for instrument, mark in sorted(self.marks._marks.items()):
                relevant = [
                    (key, state) for key, state in states.items() if f":{instrument}:" in key
                ]
                matching = [
                    state
                    for key, state in relevant
                    if hub.gate.streams[key].last_event is not None
                    and hub.gate.streams[key].last_event.event_id == mark.event_id
                ]
                state = matching[0] if matching else "warming_up"
                if state == "degraded" and self.policy.allow_receipt_marks:
                    matched = [
                        hub.gate.streams[key]
                        for key, _ in relevant
                        if hub.gate.streams[key].last_event is not None
                        and hub.gate.streams[key].last_event.event_id == mark.event_id
                    ]
                    if (
                        matched
                        and matched[0].last_rejection is None
                        and mark.timestamp_basis is TimestampBasis.RECEIPT
                    ):
                        state = "healthy"
                for bad in ("gap", "disconnected", "stale"):
                    if any(value == bad for _, value in relevant):
                        state = bad
                        break
                if any(hub.gate.streams[key].last_rejection is not None for key, _ in relevant):
                    state = "degraded"
                health.append((instrument, state))
            failover = getattr(hub, "failover", None)
            quality = DataQuality(
                as_of, tuple(health), bool(failover and failover.divergent), "phase2-health-gate"
            )
            return self.snapshot(reporting_currency, as_of=as_of, quality=quality)
