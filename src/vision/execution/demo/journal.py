"""Demo lifecycle in the append-only Outcome Journal, with durable reservations."""

from vision.core.contracts import _identifier
from vision.core.instruments import digest
from vision.execution.demo.models import OrderState
from vision.journal.repository import Pending, canonical, verify

TERMINAL = {"CLOSED", "REJECTED"}
TRANSITIONS = {
    None: {"CREATED"},
    "CREATED": {"PRECHECKED", "REJECTED"},
    "PRECHECKED": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {"BROKER_ACK", "UNKNOWN", "REJECTED", "RECONCILIATION_REQUIRED"},
    "BROKER_ACK": {"FILLED", "RECONCILIATION_REQUIRED", "CRITICAL_PROTECTION_FAULT"},
    "FILLED": {"POSITION_OPEN", "CLOSED", "RECONCILIATION_REQUIRED", "CRITICAL_PROTECTION_FAULT"},
    "POSITION_OPEN": {"CLOSING", "CLOSED", "RECONCILIATION_REQUIRED", "CRITICAL_PROTECTION_FAULT"},
    "CLOSING": {"CLOSED", "UNKNOWN", "RECONCILIATION_REQUIRED", "CRITICAL_PROTECTION_FAULT"},
    "UNKNOWN": {"POSITION_OPEN", "CLOSED", "RECONCILIATION_REQUIRED", "CRITICAL_PROTECTION_FAULT"},
    "RECONCILIATION_REQUIRED": {"POSITION_OPEN", "CLOSED", "CRITICAL_PROTECTION_FAULT"},
    "CRITICAL_PROTECTION_FAULT": {"CLOSED", "RECONCILIATION_REQUIRED"},
    "CLOSED": set(),
    "REJECTED": set(),
}


def validate_payload(payload):
    from vision.analysis.contracts import arithmetic

    with arithmetic():
        return _validate_payload(payload)


def _validate_payload(payload):
    from vision.broker.codec import decimal, dto, replay, snapshot_from_dict
    from vision.core.codec import utc_string
    from vision.execution.demo.models import RiskContext
    from vision.execution.demo.native import comment
    from vision.risk.governor import HardRiskGovernor, RiskLimits

    details, lineage = payload["details"], payload["lineage"]
    utc_string(details["created_at"])
    if "request" not in details:
        if payload["state"] not in {"CREATED", "REJECTED"}:
            raise ValueError("Missing execution precheck lineage")
        return
    audit, req = details["fresh_sizing_audit"], details["request"]
    receipt = replay(audit)
    original = replay(details["original_sizing_audit"])
    context, limits = details["risk_context"], details["risk_limits"]
    ctx, lim = dto(RiskContext, context), dto(RiskLimits, limits)
    account = snapshot_from_dict(audit["after_snapshot"]).account
    decision_id = digest({"context": context, "limits": limits, "receipt": original.receipt_id})
    if (
        receipt.status != "BROKER_SIZE_APPROVED"
        or original.status != "BROKER_SIZE_APPROVED"
        or original.receipt_id != lineage["sizing_receipt_id"]
        or original.request_id != receipt.request_id
        or original.policy_id != receipt.policy_id
        or any(
            getattr(original, key) != getattr(receipt, key)
            for key in ("volume", "one_lot_loss", "actual_risk", "margin", "open_risk")
        )
        or decision_id != lineage["risk_decision_id"]
        or ctx.account_identity != account.identity
        or ctx.currency != account.currency
        or receipt.account_identity != lineage["account_identity"]
        or audit["request"]["intent_id"] != lineage["intent_id"]
        or req["symbol"] != audit["request"]["broker_symbol"]
        or req["side"] != audit["request"]["side"]
        or decimal(req["volume"]) != receipt.volume
        or decimal(req["stop"]) != decimal(audit["request"]["stop"])
        or req["comment"] != comment(payload["client_order_id"])
        or details["currency"] != receipt.account_currency
        or decimal(details["risk"]) != receipt.actual_risk
        or decimal(details["margin"]) != receipt.margin
    ):
        raise ValueError("Demo precheck/native sizing replay mismatch")
    exposure = receipt.margin + receipt.actual_risk
    if HardRiskGovernor(lim).assess(
        equity=account.equity,
        trade_risk=receipt.actual_risk,
        open_risk=receipt.open_risk,
        projected_equity=account.equity - receipt.actual_risk,
        symbol_exposure=exposure,
        gross_exposure=exposure,
        daily_baseline=ctx.daily_baseline,
        high_water=ctx.high_water,
    ):
        raise ValueError("Demo governor replay veto")


