"""Deterministic TP/profit-lock/trailing previews; no broker mutation capability."""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from vision.broker.models import nonnegative, positive


@dataclass(frozen=True)
class ProtectionRules:
    target_r: Decimal
    activate_r: Decimal
    lock_r: Decimal
    trail_r: Decimal

    def __post_init__(self):
        for value in (self.target_r, self.activate_r, self.trail_r):
            positive(value)
        nonnegative(self.lock_r)
        if self.lock_r >= self.activate_r:
            raise ValueError("Profit lock must be below activation profit")


def preview(
    *, side, entry, initial_stop, accepted_stop, bid, ask, tick_size, minimum_distance, rules
):
    """Prices/limits must be native inputs. This preview grants no send permission."""
    for value in (entry, initial_stop, accepted_stop, bid, ask, tick_size):
        positive(value)
    nonnegative(minimum_distance)
    if side not in {"LONG", "SHORT"} or ask < bid or not isinstance(rules, ProtectionRules):
        raise ValueError("Explicit direction, native quote and protection rules required")
    sign = Decimal(1 if side == "LONG" else -1)
    risk = (entry - initial_stop) * sign
    if risk <= 0:
        raise ValueError("Original protective SL required")
    trigger = bid if side == "LONG" else ask
    floor = ROUND_FLOOR if side == "LONG" else ROUND_CEILING
    target_tp = ((entry + sign * risk * rules.target_r) / tick_size).to_integral_value(
        rounding=ROUND_CEILING if side == "LONG" else ROUND_FLOOR
    ) * tick_size
    base = {
        "execution_authorized": False,
        "target_tp": str(target_tp),
        "accepted_stop": str(accepted_stop),
        "proposed_stop": None,
    }
    if (trigger - entry) * sign < risk * rules.activate_r:
        return {**base, "state": "WAIT_ACTIVATION"}
    lock = entry + sign * risk * rules.lock_r
    trailing = trigger - sign * risk * rules.trail_r
    candidate = max(lock, trailing) if side == "LONG" else min(lock, trailing)
    candidate = (candidate / tick_size).to_integral_value(rounding=floor) * tick_size
    if (candidate - accepted_stop) * sign <= 0:
        return {**base, "state": "HOLD_EXISTING_SL"}
    if (trigger - candidate) * sign < max(minimum_distance, tick_size):
        return {**base, "state": "BLOCKED_BROKER_DISTANCE"}
    return {**base, "state": "PREVIEW_ONLY", "proposed_stop": str(candidate)}
