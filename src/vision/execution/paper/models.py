"""Immutable research-only simulation contracts. No broker/network capabilities."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, Inexact, localcontext
from enum import StrEnum

from vision.core.contracts import _identifier, _utc
from vision.core.state.portfolio import Position, Side, number


def exact():
    context = Context(prec=1024, Emin=-4096, Emax=4096)
    context.traps[Inexact] = True
    return localcontext(context)


def nonnegative(value):
    number(value)
    if value.is_signed():
        raise ValueError("Unsigned Decimal required")


class PaperMode(StrEnum):
    IDEAL = "PAPER_IDEAL"
    REALISTIC = "PAPER_REALISTIC"


class OrderState(StrEnum):
    NEW = "NEW"
    VALIDATING = "VALIDATING"
    RISK_APPROVED = "RISK_APPROVED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class PaperCosts:
    mode: PaperMode = PaperMode.REALISTIC
    spread_bps: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("2")
    commission_bps: Decimal = Decimal("10")

    def __post_init__(self):
        if not isinstance(self.mode, PaperMode):
            raise ValueError("Explicit paper mode required")
        for value in (self.spread_bps, self.slippage_bps, self.commission_bps):
            nonnegative(value)
            if value > 1000:
                raise ValueError("Paper costs exceed supported bounds")


@dataclass(frozen=True, slots=True)
class MarketRules:
    instrument_id: str
    spec_revision: str
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    quantity_step: Decimal
    minimum_notional: Decimal
    maximum_notional: Decimal | None
    evidence_digest: str
    model: str = "cash_spot"

    def __post_init__(self):
        for value in (self.instrument_id, self.spec_revision, self.evidence_digest):
            _identifier(value)
        for value in (self.minimum_quantity, self.minimum_notional):
            nonnegative(value)
        for value in (self.maximum_quantity, self.quantity_step):
            number(value)
            if value <= 0:
                raise ValueError("Positive explicit market limits required")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("Invalid quantity limits")
        if self.maximum_notional is not None:
            number(self.maximum_notional)
            if self.maximum_notional < self.minimum_notional:
                raise ValueError("Invalid notional limits")
        if self.model != "cash_spot":
            raise ValueError("Margin model is unverified")


@dataclass(frozen=True, slots=True)
class PaperOrder:
    request_id: str
    instrument_id: str
    spec_revision: str
    side: Side
    quantity: Decimal
    stop_loss: Decimal | None
    created_at: datetime
    order_type: str = "MARKET"
    close_position_id: str | None = None

    def __post_init__(self):
        for value in (self.request_id, self.instrument_id, self.spec_revision):
            _identifier(value)
        if not isinstance(self.side, Side):
            raise ValueError("Explicit side required")
        number(self.quantity)
        if self.quantity <= 0:
            raise ValueError("Positive quantity required")
        if self.stop_loss is not None:
            number(self.stop_loss)
            if self.stop_loss <= 0:
                raise ValueError("Positive stop required")
        _utc(self.created_at)
        if self.close_position_id is not None:
            _identifier(self.close_position_id)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    risk_decision_id: str
    order_id: str
    allowed: bool
    reasons: tuple[str, ...]
    equity: Decimal | None
    trade_risk: Decimal | None
    portfolio_risk: Decimal | None
    as_of: datetime
    quote_event_id: str | None


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    risk_decision_id: str
    order_id: str
    position_id: str
    price: Decimal
    quantity: Decimal
    commission: Decimal
    at: datetime
    quote_event_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PaperPosition:
    position: Position
    stop_loss: Decimal
    entry_fill_id: str
    entry_fee: Decimal
    close_fill_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    order: PaperOrder
    order_id: str
    states: tuple[OrderState, ...]
    decision: RiskDecision
    fill: Fill | None
