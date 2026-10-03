"""Hard entry vetoes for paper research; no strategy or live execution."""

from dataclasses import dataclass
from decimal import Decimal

from vision.execution.paper.models import exact, nonnegative


@dataclass(frozen=True, slots=True)
class RiskLimits:
    per_trade_fraction: Decimal = Decimal("0.01")
    portfolio_fraction: Decimal = Decimal("0.05")
    daily_equity_loss_fraction: Decimal = Decimal("0.03")
    drawdown_fraction: Decimal = Decimal("0.10")
    symbol_exposure_fraction: Decimal = Decimal("0.25")
    gross_exposure_fraction: Decimal = Decimal("0.80")

    def __post_init__(self):
        for value in (
            self.per_trade_fraction,
            self.portfolio_fraction,
            self.daily_equity_loss_fraction,
            self.drawdown_fraction,
            self.symbol_exposure_fraction,
            self.gross_exposure_fraction,
        ):
            nonnegative(value)
            if not 0 < value <= 1:
                raise ValueError("Risk fractions must be in (0, 1]")


class HardRiskGovernor:
    def __init__(self, limits):
        if not isinstance(limits, RiskLimits):
            raise ValueError("Explicit risk limits required")
        self.limits = limits

    def assess(self, **values):
        with exact():
            return self._assess(**values)

    def _assess(
        self,
        *,
        equity,
        trade_risk,
        open_risk,
        projected_equity,
        symbol_exposure,
        gross_exposure,
        daily_baseline,
        high_water,
    ):
        if equity is None or projected_equity is None:
            return ("EQUITY_UNKNOWN",)
        if equity <= 0 or projected_equity <= 0:
            return ("EQUITY_NONPOSITIVE",)
        if open_risk is None or trade_risk is None:
            return ("UNKNOWN_OPEN_RISK",)
        limits = self.limits
        reasons = []
        if trade_risk > equity * limits.per_trade_fraction:
            reasons.append("PER_TRADE_RISK_CAP")
        if open_risk + trade_risk > equity * limits.portfolio_fraction:
            reasons.append("PORTFOLIO_RISK_CAP")
        if min(equity, projected_equity) <= daily_baseline * (
            1 - limits.daily_equity_loss_fraction
        ):
            reasons.append("DAILY_EQUITY_LOSS_LIMIT")
        if min(equity, projected_equity) <= high_water * (1 - limits.drawdown_fraction):
            reasons.append("MAX_DRAWDOWN")
        if symbol_exposure > projected_equity * limits.symbol_exposure_fraction:
            reasons.append("SYMBOL_EXPOSURE_CAP")
        if gross_exposure > projected_equity * limits.gross_exposure_fraction:
            reasons.append("GROSS_EXPOSURE_CAP")
        return tuple(reasons)
