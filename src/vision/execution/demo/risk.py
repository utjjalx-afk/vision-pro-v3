"""Explicit DEMO experiment options; shared/live risk policy is never changed."""

from vision.core.instruments import digest


def decision_id(context, limits, receipt, daily_loss_limit_enabled=True):
    body = {"context": context, "limits": limits, "receipt": receipt}
    if not daily_loss_limit_enabled:
        body["demo_daily_loss_limit_enabled"] = False
    return digest(body)


def assess(governor, *, daily_loss_limit_enabled=True, **values):
    if type(daily_loss_limit_enabled) is not bool:
        raise ValueError("Explicit DEMO daily loss setting required")
    reasons = governor.assess(**values)
    return tuple(
        reason
        for reason in reasons
        if daily_loss_limit_enabled or reason != "DAILY_EQUITY_LOSS_LIMIT"
    )
