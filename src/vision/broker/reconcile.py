"""Mandatory local demo evidence capture; never fabricate acceptance from CI."""

from vision.broker.models import SizeRequest
from vision.broker.mt5_bridge import CANONICAL
from vision.core.instruments import digest


def reconcile(reader, policy, plans):
    """Plans explicitly supply LONG/SHORT SLs and budgets for all four canonical assets.

    Operator must compare captured specs/calculations/account currency with terminal
    UI and sign the report locally. No real account identifiers are exported.
    """
    if set(plans) != set(CANONICAL) or set(reader.mapping) != set(CANONICAL):
        raise ValueError("Four explicitly mapped demo instruments required")
    evidence = []
    for canonical in CANONICAL:
        if set(plans[canonical]) != {"LONG", "SHORT", "risk_budget"}:
            raise ValueError("Explicit stops/budget for both sides required")
        for side in ("LONG", "SHORT"):
            snapshot = reader.snapshot()
            spec = next(v for v in snapshot.specs if v.canonical == canonical)
            now = reader.clock()
            from datetime import timedelta

            request = SizeRequest(
                digest({"canonical": canonical, "side": side, "snapshot": snapshot.snapshot_id}),
                canonical,
                spec.symbol,
                side,
                plans[canonical][side],
                plans[canonical]["risk_budget"],
                now,
                now + timedelta(seconds=30),
                snapshot.account.identity,
                spec.revision,
                True,
                "manual-demo-calculation-protocol",
            )
            audit = reader.audit_size(request, policy)
            evidence.append(
                {
                    "canonical": canonical,
                    "side": side,
                    "audit": audit,
                }
            )
    report = {
        "protocol": "phase11-demo-v1",
        "health": reader.health(),
        "evidence": evidence,
        "status": "AWAITING_OPERATOR_RECONCILIATION",
        "acceptance_passed": False,
        "required_checks": [
            "suffix_mapping",
            "profit_margin_native_reconciliation",
            "stop_levels",
            "volume_grid",
            "account_currency",
            "terminal_ui_comparison",
        ],
        "execution_authorized": False,
    }
    return {**report, "report_id": digest(report)}
