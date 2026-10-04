"""Deterministic cash Spot simulator with mandatory hard risk vetoes and replay."""

import json
import os
import tempfile
from copy import copy
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from threading import RLock

from vision.core.codec import decimal_string, event_from_dict, utc_string
from vision.core.contracts import QuotePayload, TimestampBasis, _identifier, _utc
from vision.core.instruments import QuantityUnit, digest
from vision.core.state.portfolio import DataQuality, Position, Side, wire
from vision.core.state.replay import record_from_dict, shape
from vision.execution.paper.models import (
    Fill,
    MarketRules,
    OrderResult,
    OrderState,
    PaperCosts,
    PaperMode,
    PaperOrder,
    PaperPosition,
    RiskDecision,
    exact,
    nonnegative,
)
from vision.risk.governor import HardRiskGovernor, RiskLimits


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    as_of: datetime
    currency: str
    cash: Decimal
    equity: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    commissions: Decimal
    open_risk: Decimal | None
    high_water: Decimal
    daily_baseline: Decimal
    daily_loss_latched: bool
    drawdown_latched: bool
    issues: tuple[str, ...]
    positions: tuple[PaperPosition, ...]
    journal_head: str

    def to_dict(self):
        return {field.name: wire(getattr(self, field.name)) for field in fields(self)}


