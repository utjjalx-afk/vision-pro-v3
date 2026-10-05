"""Narrow MT5 DEMO transport. No import-time connection or public raw order endpoint."""

from vision.analysis.contracts import wire
from vision.broker.models import nonnegative, positive
from vision.broker.mt5_bridge import BridgeUnavailable, native_decimal
from vision.core.instruments import digest
from vision.execution.demo.models import BrokerFacts, BrokerResult


class DispatchBlocked(RuntimeError):
    """A check failed before invoking native order_send; definitely no submission."""


def stable(snapshot):
    data = wire(snapshot)
    data.pop("as_of")
    data["account"].pop("observed_at")
    for row in (*data["specs"], *data["quotes"]):
        row.pop("observed_at")
    return digest(data)


def comment(client_order_id):
    return "v3d:" + digest({"client_order_id": client_order_id})[:24]


class DemoMT5Transport:
    """Only isolated hedging accounts and Market Execution/FOK are initially supported.

    Netting, RETURN/IOC-only execution and partial-close strategies fail closed.
    Account, symbol support and permissions are checked under the reader's SDK lock.
    """

    def __init__(self, reader, *, account_identity, magic):
        self.reader, self.account_identity, self.magic = reader, account_identity, magic

    @property
    def lock(self):
        return self.reader.lock

    def snapshot(self):
        return self.reader.snapshot()

    def audit_size(self, request, policy):
        return self.reader.audit_size(request, policy)

    def profit(self, *args):
        return self.reader.profit(*args)

    def margin(self, *args):
        return self.reader.margin(*args)

    def readiness(self, symbol):
        self.reader.account()
        n = self.reader._native
        a, t, s = n.account_info(), n.terminal_info(), n.symbol_info(symbol)
        if a is None or t is None or s is None:
            raise DispatchBlocked("BROKER_UNAVAILABLE")
        if a.trade_mode != 0 or self.reader._id((a.login, a.server)) != self.account_identity:
            raise DispatchBlocked("DEMO_ACCOUNT_ALLOWLIST_REQUIRED")
        if not t.connected or not a.trade_allowed or not a.trade_expert or not t.trade_allowed:
            raise DispatchBlocked("BROKER_TRADE_PERMISSION_REQUIRED")
        if a.margin_mode != 2:
            raise DispatchBlocked("HEDGING_ACCOUNT_REQUIRED")
        if s.trade_exemode != 2 or not s.filling_mode & 1:
            raise DispatchBlocked("MARKET_EXECUTION_FOK_REQUIRED")
        return s

    def send(self, request, *, expected_snapshot):
        with self.lock:
            fields = {"symbol", "side", "volume", "stop", "tp", "deviation", "comment"}
            if (
                type(request) is not dict
                or set(request) not in (fields, fields | {"position_id"})
                or request["side"] not in {"LONG", "SHORT"}
                or type(request["comment"]) is not str
                or not request["comment"].startswith("v3d:")
                or len(request["comment"]) != 28
                or type(request["deviation"]) is not int
                or not 0 <= request["deviation"] <= 100
            ):
                raise DispatchBlocked("INVALID_NARROW_DEMO_REQUEST")
            positive(request["volume"])
            nonnegative(request["stop"])
            if not request.get("position_id") and request["stop"] <= 0:
                raise DispatchBlocked("MANDATORY_ENTRY_SL")
            if request["tp"] is not None:
                positive(request["tp"])
            s = self.readiness(request["symbol"])
            if (
                request["symbol"] not in self.reader.mapping.values()
                or request["volume"] % native_decimal(s.volume_step)
                or not native_decimal(s.volume_min)
                <= request["volume"]
                <= native_decimal(s.volume_max)
            ):
                raise DispatchBlocked("SYMBOL_OR_VOLUME_GRID_INVALID")
            if stable(self.snapshot()) != stable(expected_snapshot):
                raise DispatchBlocked("FINAL_BROKER_STATE_CHANGED")
            n = self.reader._native
            native = {
                "action": 1,
                "symbol": request["symbol"],
                "type": 0 if request["side"] == "LONG" else 1,
                "volume": float(request["volume"]),
                "sl": float(request["stop"]),
                "tp": float(request["tp"] or 0),
                "deviation": request["deviation"],
                "magic": self.magic,
                "comment": request["comment"],
                "type_time": 0,
                "type_filling": 0,
            }
            if request.get("position_id"):
                positions = n.positions_get()
                if positions is None:
                    raise DispatchBlocked("POSITIONS_UNAVAILABLE")
                owned = [
                    p
                    for p in positions
                    if self.reader._id(p.identifier) == request["position_id"]
                    and p.magic == self.magic
                    and p.comment == request["comment"]
                ]
                if len(owned) != 1 or native_decimal(owned[0].volume) != request["volume"]:
                    raise DispatchBlocked("POSITION_OWNERSHIP_OR_VOLUME_CHANGED")
                if owned[0].symbol != request["symbol"] or request["side"] != (
                    "SHORT" if owned[0].type == 0 else "LONG"
                ):
                    raise DispatchBlocked("FULL_CLOSE_DIRECTION_REQUIRED")
                native["position"] = owned[0].ticket
            # Final account gate immediately before the only submission call.
            self.readiness(request["symbol"])
            result = n.order_send(native)
            if result is None:
                return BrokerResult("UNKNOWN", None, None, None, None, None)
            code = result.retcode
            # Explicit server rejections only; timeout/connection/unrecognized remain uncertain.
            rejected = {
                10004,
                10006,
                10013,
                10014,
                10015,
                10016,
                10017,
                10018,
                10019,
                10020,
                10021,
                10022,
                10024,
                10026,
                10027,
                10030,
                10033,
                10034,
                10035,
                10038,
                10042,
                10043,
                10044,
                10045,
                10046,
            }
            category = (
                "ACK"
                if code in {10008, 10009}
                else "PARTIAL"
                if code == 10010
                else "REJECTED"
                if code in rejected
                else "UNKNOWN"
            )
            return BrokerResult(
                category,
                self.reader._id(result.request_id) if result.request_id else None,
                self.reader._id(result.order) if result.order else None,
                self.reader._id(result.deal) if result.deal else None,
                native_decimal(result.volume),
                native_decimal(result.price),
            )

    def facts(self, *, since):
        with self.lock:
            before = self.reader.account()
            if before.identity != self.account_identity:
                raise BridgeUnavailable("ACCOUNT_IDENTITY_CHANGED")
            n = self.reader._native
            positions, pending = n.positions_get(), n.orders_get()
            orders = n.history_orders_get(since, self.reader.clock())
            deals = n.history_deals_get(since, self.reader.clock())
            if any(x is None or len(x) > 256 for x in (positions, pending, orders, deals)):
                raise BridgeUnavailable("BROKER_HISTORY_UNAVAILABLE")

            def order(row, active):
                return {
                    "order_id": self.reader._id(row.ticket),
                    "symbol": row.symbol,
                    "magic": row.magic,
                    "comment": row.comment,
                    "active": active,
                    "side": "LONG"
                    if row.type == 0
                    else "SHORT"
                    if row.type == 1
                    else "UNSUPPORTED",
                    "state": row.state,
                }

            order_rows = tuple(order(r, False) for r in orders) + tuple(
                order(r, True) for r in pending
            )
            deal_rows = tuple(
                {
                    "deal_id": self.reader._id(r.ticket),
                    "order_id": self.reader._id(r.order),
                    "position_id": self.reader._id(r.position_id),
                    "symbol": r.symbol,
                    "magic": r.magic,
                    "comment": r.comment,
                    "side": "LONG" if r.type == 0 else "SHORT" if r.type == 1 else "UNSUPPORTED",
                    "entry": r.entry,
                    "volume": str(native_decimal(r.volume)),
                    "price": str(native_decimal(r.price)),
                    "profit": str(native_decimal(r.profit)),
                    "commission": str(native_decimal(r.commission)),
                    "swap": str(native_decimal(r.swap)),
                    "fee": str(native_decimal(r.fee)),
                }
                for r in deals
            )
            position_rows = tuple(
                {
                    "position_id": self.reader._id(r.identifier),
                    "symbol": r.symbol,
                    "magic": r.magic,
                    "comment": r.comment,
                    "side": "LONG" if r.type == 0 else "SHORT",
                    "volume": str(native_decimal(r.volume)),
                    "price": str(native_decimal(r.price_open)),
                    "stop": str(native_decimal(r.sl)),
                    "tp": str(native_decimal(r.tp)),
                }
                for r in positions
            )
            after = self.reader.account()
            if after.identity != before.identity or after.currency != before.currency:
                raise BridgeUnavailable("ACCOUNT_CHANGED_DURING_RECONCILIATION")
            return BrokerFacts(
                after.identity,
                after.currency,
                order_rows,
                deal_rows,
                position_rows,
                self.reader.clock(),
            )
