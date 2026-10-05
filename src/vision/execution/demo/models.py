"""Bounded demo execution contracts, distinct from calculation-only receipts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from vision.broker.models import nonnegative, positive
from vision.core.contracts import _identifier, _utc


class GatewayState(StrEnum):
    DISARMED = "DISARMED"
    ARMED_DEMO = "ARMED_DEMO"
    HALTED = "HALTED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    RISK_LOCKED = "RISK_LOCKED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class OrderState(StrEnum):
    CREATED = "CREATED"
    PRECHECKED = "PRECHECKED"
    SUBMITTING = "SUBMITTING"
    BROKER_ACK = "BROKER_ACK"
    FILLED = "FILLED"
    POSITION_OPEN = "POSITION_OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CRITICAL_PROTECTION_FAULT = "CRITICAL_PROTECTION_FAULT"


@dataclass(frozen=True)
class DemoPolicy:
    account_identity: str
    phase11_evidence_id: str
    phase11_accepted: bool
    max_spread: Decimal
    deviation_points: int
    receipt_ttl: timedelta
    arm_ttl: timedelta
    magic: int

    def __post_init__(self):
        _identifier(self.account_identity)
        _identifier(self.phase11_evidence_id)
        if type(self.phase11_accepted) is not bool:
            raise ValueError("Explicit operator Phase 11 decision required")
        positive(self.max_spread)
        if type(self.deviation_points) is not int or not 0 <= self.deviation_points <= 100:
            raise ValueError("Bounded explicit deviation in points required")
        if type(self.magic) is not int or not 1 <= self.magic <= 2147483647:
            raise ValueError("Dedicated bounded demo magic required")
        for v in (self.receipt_ttl, self.arm_ttl):
            if not isinstance(v, timedelta) or not timedelta(0) < v <= timedelta(seconds=60):
                raise ValueError("Explicit temporary demo timing required")


@dataclass(frozen=True)
class RiskContext:
    account_identity: str
    currency: str
    daily_baseline: Decimal
    high_water: Decimal
    observed_at: datetime

    def __post_init__(self):
        _identifier(self.account_identity)
        _identifier(self.currency)
        positive(self.daily_baseline)
        positive(self.high_water)
        _utc(self.observed_at)


@dataclass(frozen=True)
class BrokerResult:
    category: str
    request_id: str | None
    order_id: str | None
    deal_id: str | None
    volume: Decimal | None
    price: Decimal | None

    def __post_init__(self):
        if self.category not in {"ACK", "REJECTED", "UNKNOWN", "PARTIAL"}:
            raise ValueError("Known broker result category required")
        for v in (self.request_id, self.order_id, self.deal_id):
            if v is not None:
                _identifier(v)
        for v in (self.volume, self.price):
            if v is not None:
                nonnegative(v)


@dataclass(frozen=True)
class BrokerFacts:
    """Account-pinned native facts; no ticket assumption from an order response."""

    account_identity: str
    currency: str
    orders: tuple[dict, ...]
    deals: tuple[dict, ...]
    positions: tuple[dict, ...]
    observed_at: datetime

    def __post_init__(self):
        _identifier(self.account_identity)
        _identifier(self.currency)
        _utc(self.observed_at)
        for values in (self.orders, self.deals, self.positions):
            if type(values) is not tuple or len(values) > 256:
                raise ValueError("Bounded known broker facts required")
