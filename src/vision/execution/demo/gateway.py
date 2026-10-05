"""Explicit demo entry/exit, durable no-resend protocol and broker fact reconciliation."""

from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock

from vision.analysis.contracts import arithmetic, wire
from vision.broker.codec import replay, snapshot_from_dict
from vision.broker.models import SizeRequest, SizingPolicy, positive
from vision.core.contracts import _identifier, _utc
from vision.core.instruments import digest
from vision.execution.demo.journal import TERMINAL, DemoJournal
from vision.execution.demo.models import DemoPolicy, GatewayState, OrderState, RiskContext
from vision.execution.demo.native import DispatchBlocked, comment, stable
from vision.intents.models import TradeIntent
from vision.risk.governor import HardRiskGovernor


class DemoGateway:
    def __init__(
        self,
        transport,
        journal,
        policy,
        governor,
        *,
        enabled=False,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(journal, DemoJournal)
            or not isinstance(policy, DemoPolicy)
            or not isinstance(governor, HardRiskGovernor)
            or type(enabled) is not bool
        ):
            raise ValueError("Explicit demo contracts required")
        self.transport, self.journal, self.policy, self.governor = (
            transport,
            journal,
            policy,
            governor,
        )
        self.enabled, self.clock = enabled, clock
        self.state, self.armed_until = GatewayState.DISARMED, None
        self.lock = RLock()
        # Persistent orders survive restarts; arming never does.
        self.journal.heads()

    def halt(self):
        with self.lock:
            self.state, self.armed_until = GatewayState.HALTED, None

    def disarm(self):
        with self.lock:
            self.state, self.armed_until = GatewayState.DISARMED, None

    def arm(self, *, account_identity, operator_confirmation):
        with self.lock, self.transport.lock:
            if (
                not self.enabled
                or not self.policy.phase11_accepted
                or operator_confirmation != "ARMED_DEMO"
                or account_identity != self.policy.account_identity
            ):
                raise ValueError("Accepted Phase 11, enabled demo and temporary arm required")
            if self.state not in {GatewayState.DISARMED, GatewayState.ARMED_DEMO}:
                raise ValueError("Incident state requires restart and broker reconciliation")
            heads = self.journal.heads()
            if any(h["state"] not in TERMINAL | {"POSITION_OPEN"} for h in heads.values()):
                raise ValueError("Unresolved submission requires broker reconciliation")
            a = self.transport.snapshot().account
            if not a.demo or not a.connected or a.identity != account_identity:
                raise ValueError("Pinned connected demo account required")
            self.armed_until = self.clock() + self.policy.arm_ttl
            self.state = GatewayState.ARMED_DEMO

    def _armed(self):
        now = self.clock()
        _utc(now)
        if self.armed_until is not None and now >= self.armed_until:
            self.disarm()
        if (
            not self.enabled
            or not self.policy.phase11_accepted
            or self.state != GatewayState.ARMED_DEMO
        ):
            raise ValueError("DEMO_SESSION_DISARMED")
        return now

    def _event(self, head, state, **details):
        merged = {**head["details"], **details}
        return self.journal.append(
            head["client_order_id"], state, head["lineage"], merged, at=self.clock()
        )

    def execute(
        self,
        *,
        client_order_id,
        intent,
        request,
        sizing_policy,
        sizing_audit,
        sized_at,
        risk_context,
        tp=None,
    ):
        """One explicit intent; no strategy loop, arbitrary raw orders or automatic retries."""
        with self.lock, self.transport.lock, arithmetic():
            _identifier(client_order_id)
            _utc(sized_at)
            if (
                not isinstance(intent, TradeIntent)
                or not isinstance(request, SizeRequest)
                or not isinstance(sizing_policy, SizingPolicy)
                or not isinstance(risk_context, RiskContext)
            ):
                raise ValueError("Typed candidate, native sizing and risk context required")
            if tp is not None:
                positive(tp)
            command_hash = digest(
                {
                    "intent": wire(intent),
                    "request": wire(request),
                    "policy": wire(sizing_policy),
                    "audit": sizing_audit,
                    "sized_at": wire(sized_at),
                    "risk_context": wire(risk_context),
                    "tp": wire(tp),
                }
            )
            heads = self.journal.heads()
            if client_order_id in heads:
                old = heads[client_order_id]
                if old["lineage"]["request_hash"] != command_hash:
                    raise ValueError("CLIENT_ORDER_ID_COLLISION")
                return old  # Includes rejected/unknown states; never resubmits.
            now = self._armed()
            if not (
                intent.intent_id == request.intent_id
                and intent.symbol == request.canonical
                and intent.direction.value == request.side
                and intent.timestamp == request.requested_at
                and intent.expires_at == request.expires_at
                and request.strategy_eligible
            ):
                raise ValueError("INTENT_SIZING_LINEAGE_MISMATCH")
            supplied = replay(sizing_audit)
            if (
                supplied.status != "BROKER_SIZE_APPROVED"
                or supplied.request_id != digest(wire(request))
                or supplied.policy_id != digest(wire(sizing_policy))
                or not 0
                <= (now - sized_at).total_seconds()
                <= self.policy.receipt_ttl.total_seconds()
                or sizing_audit["now"] != wire(sized_at)
            ):
                raise ValueError("STALE_OR_UNAPPROVED_SIZING")
            lineage = {
                "intent_id": intent.intent_id,
                "synthesis_id": intent.decision_id,
                "signal_ids": list(intent.evidence_hashes),
                "source_lineage": intent.source_lineage,
                "risk_decision_id": digest(
                    {
                        "context": wire(risk_context),
                        "limits": wire(self.governor.limits),
                        "receipt": supplied.receipt_id,
                    }
                ),
                "sizing_receipt_id": supplied.receipt_id,
                "account_identity": self.policy.account_identity,
                "phase11_evidence_id": self.policy.phase11_evidence_id,
                "request_hash": command_hash,
            }
            head = self.journal.append(
                client_order_id, OrderState.CREATED, lineage, {"created_at": wire(now)}, at=now
            )
            try:
                snapshot = self.transport.snapshot()
                original = snapshot_from_dict(sizing_audit["after_snapshot"])
                if (
                    not snapshot.account.demo
                    or not snapshot.account.connected
                    or snapshot.account.identity != self.policy.account_identity
                ):
                    raise DispatchBlocked("DEMO_ACCOUNT_ALLOWLIST_REQUIRED")
                if snapshot.positions or snapshot.pending_orders:
                    raise DispatchBlocked("ISOLATED_ACCOUNT_NO_PYRAMIDING_REQUIRED")
                if stable(snapshot) != stable(original):
                    raise DispatchBlocked("BROKER_STATE_CHANGED_RECALCULATE")
                self.transport.readiness(request.broker_symbol)
                quote = next(q for q in snapshot.quotes if q.symbol == request.broker_symbol)
                spec = next(s for s in snapshot.specs if s.symbol == request.broker_symbol)
                if quote.ask - quote.bid > self.policy.max_spread:
                    raise DispatchBlocked("SPREAD_CAP")
                if tp is not None and (
                    tp % spec.tick_size
                    or (
                        request.side == "LONG"
                        and tp - quote.bid < max(spec.stops_level, spec.freeze_level) * spec.point
                    )
                    or (
                        request.side == "SHORT"
                        and quote.ask - tp < max(spec.stops_level, spec.freeze_level) * spec.point
                    )
                ):
                    raise DispatchBlocked("INVALID_TP")
                fresh_audit = self.transport.audit_size(request, sizing_policy)
                fresh = replay(fresh_audit)
                if fresh.status != "BROKER_SIZE_APPROVED":
                    raise DispatchBlocked("FRESH_SIZING_BLOCKED:" + ",".join(fresh.reasons))
                final = snapshot_from_dict(fresh_audit["after_snapshot"])
                if stable(final) != stable(snapshot):
                    raise DispatchBlocked("BROKER_STATE_CHANGED_RECALCULATE")
                economic_fields = ("volume", "one_lot_loss", "actual_risk", "margin", "open_risk")
                if any(getattr(fresh, k) != getattr(supplied, k) for k in economic_fields):
                    raise DispatchBlocked("NATIVE_ECONOMICS_CHANGED_RECALCULATE")
                ctx, a = risk_context, final.account
                if (
                    ctx.account_identity != a.identity
                    or ctx.currency != a.currency
                    or ctx.observed_at.date() != now.date()
                    or not 0
                    <= (now - ctx.observed_at).total_seconds()
                    <= sizing_policy.max_age.total_seconds()
                    or ctx.high_water < a.equity
                ):
                    raise DispatchBlocked("RISK_BASELINE_STALE_OR_INVALID")
                # Existing paper governor uses explicitly declared margin-at-risk exposure
                # for this isolated CFD account, never a guessed notional multiplier.
                exposure = fresh.margin + fresh.actual_risk
                reasons = self.governor.assess(
                    equity=a.equity,
                    trade_risk=fresh.actual_risk,
                    open_risk=fresh.open_risk,
                    projected_equity=a.equity - fresh.actual_risk,
                    symbol_exposure=exposure,
                    gross_exposure=exposure,
                    daily_baseline=ctx.daily_baseline,
                    high_water=ctx.high_water,
                )
                if reasons:
                    self.state = GatewayState.RISK_LOCKED
                    raise DispatchBlocked("RISK_VETO:" + ",".join(reasons))
                raw = {
                    "symbol": request.broker_symbol,
                    "side": request.side,
                    "volume": str(fresh.volume),
                    "stop": str(request.stop),
                    "tp": str(tp) if tp is not None else None,
                    "deviation": self.policy.deviation_points,
                    "comment": comment(client_order_id),
                }
                head = self._event(
                    head,
                    OrderState.PRECHECKED,
                    request=raw,
                    fresh_sizing_audit=fresh_audit,
                    currency=a.currency,
                    expected_entry=str(quote.ask if request.side == "LONG" else quote.bid),
                    spread=str(quote.ask - quote.bid),
                    risk=str(fresh.actual_risk),
                    margin=str(fresh.margin),
                )
                self._armed()  # Recheck expiry after native calculation latency.
                head = self._event(head, OrderState.SUBMITTING)
                result = self.transport.send(
                    {**raw, "volume": fresh.volume, "stop": request.stop, "tp": tp},
                    expected_snapshot=final,
                )
            except DispatchBlocked as error:
                if head["state"] == "SUBMITTING":
                    # Native transport asserts this category occurs before order_send.
                    return self._event(head, OrderState.REJECTED, reason=str(error))
                return self._event(head, OrderState.REJECTED, reason=str(error))
            except Exception:
                if head["state"] == "SUBMITTING":
                    self.state = GatewayState.RECONCILIATION_REQUIRED
                    return self._event(head, OrderState.UNKNOWN, reason="SUBMISSION_UNCERTAIN")
                self.state = GatewayState.BROKER_UNAVAILABLE
                return self._event(head, OrderState.REJECTED, reason="PRECHECK_UNAVAILABLE")
            if result.category == "REJECTED":
                return self._event(head, OrderState.REJECTED, broker_result=wire(result))
            if result.category in {"UNKNOWN", "PARTIAL"}:
                self.state = GatewayState.RECONCILIATION_REQUIRED
                side_state = (
                    OrderState.UNKNOWN
                    if result.category == "UNKNOWN"
                    else OrderState.RECONCILIATION_REQUIRED
                )
                return self._event(head, side_state, broker_result=wire(result))
            head = self._event(head, OrderState.BROKER_ACK, broker_result=wire(result))
            return self.monitor(client_order_id)

    def monitor(self, client_order_id):
        """Read-only monitoring remains available when disarmed/halted; no implicit repair."""
        from vision.core.codec import utc_string

        with self.lock, self.transport.lock, arithmetic():
            head = self.journal.heads()[client_order_id]
            if head["state"] in TERMINAL:
                return head
            if head["state"] in {"CREATED", "PRECHECKED"}:
                return self._event(head, OrderState.REJECTED, incident="NO_SUBMISSION_COMMITTED")
            if head["state"] == "SUBMITTING":
                head = self._event(head, OrderState.UNKNOWN, incident="RESTART_DURING_SUBMISSION")
            if "request" not in head["details"]:
                self.state = GatewayState.RECONCILIATION_REQUIRED
                return head
            try:
                facts = self.transport.facts(since=utc_string(head["details"]["created_at"]))
                if (
                    facts.account_identity != self.policy.account_identity
                    or facts.currency != head["details"]["currency"]
                ):
                    raise ValueError("Broker provenance changed")
                req = head["details"]["request"]

                def match(r):
                    return r["magic"] == self.policy.magic and r["comment"] == req["comment"]

                orders = [o for o in facts.orders if match(o)]
                order_ids = {o["order_id"] for o in orders}
                response = head["details"].get("broker_result", {})
                if response.get("order_id"):
                    order_ids.add(response["order_id"])
                deals = [d for d in facts.deals if match(d) or d["order_id"] in order_ids]
                position_ids = {d["position_id"] for d in deals}
                # Broker-triggered SL/TP exits may replace the comment and order id.
                # Proven entry position identity, never name similarity, links those deals.
                known_deal_ids = {d["deal_id"] for d in deals}
                deals += [
                    d
                    for d in facts.deals
                    if d["position_id"] in position_ids and d["deal_id"] not in known_deal_ids
                ]
                positions = [
                    p for p in facts.positions if match(p) or p["position_id"] in position_ids
                ]
                # Missing protection has priority over volume/ownership faults.
                if any(Decimal(p["stop"]) <= 0 for p in positions):
                    self.state = GatewayState.HALTED
                    if head["state"] != "CRITICAL_PROTECTION_FAULT":
                        return self._event(
                            head,
                            OrderState.CRITICAL_PROTECTION_FAULT,
                            incident="MISSING_ACCEPTED_SL",
                            facts=wire(facts),
                        )
                    return head
                if (
                    len(positions) > 1
                    or len(positions) != len(facts.positions)
                    or any(o["active"] for o in facts.orders)
                ):
                    raise ValueError("Unknown positions, duplicate or active broker orders")
                incoming = [d for d in deals if d["entry"] == 0]
                outgoing = [d for d in deals if d["entry"] == 1]
                if any(d["entry"] not in {0, 1} for d in deals):
                    raise ValueError("Unsupported deal ownership semantics")
                if (
                    not incoming
                    or any(
                        d["symbol"] != req["symbol"] or d["side"] != req["side"] for d in incoming
                    )
                    or sum((Decimal(d["volume"]) for d in incoming), Decimal(0))
                    != Decimal(req["volume"])
                ):
                    raise ValueError("Fill volume, symbol or side mismatch")
                if len(position_ids) != 1:
                    raise ValueError("Ambiguous broker position lineage")
                filled = sum(
                    (Decimal(d["volume"]) * Decimal(d["price"]) for d in incoming), Decimal(0)
                ) / Decimal(req["volume"])
                if positions:
                    p = positions[0]
                    if (
                        outgoing
                        or not match(p)
                        or p["symbol"] != req["symbol"]
                        or p["side"] != req["side"]
                        or Decimal(p["volume"]) != Decimal(req["volume"])
                        or Decimal(p["stop"]) != Decimal(req["stop"])
                        or Decimal(p["tp"]) != Decimal(req["tp"] or 0)
                        or Decimal(p["price"]) != filled
                    ):
                        raise ValueError("Position protection or economics mismatch")
                    if head["state"] in {"CLOSING", "CRITICAL_PROTECTION_FAULT"}:
                        raise ValueError("Exit or protection incident unresolved")
                    if head["state"] == "POSITION_OPEN":
                        return head
                    from vision.broker.codec import dto

                    policy = dto(SizingPolicy, head["details"]["fresh_sizing_audit"]["policy"])
                    volume, stop = Decimal(req["volume"]), Decimal(req["stop"])
                    loss = self.transport.profit(req["side"], req["symbol"], volume, filled, stop)
                    margin = self.transport.margin(req["side"], req["symbol"], volume, filled)
                    budget = Decimal(
                        head["details"]["fresh_sizing_audit"]["request"]["risk_budget"]
                    )
                    if loss is None or loss >= 0 or margin is None or margin < 0:
                        raise ValueError("Actual fill economics unavailable")
                    if -loss + policy.loss_buffer > budget:
                        self.state = GatewayState.RISK_LOCKED
                        return self._event(
                            head,
                            OrderState.RECONCILIATION_REQUIRED,
                            incident="ACTUAL_FILL_RISK_CAP",
                            facts=wire(facts),
                            actual_fill_risk=str(-loss + policy.loss_buffer),
                            actual_fill_margin=str(margin),
                        )
                    if head["state"] == "BROKER_ACK":
                        head = self._event(head, OrderState.FILLED, fill_price=str(filled))
                    return self._event(
                        head,
                        OrderState.POSITION_OPEN,
                        position_id=p["position_id"],
                        fill_price=str(filled),
                        facts=wire(facts),
                        actual_fill_risk=str(-loss + policy.loss_buffer),
                        actual_fill_margin=str(margin),
                    )
                if sum((Decimal(d["volume"]) for d in outgoing), Decimal(0)) != Decimal(
                    req["volume"]
                ) or any(
                    d["symbol"] != req["symbol"] or d["side"] == req["side"] for d in outgoing
                ):
                    raise ValueError("Closure not proven by broker deals")
                net = sum(
                    (
                        sum(
                            (Decimal(d[k]) for k in ("profit", "commission", "swap", "fee")),
                            Decimal(0),
                        )
                        for d in deals
                    ),
                    Decimal(0),
                )
                if head["state"] == "BROKER_ACK":
                    head = self._event(head, OrderState.FILLED, fill_price=str(filled))
                return self._event(
                    head,
                    OrderState.CLOSED,
                    facts=wire(facts),
                    fill_price=str(filled),
                    realized_pnl=str(net),
                    reliability_update=False,
                )
            except Exception:
                self.state = GatewayState.RECONCILIATION_REQUIRED
                if head["state"] == "RECONCILIATION_REQUIRED":
                    return head
                return self._event(
                    head,
                    OrderState.RECONCILIATION_REQUIRED,
                    incident="BROKER_STATE_OR_HISTORY_MISMATCH",
                )

    def close(self, client_order_id, *, operator_confirmation):
        """Explicit full close only. No emergency repair/close or close retry loop."""
        with self.lock, self.transport.lock, arithmetic():
            now = self._armed()
            if operator_confirmation != "CLOSE_DEMO_POSITION":
                raise ValueError("Explicit demo full-close confirmation required")
            head = self.monitor(client_order_id)
            if head["state"] != "POSITION_OPEN":
                raise ValueError("Reconciled owned open position required")
            snapshot = self.transport.snapshot()
            if (
                not snapshot.account.demo
                or snapshot.account.identity != self.policy.account_identity
            ):
                raise ValueError("Pinned demo account required")
            req = head["details"]["request"]
            quote = next(q for q in snapshot.quotes if q.symbol == req["symbol"])
            if (
                not 0
                <= (now - quote.source_at).total_seconds()
                <= self.policy.receipt_ttl.total_seconds()
                or quote.ask - quote.bid > self.policy.max_spread
            ):
                raise ValueError("Fresh bounded-spread exit quote required")
            raw = {
                **req,
                "side": "SHORT" if req["side"] == "LONG" else "LONG",
                "volume": Decimal(req["volume"]),
                "stop": Decimal(0),
                "tp": None,
                "position_id": head["details"]["position_id"],
            }
            head = self._event(head, OrderState.CLOSING)
            try:
                result = self.transport.send(raw, expected_snapshot=snapshot)
            except Exception:
                self.state = GatewayState.RECONCILIATION_REQUIRED
                return self._event(head, OrderState.UNKNOWN, incident="CLOSE_UNCERTAIN")
            if result.category != "ACK":
                self.state = GatewayState.RECONCILIATION_REQUIRED
                return self._event(head, OrderState.UNKNOWN, close_result=wire(result))
            return self.monitor(client_order_id)