def replay(entries):
    """Validate chain, lineage, account reservation and legal lifecycle transitions."""
    verify(entries)
    heads, intents = {}, {}
    for entry in entries:
        if entry.kind != "demo_execution":
            continue
        p = entry.payload
        if set(p) != {"client_order_id", "state", "lineage", "details", "version"}:
            raise ValueError("Exact demo journal envelope required")
        if p["version"] != "phase12-demo-v1":
            raise ValueError("Demo journal version mismatch")
        cid, state, lineage = p["client_order_id"], p["state"], p["lineage"]
        _identifier(cid)
        OrderState(state)
        if set(lineage) != {
            "intent_id",
            "synthesis_id",
            "signal_ids",
            "source_lineage",
            "risk_decision_id",
            "sizing_receipt_id",
            "account_identity",
            "phase11_evidence_id",
            "request_hash",
        }:
            raise ValueError("Complete demo lineage required")
        validate_payload(p)
        previous = heads.get(cid)
        old = previous["state"] if previous else None
        if state not in TRANSITIONS[old] or previous and lineage != previous["lineage"]:
            raise ValueError("Demo lifecycle or lineage mismatch")
        if previous is not None:
            for k in (
                "created_at",
                "request",
                "fresh_sizing_audit",
                "original_sizing_audit",
                "risk_context",
                "risk_limits",
                "currency",
                "expected_entry",
                "spread",
                "risk",
                "margin",
            ):
                if k in previous["details"] and previous["details"][k] != p["details"].get(k):
                    raise ValueError("Committed demo sizing lineage changed")
        if previous is None:
            intent = lineage["intent_id"]
            if intent in intents:
                raise ValueError("Duplicate demo intent")
            if any(h["state"] not in TERMINAL for h in heads.values()):
                raise ValueError("Concurrent demo reservation unsupported")
            intents[intent] = cid
        heads[cid] = p
    return heads


class DemoJournal:
    def __init__(self, repository, *, experiment_id):
        _identifier(experiment_id)
        self.repository, self.experiment_id = repository, experiment_id
        self.heads()

    def heads(self):
        return replay(self.repository.entries())

    def append(self, client_order_id, state, lineage, details, *, at):
        payload = {
            "client_order_id": client_order_id,
            "state": str(state),
            "lineage": lineage,
            "details": details,
            "version": "phase12-demo-v1",
        }

        def build(entries):
            validate_payload(payload)
            heads = replay(entries)
            old = heads.get(client_order_id)
            before = old["state"] if old else None
            if str(state) not in TRANSITIONS[before] or old and old["lineage"] != lineage:
                raise ValueError("Illegal demo transition or changed lineage")
            if old is None:
                if any(h["lineage"]["intent_id"] == lineage["intent_id"] for h in heads.values()):
                    raise ValueError("Duplicate demo intent")
                if any(h["state"] not in TERMINAL for h in heads.values()):
                    raise ValueError("Active demo reservation requires reconciliation")
            return Pending(
                "demo_execution",
                self.experiment_id,
                digest({"client": client_order_id, "sequence": len(entries) + 1}),
                canonical(payload),
            )

        self.repository.transact(at, build)  # SQLite FULL commit happens before native call.
        return payload
