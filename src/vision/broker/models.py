"""Immutable cross-platform broker calculation contracts, separate from market specs."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from vision.analysis.contracts import number, wire
from vision.core.contracts import _identifier, _utc
from vision.core.instruments import digest


def positive(value):
    number(value)
    if value <= 0:
        raise ValueError("Positive bounded Decimal required")


def nonnegative(value):
    number(value)
    if value < 0:
        raise ValueError("Nonnegative bounded Decimal required")


@dataclass(frozen=True)
class BrokerSpec:
    symbol: str
    canonical: str
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    volume_limit: Decimal
    point: Decimal
    tick_size: Decimal
    tick_value_profit: Decimal
    tick_value_loss: Decimal
    contract_size: Decimal
    stops_level: int
    freeze_level: int
    trade_mode: int
    profit_currency: str
    margin_currency: str
    observed_at: datetime
    account_identity: str

    def __post_init__(self):
        for v in (
            self.symbol,
            self.canonical,
            self.profit_currency,
            self.margin_currency,
            self.account_identity,
        ):
            _identifier(v)
        _utc(self.observed_at)
        for v in (
            self.volume_min,
            self.volume_max,
            self.volume_step,
            self.point,
            self.tick_size,
            self.contract_size,
        ):
            positive(v)
        for v in (self.volume_limit, self.tick_value_profit, self.tick_value_loss):
            nonnegative(v)
        if self.volume_min > self.volume_max:
            raise ValueError("Invalid broker volume bounds")
        if any(
            type(v) is not int or v < 0
            for v in (self.stops_level, self.freeze_level, self.trade_mode)
        ):
            raise ValueError("Explicit broker integer semantics required")

    @property
    def revision(self):
        # Observation time is separate provenance; stable economics produce stable revisions.
        return digest({k: v for k, v in wire(self).items() if k != "observed_at"})


@dataclass(frozen=True)
class BrokerQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    source_at: datetime
    observed_at: datetime

    def __post_init__(self):
        _identifier(self.symbol)
        positive(self.bid)
        positive(self.ask)
        if self.bid > self.ask:
            raise ValueError("Crossed broker quote")
        _utc(self.source_at)
        _utc(self.observed_at)


@dataclass(frozen=True)
class BrokerAccount:
    identity: str
    currency: str
    equity: Decimal
    balance: Decimal
    free_margin: Decimal
    demo: bool
    connected: bool
    observed_at: datetime

    def __post_init__(self):
        _identifier(self.identity)
        _identifier(self.currency)
        for v in (self.equity, self.balance, self.free_margin):
            number(v)
        if type(self.demo) is not bool or type(self.connected) is not bool:
            raise ValueError("Explicit account health required")
        _utc(self.observed_at)


@dataclass(frozen=True)
class BrokerPosition:
    identity: str
    symbol: str
    side: str
    volume: Decimal
    entry: Decimal
    stop: Decimal

    def __post_init__(self):
        _identifier(self.identity)
        _identifier(self.symbol)
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("Unknown position side")
        positive(self.volume)
        positive(self.entry)
        nonnegative(self.stop)


@dataclass(frozen=True)
class BrokerSnapshot:
    account: BrokerAccount
    specs: tuple[BrokerSpec, ...]
    quotes: tuple[BrokerQuote, ...]
    positions: tuple[BrokerPosition, ...]
    pending_orders: int
    as_of: datetime
    bridge_version: str

    def __post_init__(self):
        _utc(self.as_of)
        _identifier(self.bridge_version)
        if not isinstance(self.account, BrokerAccount):
            raise ValueError("Broker account required")
        for values, cls in (
            (self.specs, BrokerSpec),
            (self.quotes, BrokerQuote),
            (self.positions, BrokerPosition),
        ):
            if (
                type(values) is not tuple
                or len(values) > 256
                or any(not isinstance(v, cls) for v in values)
            ):
                raise ValueError("Bounded immutable broker snapshot required")
        if any(
            len({v.symbol for v in values}) != len(values) for values in (self.specs, self.quotes)
        ):
            raise ValueError("Duplicate broker specs/quotes")
        if len({v.identity for v in self.positions}) != len(self.positions):
            raise ValueError("Duplicate broker positions")
        if type(self.pending_orders) is not int or self.pending_orders < 0:
            raise ValueError("Known pending-order count required")
        if any(v.account_identity != self.account.identity for v in self.specs):
            raise ValueError("Mixed account provenance")

    @property
    def snapshot_id(self):
        return digest(wire(self))


@dataclass(frozen=True)
class SizeRequest:
    intent_id: str
    canonical: str
    broker_symbol: str
    side: str
    stop: Decimal
    risk_budget: Decimal
    requested_at: datetime
    expires_at: datetime
    expected_account_identity: str
    expected_spec_revision: str
    strategy_eligible: bool
    eligibility_evidence_id: str

    def __post_init__(self):
        for v in (
            self.intent_id,
            self.canonical,
            self.broker_symbol,
            self.expected_account_identity,
            self.expected_spec_revision,
            self.eligibility_evidence_id,
        ):
            _identifier(v)
        if self.side not in {"LONG", "SHORT"} or type(self.strategy_eligible) is not bool:
            raise ValueError("Explicit candidate side/eligibility required")
        positive(self.stop)
        positive(self.risk_budget)
        _utc(self.requested_at)
        _utc(self.expires_at)
        if self.expires_at <= self.requested_at:
            raise ValueError("Explicit intent expiry required")


def request_from_intent(
    intent,
    *,
    canonical,
    broker_symbol,
    stop,
    risk_budget,
    expected_account_identity,
    expected_spec_revision,
    strategy_eligible,
    eligibility_evidence_id,
):
    from vision.intents.models import TradeIntent

    if not isinstance(intent, TradeIntent):
        raise ValueError("Unsized TradeIntent required")
    # Do not map BTC/USDT analysis into BTC/USD broker economics without a separate gate.
    if intent.symbol != canonical:
        raise ValueError("Explicit identical canonical intent/broker mapping required")
    return SizeRequest(
        intent.intent_id,
        canonical,
        broker_symbol,
        intent.direction.value,
        stop,
        risk_budget,
        intent.timestamp,
        intent.expires_at,
        expected_account_identity,
        expected_spec_revision,
        strategy_eligible,
        eligibility_evidence_id,
    )


@dataclass(frozen=True)
class SizingPolicy:
    per_trade_fraction: Decimal
    aggregate_fraction: Decimal
    max_age: timedelta
    max_skew: timedelta
    margin_reserve: Decimal
    loss_buffer: Decimal
    unknown_risk_policy: str
    demo_only: bool

    def __post_init__(self):
        for v in (self.per_trade_fraction, self.aggregate_fraction):
            positive(v)
            if v > 1:
                raise ValueError("Invalid risk fraction")
        for v in (self.margin_reserve, self.loss_buffer):
            nonnegative(v)
        if any(
            not isinstance(v, timedelta) or not timedelta(0) < v <= timedelta(seconds=60)
            for v in (self.max_age, self.max_skew)
        ):
            raise ValueError("Explicit bounded snapshot timing required")
        if self.unknown_risk_policy != "BLOCK" or self.demo_only is not True:
            raise ValueError("Phase 11 requires unknown-risk blocking and demo-only mode")


@dataclass(frozen=True)
class Calculation:
    kind: str
    side: str
    symbol: str
    volume: Decimal
    entry: Decimal
    stop: Decimal | None
    result: Decimal | None
    account_currency: str

    def __post_init__(self):
        if self.kind not in {"profit", "margin"} or self.side not in {"LONG", "SHORT"}:
            raise ValueError("Invalid calculation receipt")
        _identifier(self.symbol)
        _identifier(self.account_currency)
        positive(self.volume)
        positive(self.entry)
        if self.stop is not None:
            positive(self.stop)
        if self.result is not None:
            number(self.result)


@dataclass(frozen=True)
class SizingReceipt:
    request_id: str
    policy_id: str
    snapshot_id: str
    spec_revision: str | None
    account_identity: str
    account_currency: str
    status: str
    reasons: tuple[str, ...]
    volume: Decimal | None
    one_lot_loss: Decimal | None
    actual_risk: Decimal | None
    margin: Decimal | None
    open_risk: Decimal | None
    calculations: tuple[Calculation, ...]
    execution_authorized: bool = False
    sizer_version: str = "phase11-sizer-v1"

    def __post_init__(self):
        for value in (
            self.request_id,
            self.policy_id,
            self.snapshot_id,
            self.account_identity,
            self.account_currency,
        ):
            _identifier(value)
        if (
            self.status not in {"BLOCKED", "BROKER_SIZE_APPROVED"}
            or self.execution_authorized is not False
            or self.sizer_version != "phase11-sizer-v1"
        ):
            raise ValueError("Calculation-only receipt required")
        if type(self.reasons) is not tuple or (self.status == "BLOCKED") != bool(self.reasons):
            raise ValueError("Explicit immutable sizing reasons required")
        if len(self.reasons) > 16:
            raise ValueError("Sizing reason bound exceeded")
        for reason in self.reasons:
            _identifier(reason)
        if (
            type(self.calculations) is not tuple
            or len(self.calculations) > 259
            or any(not isinstance(v, Calculation) for v in self.calculations)
        ):
            raise ValueError("Bounded native calculation lineage required")
        for value in (
            self.volume,
            self.one_lot_loss,
            self.actual_risk,
            self.margin,
            self.open_risk,
        ):
            if value is not None:
                number(value)
        if self.status == "BROKER_SIZE_APPROVED" and any(
            value is None
            for value in (
                self.volume,
                self.one_lot_loss,
                self.actual_risk,
                self.margin,
                self.open_risk,
            )
        ):
            raise ValueError("Complete approved calculation required")

    @property
    def receipt_id(self):
        return digest(wire(self))

    def to_dict(self):
        return {**wire(self), "receipt_id": self.receipt_id}
