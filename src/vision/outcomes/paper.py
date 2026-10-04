"""Executed paper outcome grading; never directional lane-reliability learning."""

from vision.analysis.contracts import arithmetic, wire
from vision.core.codec import event_from_dict
from vision.core.contracts import QuotePayload
from vision.core.instruments import digest
from vision.execution.paper.broker import PaperBroker
from vision.execution.paper.models import exact
from vision.journal.models import OutcomeState, TradeLineage, TradeOutcome


def grade(
    checkpoint,
    request_id,
    *,
    at,
    receipt_id,
    assessments=(),
    synthesis_id=None,
    intent_id=None,
    declaration=None,
    supersedes=None,
    correction_reason=None,
    correction_evidence_ids=(),
):
    broker = PaperBroker.from_checkpoint(checkpoint)
    result = broker.orders.get(request_id)
    if result is None or result.order.close_position_id is not None:
        raise ValueError("Actual paper entry request required")
    if broker.last_at > at:
        raise ValueError("Future paper checkpoint")
    entry, exit_result = result.fill, None
    if entry is not None:
        exit_result = next(
            (
                r
                for r in broker.orders.values()
                if r.fill is not None and r.order.close_position_id == entry.position_id
            ),
            None,
        )
    root = digest({"session": digest(broker.config()), "entry_order": result.order_id})
    state = OutcomeState.NO_RESULT
    pnl = fees = shortfall = r_multiple = None
    risk = result.decision.trade_risk if entry is not None else None
    if entry is not None and exit_result is not None:
        exit_fill = exit_result.fill
        state = (
            OutcomeState.STOP_HIT
            if exit_fill.reason in {"STOP", "GAP_THROUGH_STOP"}
            else OutcomeState.MANUAL_EXIT
        )
        if declaration is not None:
            plan, declared_at = declaration
            if (
                plan.position_id != entry.position_id
                or plan.exit_request_id != exit_result.order.request_id
                or declared_at > exit_fill.at
            ):
                raise ValueError(
                    "Exit classification requires a declaration before the actual matching close"
                )
            if state is not OutcomeState.STOP_HIT:
                if plan.state is OutcomeState.TARGET_HIT and exit_fill.price < plan.target_price:
                    raise ValueError("Actual fill did not reach the declared target")
                if plan.state is OutcomeState.EXPIRED and exit_fill.at < plan.expires_at:
                    raise ValueError("Actual close predates declared expiry")
                state = plan.state
        events = [
            event_from_dict(e["command"]["event"])
            for e in broker.journal
            if e["command"]["op"] == "quote"
        ]

        def quote(fill):
            matches = [e for e in events if e.event_id == fill.quote_event_id]
            if len(matches) != 1 or not isinstance(matches[0].payload, QuotePayload):
                raise ValueError("Exact fill quote provenance required")
            return matches[0].payload

        with exact():
            fees = entry.commission + exit_fill.commission
            pnl = (exit_fill.price - entry.price) * entry.quantity - fees
            shortfall = (
                (entry.price - quote(entry).ask) + (quote(exit_fill).bid - exit_fill.price)
            ) * entry.quantity
        if risk is not None and risk > 0:
            with arithmetic():
                r_multiple = pnl / risk
    identity = digest(
        {
            "receipt": receipt_id,
            "request": request_id,
            "state": state.value,
            "supersedes": supersedes,
            "correction_reason": correction_reason,
            "evidence": correction_evidence_ids,
            "declaration": wire(declaration[0]) if declaration else None,
            "version": "phase7-paper-v1",
        }
    )
    lineage = TradeLineage(
        tuple(assessments),
        synthesis_id,
        intent_id,
        result.decision.risk_decision_id,
        result.order_id,
        entry.fill_id if entry else None,
        entry.position_id if entry else None,
        exit_result.fill.fill_id if exit_result else None,
        exit_result.order_id if exit_result else None,
        identity,
    )
    return TradeOutcome(
        identity,
        root,
        state,
        broker.currency,
        pnl,
        fees,
        shortfall,
        risk,
        r_multiple,
        at,
        lineage,
        receipt_id,
        supersedes,
        correction_reason,
        tuple(correction_evidence_ids),
    )