class PaperBroker:
    def __init__(
        self,
        currency,
        starting_cash,
        started_at,
        *,
        costs=None,
        limits=None,
        allow_receipt_quotes=False,
        journal_path=None,
    ):
        _identifier(currency)
        _utc(started_at)
        nonnegative(starting_cash)
        if starting_cash <= 0:
            raise ValueError("Positive initial cash required")
        if type(allow_receipt_quotes) is not bool:
            raise ValueError("Explicit receipt policy required")
        self._currency, self.initial_cash, self.started_at = currency, starting_cash, started_at
        self._costs, self._limits = costs or PaperCosts(), limits or RiskLimits()
        if not isinstance(self.costs, PaperCosts) or not isinstance(self.limits, RiskLimits):
            raise ValueError("Typed simulator policy required")
        self.allow_receipt_quotes = allow_receipt_quotes
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self.lock = RLock()
        self.cash, self.realized, self.commissions = starting_cash, Decimal(0), Decimal(0)
        self.high_water = self.daily_baseline = self.last_equity = starting_cash
        self.daily_loss_latched = self.drawdown_latched = False
        self.day, self.last_at = started_at.date(), started_at
        self.records, self.rules, self.quotes, self.positions, self.orders = {}, {}, {}, {}, {}
        self.consumed = {}
        self.quality = DataQuality(started_at, ())
        self.journal = []
        self._genesis = self._configuration()

    def _configuration(self):
        return {
            "currency": self.currency,
            "starting_cash": str(self.initial_cash),
            "started_at": _utc(self.started_at),
            "costs": wire(self.costs),
            "limits": wire(self.limits),
            "allow_receipt_quotes": self.allow_receipt_quotes,
        }

    @property
    def costs(self):
        return self._costs

    @property
    def limits(self):
        return self._limits

    @property
    def currency(self):
        return self._currency

    def config(self):
        return json.loads(json.dumps(self._genesis))

    def configure(self, *, costs=None, limits=None):
        return self._execute(
            {
                "op": "configure",
                "costs": wire(costs or self.costs),
                "limits": wire(limits or self.limits),
            }
        )

    @property
    def journal_head(self):
        return self.journal[-1]["digest"] if self.journal else digest(self.config())

    def _execute(self, command):
        with self.lock, exact():
            candidate = copy(self)
            for key in ("records", "rules", "quotes", "positions", "orders", "consumed"):
                setattr(candidate, key, dict(getattr(self, key)))
            candidate.journal = list(self.journal)
            result = candidate._apply(command)
            previous = self.journal_head
            entry = {"command": command, "previous": previous}
            candidate.journal.append({**entry, "digest": digest(entry)})
            if len(candidate.journal) > 10000:
                raise ValueError("Paper journal capacity exhausted")
            if self.journal_path is not None:
                candidate.save(self.journal_path)
            for key, value in candidate.__dict__.items():
                if key != "lock":
                    setattr(self, key, value)
            return result

    def register(self, record, rules):
        return self._execute({"op": "register", "record": record.to_dict(), "rules": wire(rules)})

    def update_quote(self, event, quality):
        return self._execute({"op": "quote", "event": event.to_dict(), "quality": wire(quality)})

    def update_quality(self, quality):
        return self._execute({"op": "quality", "quality": wire(quality)})

    def capture_from_hub(self, hub, event=None):
        """Run on the owning loop; only currently admitted hub quotes can fill."""
        from vision.market_data.health.gate import stream_key

        at = hub.clock()
        statuses = hub.gate.snapshot(at)
        states = []
        for instrument in sorted(self.records):
            relevant = [(key, value) for key, value in statuses.items() if f":{instrument}:" in key]
            state = "warming_up"
            if relevant:
                state = "healthy"
                for key, value in relevant:
                    stream = hub.gate.streams[key]
                    if (
                        value == "degraded"
                        and self.allow_receipt_quotes
                        and stream.last_rejection is None
                        and stream.last_event is not None
                        and stream.last_event.timestamp_basis is TimestampBasis.RECEIPT
                    ):
                        continue
                    if value != "healthy":
                        state = value
                        break
            states.append((instrument, state))
        selector = getattr(hub, "failover", None)
        quality = DataQuality(
            at, tuple(states), bool(selector and selector.divergent), "phase2-paper-feed"
        )
        if event is not None:
            stream = hub.gate.streams.get(stream_key(event))
            if (
                stream is None
                or stream.last_event != event
                or stream.last_rejection is not None
                or stream.gap
                or stream.disconnected
            ):
                raise ValueError("Paper fill requires an admitted current hub quote")
            return self.update_quote(event, quality)
        return self.update_quality(quality)

    def submit(self, order):
        if not isinstance(order, PaperOrder):
            raise ValueError("PaperOrder required")
        command = {"op": "order", "order": wire(order)}
        with self.lock:
            prior = self.orders.get(order.request_id)
            if prior is not None:
                if prior.order != order:
                    raise ValueError("Request ID collision")
                return prior
            return self._execute(command)

    def close(self, position_id, request_id, at):
        with self.lock:
            p = self.positions[position_id].position
            return self.submit(
                PaperOrder(
                    request_id,
                    p.instrument_id,
                    p.spec_revision,
                    p.side,
                    p.quantity,
                    None,
                    at,
                    close_position_id=position_id,
                )
            )

    def _quality(self, value):
        data = shape(value, DataQuality)
        data["as_of"] = utc_string(data["as_of"])
        data["states"] = tuple(tuple(pair) for pair in data["states"])
        return DataQuality(**data)

    def _advance(self, at):
        _utc(at)
        if at < self.last_at:
            raise ValueError("Paper command time cannot regress")
        if at.date() != self.day:
            # Carry the last verified equity so an overnight gap cannot reset away loss.
            self.daily_baseline = self.last_equity
            self.day = at.date()
            self.daily_loss_latched = False
        self.last_at = at

    def _apply(self, command):
        op = command.get("op")
        if op == "configure":
            if set(command) != {"op", "costs", "limits"} or self.orders:
                raise ValueError("Paper policy is locked after the first order")
            costs = shape(command["costs"], PaperCosts)
            costs["mode"] = PaperMode(costs["mode"])
            for key in ("spread_bps", "slippage_bps", "commission_bps"):
                costs[key] = decimal_string(costs[key])
            limits = {
                key: decimal_string(value)
                for key, value in shape(command["limits"], RiskLimits).items()
            }
            self._costs, self._limits = PaperCosts(**costs), RiskLimits(**limits)
            return None
        if op == "register":
            if set(command) != {"op", "record", "rules"}:
                raise ValueError("Unknown register fields")
            record = record_from_dict(command["record"])
            data = shape(command["rules"], MarketRules)
            for key in (
                "minimum_quantity",
                "maximum_quantity",
                "quantity_step",
                "minimum_notional",
            ):
                data[key] = decimal_string(data[key])
            if data["maximum_notional"] is not None:
                data["maximum_notional"] = decimal_string(data["maximum_notional"])
            rules = MarketRules(**data)
            if (
                record.quantity_unit is not QuantityUnit.BASE
                or record.spec.venue not in {"BINANCE_SPOT", "BYBIT_SPOT"}
                or record.valuation_model != "linear_quote"
                or record.revision != rules.spec_revision
                or record.spec.instrument_id != rules.instrument_id
                or record.provenance.metadata_digest != rules.evidence_digest
            ):
                raise ValueError("Unverified cash Spot economics or rule provenance")
            prior = self.records.get(record.spec.instrument_id)
            if prior is None and len(self.records) >= 256:
                raise ValueError("Paper instrument capacity exhausted")
            if prior and (
                prior.canonical != record.canonical
                or record.provenance.observed_at < prior.provenance.observed_at
            ):
                raise ValueError("Spec provenance regression")
            self.records[record.spec.instrument_id] = record
            self.rules[record.spec.instrument_id] = rules
            return record
        if op in {"quote", "quality"}:
            if set(command) != ({"op", "quality", "event"} if op == "quote" else {"op", "quality"}):
                raise ValueError("Unknown feed fields")
            quality = self._quality(command["quality"])
            self._advance(quality.as_of)
            if op == "quote":
                event = event_from_dict(command["event"])
                record = self.records.get(event.instrument_id)
                if (
                    record is None
                    or not isinstance(event.payload, QuotePayload)
                    or event.delivery_kind != "live"
                    or event.source != record.provenance.source
                ):
                    raise ValueError("Registered live quote required")
                old = self.quotes.get(event.instrument_id)
                if old and (
                    event.sequence <= old.sequence
                    or event.source_epoch < old.source_epoch
                    or event.source_ts < old.source_ts
                    or event.received_ts < old.received_ts
                ):
                    raise ValueError("Quote replay/regression")
                self.quotes[event.instrument_id] = event
                self.consumed[event.instrument_id] = (Decimal(0), Decimal(0))
            self.quality = quality
            if op == "quote" and self._fresh(event, quality.as_of):
                for position_id, paper in sorted(self.positions.items()):
                    if (
                        paper.close_fill_id is None
                        and paper.position.instrument_id == event.instrument_id
                        and event.payload.bid <= paper.stop_loss
                    ):
                        p = paper.position
                        self._submit(
                            PaperOrder(
                                f"stop:{position_id}:{event.event_id}",
                                p.instrument_id,
                                p.spec_revision,
                                p.side,
                                p.quantity,
                                None,
                                quality.as_of,
                                close_position_id=position_id,
                            ),
                            reason="GAP_THROUGH_STOP"
                            if event.payload.bid < paper.stop_loss
                            else "STOP",
                        )
            self._observe_equity(quality.as_of)
            return self.snapshot(quality.as_of)
        if op == "order":
            if set(command) != {"op", "order"}:
                raise ValueError("Unknown order fields")
            data = shape(command["order"], PaperOrder)
            data["side"] = Side(data["side"])
            data["quantity"] = decimal_string(data["quantity"])
            if data["stop_loss"] is not None:
                data["stop_loss"] = decimal_string(data["stop_loss"])
            data["created_at"] = utc_string(data["created_at"])
            order = PaperOrder(**data)
            self._advance(order.created_at)
            if order.request_id in self.orders:
                raise ValueError("Duplicate journal order")
            return self._submit(order)
        raise ValueError("Unknown paper journal command")

    def _fresh(self, quote, at):
        return all(
            timedelta(0) <= at - ts <= timedelta(seconds=5)
            for ts in (quote.source_ts, quote.received_ts)
        ) and (quote.timestamp_basis is TimestampBasis.EXCHANGE or self.allow_receipt_quotes)

    def _price(self, quote, record, buy):
        if self.costs.mode is PaperMode.IDEAL:
            return (quote.payload.bid + quote.payload.ask) / 2
        price = quote.payload.ask if buy else quote.payload.bid
        adjustment = self.costs.slippage_bps + self.costs.spread_bps / 2
        price *= 1 + (adjustment / 10000 if buy else -adjustment / 10000)
        tick = record.spec.tick_size
        price = (price / tick).to_integral_value(
            rounding=ROUND_CEILING if buy else ROUND_FLOOR
        ) * tick
        if price <= 0:
            raise ValueError("Unsupported simulated price")
        return price

    def _fee(self, notional):
        return (
            Decimal(0)
            if self.costs.mode is PaperMode.IDEAL
            else notional * self.costs.commission_bps / 10000
        )

    def _values(self, at):
        equity = self.cash
        unrealized = Decimal(0)
        risk = Decimal(0)
        exposure = Decimal(0)
        by_symbol = {}
        issues = []
        for paper in self.positions.values():
            if paper.close_fill_id is not None:
                continue
            p = paper.position
            quote = self.quotes.get(p.instrument_id)
            record = self.records.get(p.instrument_id)
            if quote is None or record is None or not self._fresh(quote, at):
                issues.append("STALE_OR_MISSING_MARKS")
                continue
            if record.spec.quote_currency != self.currency:
                issues.append("FX_CONVERSION_REQUIRED")
                continue
            price = self._price(quote, record, False)
            notional = price * p.quantity
            equity += notional - self._fee(notional)
            unrealized += (
                (price - p.entry_price) * p.quantity - paper.entry_fee - self._fee(notional)
            )
            exposure += quote.payload.bid * p.quantity
            by_symbol[record.canonical.key] = (
                by_symbol.get(record.canonical.key, Decimal(0)) + quote.payload.bid * p.quantity
            )
            if paper.stop_loss is None or paper.stop_loss <= 0:
                risk = None
                issues.append("UNKNOWN_OPEN_RISK")
            elif quote.payload.bid <= paper.stop_loss:
                risk = None
                issues.append("STOP_EXIT_PENDING")
            elif risk is not None:
                stop_price = self._stop_price(paper.stop_loss, record)
                risk += max(Decimal(0), price - stop_price) * p.quantity + self._fee(
                    stop_price * p.quantity
                )
        return (
            None
            if any(
                issue in {"STALE_OR_MISSING_MARKS", "FX_CONVERSION_REQUIRED"} for issue in issues
            )
            else equity,
            None
            if any(
                issue in {"STALE_OR_MISSING_MARKS", "FX_CONVERSION_REQUIRED"} for issue in issues
            )
            else unrealized,
            risk if not issues else None,
            exposure,
            by_symbol,
            tuple(sorted(set(issues))),
        )

    def _stop_price(self, stop, record):
        if self.costs.mode is PaperMode.IDEAL:
            return stop
        price = stop * (1 - (self.costs.slippage_bps + self.costs.spread_bps / 2) / 10000)
        return (price / record.spec.tick_size).to_integral_value(
            rounding=ROUND_FLOOR
        ) * record.spec.tick_size

    def _observe_equity(self, at):
        equity, *_ = self._values(at)
        if equity is not None:
            self.last_equity = equity
            self.high_water = max(self.high_water, equity)
            if equity <= self.daily_baseline * (1 - self.limits.daily_equity_loss_fraction):
                self.daily_loss_latched = True
            if equity <= self.high_water * (1 - self.limits.drawdown_fraction):
                self.drawdown_latched = True

    def _submit(self, order, reason="MARKET"):
        record = self.records.get(order.instrument_id)
        rules = self.rules.get(order.instrument_id)
        quote = self.quotes.get(order.instrument_id)
        order_id = digest({"seed": digest(self.config()), "request": order.request_id})
        states = (OrderState.NEW, OrderState.VALIDATING)
        reasons = []
        equity, _, open_risk, gross, by_symbol, _ = self._values(order.created_at)
        closing = order.close_position_id is not None
        paper = self.positions.get(order.close_position_id)
        if order.order_type != "MARKET":
            reasons.append("ORDER_TYPE_UNSUPPORTED")
        if record is None or rules is None:
            reasons.append("INSTRUMENT_SPEC_INCOMPLETE")
        elif record.spec.quote_currency != self.currency:
            reasons.append("FX_CONVERSION_REQUIRED")
        elif not closing and (
            order.spec_revision != record.revision
            or not timedelta(0)
            <= order.created_at - record.provenance.observed_at
            <= timedelta(hours=24)
        ):
            reasons.append("INSTRUMENT_SPEC_INCOMPLETE")
        if order.side is not Side.LONG:
            reasons.append("MARGIN_MODEL_UNVERIFIED")
        if quote is None or not self._fresh(quote, order.created_at):
            reasons.append("STALE_OR_MISSING_QUOTE")
        if closing:
            if (
                paper is None
                or paper.close_fill_id is not None
                or paper.position.instrument_id != order.instrument_id
                or paper.position.quantity != order.quantity
                or paper.position.spec_revision != order.spec_revision
            ):
                reasons.append("CLOSE_POSITION_MISMATCH")
        else:
            if (
                self.quality.as_of > order.created_at
                or order.created_at - self.quality.as_of > timedelta(seconds=5)
                or dict(self.quality.states).get(order.instrument_id) != "healthy"
            ):
                reasons.append("FEED_NOT_READY")
            if self.quality.divergent:
                reasons.append("DATA_DIVERGENT")
            if self.daily_loss_latched:
                reasons.append("DAILY_EQUITY_LOSS_LIMIT")
            if self.drawdown_latched:
                reasons.append("MAX_DRAWDOWN")
            if order.stop_loss is None:
                reasons.append("NO_STOP_LOSS")
        price = fee = trade_risk = projected = None
        if record is not None and rules is not None and quote is not None:
            price = self._price(quote, record, not closing)
            notional = price * order.quantity
            fee = self._fee(notional)
            if (
                order.quantity % rules.quantity_step != 0
                or order.quantity % record.spec.lot_size != 0
                or not rules.minimum_quantity <= order.quantity <= rules.maximum_quantity
            ):
                reasons.append("QUANTITY_RULE")
            if (
                notional < rules.minimum_notional
                or rules.maximum_notional is not None
                and notional > rules.maximum_notional
            ):
                reasons.append("NOTIONAL_RULE")
            if self.costs.mode is PaperMode.REALISTIC:
                consumed = self.consumed.get(order.instrument_id, (Decimal(0), Decimal(0)))
                available = (
                    (quote.payload.bid_quantity - consumed[0])
                    if closing
                    else (quote.payload.ask_quantity - consumed[1])
                )
                if order.quantity > available:
                    reasons.append("INSUFFICIENT_TOP_OF_BOOK")
            if not closing:
                if notional + fee > self.cash:
                    reasons.append("INSUFFICIENT_CASH")
                if order.stop_loss is not None:
                    if (
                        order.stop_loss % record.spec.tick_size != 0
                        or order.stop_loss >= quote.payload.bid
                    ):
                        reasons.append("INVALID_STOP_LOSS")
                    stop_price = self._stop_price(order.stop_loss, record)
                    trade_risk = (
                        max(Decimal(0), price - stop_price) * order.quantity
                        + fee
                        + self._fee(stop_price * order.quantity)
                    )
                exit_price = self._price(quote, record, False)
                exit_notional = exit_price * order.quantity
                projected = (
                    None
                    if equity is None
                    else equity - notional - fee + exit_notional - self._fee(exit_notional)
                )
                exposure = quote.payload.bid * order.quantity
                reasons.extend(
                    HardRiskGovernor(self.limits).assess(
                        equity=equity,
                        trade_risk=trade_risk,
                        open_risk=open_risk,
                        projected_equity=projected,
                        symbol_exposure=by_symbol.get(record.canonical.key, Decimal(0)) + exposure,
                        gross_exposure=gross + exposure,
                        daily_baseline=self.daily_baseline,
                        high_water=self.high_water,
                    )
                )
        if len(self.orders) >= 2048:
            raise ValueError("Paper order capacity exhausted")
        reasons = tuple(sorted(set(reasons)))
        decision_id = digest(
            {
                "order_id": order_id,
                "head": self.journal_head,
                "at": _utc(order.created_at),
                "reasons": reasons,
            }
        )
        decision = RiskDecision(
            decision_id,
            order_id,
            not reasons,
            reasons,
            equity,
            trade_risk,
            None if trade_risk is None or open_risk is None else open_risk + trade_risk,
            order.created_at,
            quote.event_id if quote else None,
        )
        fill = None
        if not reasons:
            position_id = (
                order.close_position_id if closing else digest({"order": order_id, "position": 1})
            )
            fill = Fill(
                digest({"order": order_id, "fill": 1}),
                decision_id,
                order_id,
                position_id,
                price,
                order.quantity,
                fee,
                order.created_at,
                quote.event_id,
                reason,
            )
            consumed = self.consumed[order.instrument_id]
            self.consumed[order.instrument_id] = (
                (consumed[0] + order.quantity, consumed[1])
                if closing
                else (consumed[0], consumed[1] + order.quantity)
            )
            self.commissions += fee
            if closing:
                self.cash += price * order.quantity - fee
                self.realized += (
                    (price - paper.position.entry_price) * order.quantity - paper.entry_fee - fee
                )
                self.positions[position_id] = replace(paper, close_fill_id=fill.fill_id)
            else:
                self.cash -= price * order.quantity + fee
                self.positions[position_id] = PaperPosition(
                    Position(
                        position_id,
                        order.instrument_id,
                        order.spec_revision,
                        Side.LONG,
                        order.quantity,
                        QuantityUnit.BASE,
                        price,
                        order.created_at,
                        fill.fill_id,
                    ),
                    order.stop_loss,
                    fill.fill_id,
                    fee,
                )
            states += (OrderState.RISK_APPROVED, OrderState.FILLED)
        else:
            states += (OrderState.REJECTED,)
        result = OrderResult(order, order_id, states, decision, fill)
        self.orders[order.request_id] = result
        self._observe_equity(order.created_at)
        return result

    def snapshot(self, as_of):
        with self.lock, exact():
            _utc(as_of)
            if as_of < self.last_at:
                raise ValueError("Cannot retrospectively value current paper state")
            equity, unrealized, risk, _, _, issues = self._values(as_of)
            return AccountSnapshot(
                as_of,
                self.currency,
                self.cash,
                equity,
                self.realized,
                unrealized,
                self.commissions,
                risk,
                self.high_water,
                self.daily_baseline,
                self.daily_loss_latched,
                self.drawdown_latched,
                issues,
                tuple(self.positions[key] for key in sorted(self.positions)),
                self.journal_head,
            )

    def save(self, path):
        with self.lock:
            payload = {
                "version": 1,
                "config": self.config(),
                "journal": self.journal,
                "head": self.journal_head,
            }
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if len(encoded) > 5000000:
                raise ValueError("Paper checkpoint size exceeded")
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                    temp = Path(handle.name)
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if temp is not None:
                    temp.unlink(missing_ok=True)

    @classmethod
    def load(cls, path):
        try:
            return cls._load(path)
        except (TypeError, KeyError, IndexError, OverflowError) as error:
            raise ValueError("Invalid paper checkpoint structure") from error

    @classmethod
    def _load(cls, path):
        with Path(path).open("rb") as handle:
            raw = handle.read(5000001)
        if len(raw) > 5000000:
            raise ValueError("Paper checkpoint size exceeded")
        broker = cls.from_checkpoint(json.loads(raw))
        broker.journal_path = Path(path)
        return broker

    def checkpoint(self):
        """Defensive offline checkpoint for durable research capture, under the broker lock."""
        with self.lock:
            value = {
                "version": 1,
                "config": self.config(),
                "journal": self.journal,
                "head": self.journal_head,
            }
            encoded = json.dumps(value)
            if len(encoded.encode()) > 5000000:
                raise ValueError("Paper checkpoint size exceeded")
            return json.loads(encoded)

    @classmethod
    def from_checkpoint(cls, value):
        """Replay without file I/O; this never changes the supplied broker or checkpoint."""
        try:
            return cls._from_checkpoint(value)
        except (TypeError, KeyError, IndexError, OverflowError) as error:
            raise ValueError("Invalid paper checkpoint structure") from error

    @classmethod
    def _from_checkpoint(cls, value):
        if len(json.dumps(value).encode()) > 5000000:
            raise ValueError("Paper checkpoint size exceeded")
        if (
            set(value) != {"version", "config", "journal", "head"}
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("Unknown checkpoint version/shape")
        config = value["config"]
        if set(config) != {
            "currency",
            "starting_cash",
            "started_at",
            "costs",
            "limits",
            "allow_receipt_quotes",
        }:
            raise ValueError("Unknown paper config")
        costs = shape(config["costs"], PaperCosts)
        costs["mode"] = PaperMode(costs["mode"])
        for key in ("spread_bps", "slippage_bps", "commission_bps"):
            costs[key] = decimal_string(costs[key])
        limits = shape(config["limits"], RiskLimits)
        limits = {key: decimal_string(amount) for key, amount in limits.items()}
        broker = cls(
            config["currency"],
            decimal_string(config["starting_cash"]),
            utc_string(config["started_at"]),
            costs=PaperCosts(**costs),
            limits=RiskLimits(**limits),
            allow_receipt_quotes=config["allow_receipt_quotes"],
        )
        if not isinstance(value["journal"], list) or len(value["journal"]) > 10000:
            raise ValueError("Invalid journal length")
        for entry in value["journal"]:
            if (
                set(entry) != {"command", "previous", "digest"}
                or entry["previous"] != broker.journal_head
                or entry["digest"]
                != digest({"command": entry["command"], "previous": entry["previous"]})
            ):
                raise ValueError("Paper journal chain mismatch")
            broker._execute(entry["command"])
        if broker.journal_head != value["head"]:
            raise ValueError("Paper checkpoint head mismatch")
        return broker
