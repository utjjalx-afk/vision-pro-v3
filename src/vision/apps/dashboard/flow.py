"""Provider-local exact footprint aggregation and sequence-checked depth."""

from collections import OrderedDict, deque
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal

from vision.analysis.contracts import arithmetic, number
from vision.core.contracts import TradePayload

D = Decimal


def max_ratio(levels, key):
    values = [D(v[key]) for v in levels if v[key] is not None]
    return str(max(values)) if values else None


class Footprint:
    def __init__(self, tick, interval=300):
        if not isinstance(tick, D) or not tick.is_finite() or tick <= 0:
            raise ValueError("Explicit positive bucket increment required")
        if type(interval) is not int or interval <= 0:
            raise ValueError("Positive interval required")
        self.tick, self.interval = tick, interval
        self.candles, self.seen = OrderedDict(), OrderedDict()
        self.epoch, self.cvd, self.last_time = None, D(0), None
        self.unknown = 0
        self.last_sequence = None
        self.trades = deque(maxlen=200)

    def admit(self, event):
        with arithmetic():
            return self._admit(event)

    def _admit(self, event):
        if not isinstance(event.payload, TradePayload) or event.delivery_kind != "live":
            return
        identity = (event.source, event.instrument_id, event.source_epoch)
        if identity != self.epoch:
            self.candles.clear()
            self.seen.clear()
            self.trades.clear()
            self.epoch, self.cvd, self.last_time, self.unknown = identity, D(0), None, 0
            self.last_sequence = None
        if event.event_id in self.seen:
            return
        if self.last_sequence is not None and event.sequence <= self.last_sequence:
            raise ValueError("Regressed trade sequence")
        if self.last_time is not None and event.source_ts < self.last_time:
            raise ValueError("Footprint requires ordered admitted events")
        t = int(event.source_ts.timestamp()) // self.interval * self.interval
        price = (event.payload.price / self.tick).to_integral_value(
            rounding=ROUND_FLOOR
        ) * self.tick
        row = self.candles.setdefault(t, {"levels": {}, "cvd": self.cvd})
        if price not in row["levels"] and len(row["levels"]) >= 512:
            raise ValueError("Footprint level capacity exceeded")
        level = row["levels"].setdefault(price, [D(0), D(0)])
        maker, qty = event.payload.buyer_is_maker, event.payload.quantity
        if maker is None:
            self.unknown += 1
        else:
            level[0 if maker else 1] += qty
            self.cvd += -qty if maker else qty
        row["cvd"] = self.cvd
        self.last_time = event.source_ts
        self.last_sequence = event.sequence
        self.seen[event.event_id] = None
        if len(self.seen) > 10000:
            self.seen.popitem(last=False)
        while len(self.candles) > 64:
            self.candles.popitem(last=False)
        self.trades.append(
            {
                "id": event.event_id,
                "time": event.source_ts.isoformat(),
                "price": str(event.payload.price),
                "quantity": str(qty),
                "aggressor": None if maker is None else "SELL" if maker else "BUY",
            }
        )

    def snapshot(self):
        with arithmetic():
            return self._snapshot()

    def _snapshot(self):
        candles = []
        for t, row in self.candles.items():
            levels = [
                {
                    "price": str(p),
                    "bid": str(v[0]),
                    "ask": str(v[1]),
                    "delta": str(v[1] - v[0]),
                    "buy_ratio": str(v[1] / v[0]) if v[0] else None,
                    "sell_ratio": str(v[0] / v[1]) if v[1] else None,
                }
                for p, v in sorted(row["levels"].items())
            ]
            sell = sum((v[0] for v in row["levels"].values()), D(0))
            buy = sum((v[1] for v in row["levels"].values()), D(0))
            total = buy + sell
            poc = max(sorted(row["levels"]), key=lambda p: sum(row["levels"][p]))
            candles.append(
                {
                    "time": t,
                    "levels": levels,
                    "buy": str(buy),
                    "sell": str(sell),
                    "delta": str(buy - sell),
                    "delta_pct": str(100 * (buy - sell) / total) if total else None,
                    "poc": str(poc),
                    "cvd": str(row["cvd"]),
                    "max_buy_imbalance": max_ratio(levels, "buy_ratio"),
                    "max_sell_imbalance": max_ratio(levels, "sell_ratio"),
                }
            )
        return {
            "source": self.epoch[0] if self.epoch else None,
            "source_epoch": self.epoch[2] if self.epoch else None,
            "unit": "base_quantity",
            "bucket_increment": str(self.tick),
            "interval_seconds": self.interval,
            "cvd_basis": "since_source_epoch_start",
            "imbalance_basis": "horizontal ask/bid; zero denominators unavailable",
            "unclassified_trades": self.unknown,
            "candles": candles,
            "trades": list(self.trades),
        }


class DepthBook:
    """Snapshot + Binance U/u continuity; gaps erase displayed depth authority."""

    def __init__(self, source, instrument):
        self.source, self.instrument = source, instrument
        self.bids, self.asks = {}, {}
        self.last_id, self.updated_at = None, None
        self.state, self.reason = "UNAVAILABLE", "NO_DEPTH_FEED"

    def invalidate(self, reason):
        self.bids.clear()
        self.asks.clear()
        self.last_id, self.updated_at = None, None
        self.state, self.reason = "BLOCKED", reason

    @staticmethod
    def parse(rows):
        if type(rows) is not list or len(rows) > 5000:
            raise ValueError("Bounded depth rows required")
        out = {}
        for pair in rows:
            if type(pair) is not list or len(pair) != 2:
                raise ValueError("Depth pair required")
            if any(not isinstance(v, str) or len(v) > 64 for v in pair):
                raise ValueError("Bounded decimal depth fields required")
            p, q = map(D, pair)
            number(p)
            number(q)
            if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0 or p in out:
                raise ValueError("Invalid depth level")
            out[p] = q
        return out

    def snapshot_load(self, value):
        if type(value["lastUpdateId"]) is not int or value["lastUpdateId"] < 0:
            raise ValueError("Snapshot update ID required")
        bids, asks = self.parse(value["bids"]), self.parse(value["asks"])
        self.bids = {p: q for p, q in bids.items() if q}
        self.asks = {p: q for p, q in asks.items() if q}
        self.last_id = value["lastUpdateId"]
        self.state, self.reason = "WARMING_UP", "AWAITING_CONTIGUOUS_DELTA"

    def delta(self, value, at):
        first, last = value["U"], value["u"]
        if type(first) is not int or type(last) is not int or first < 0 or last < first:
            self.invalidate("INVALID_SEQUENCE")
            return False
        if self.last_id is None:
            return False
        if last <= self.last_id:
            return False
        if not first <= self.last_id + 1 <= last:
            self.invalidate("DEPTH_SEQUENCE_GAP")
            return False
        for book, key in ((self.bids, "b"), (self.asks, "a")):
            for price, qty in self.parse(value[key]).items():
                if qty:
                    book[price] = qty
                else:
                    book.pop(price, None)
        if (
            not self.bids
            or not self.asks
            or max(self.bids) >= min(self.asks)
            or len(self.bids) + len(self.asks) > 12000
        ):
            self.invalidate("INVALID_OR_CAPACITY_BOOK")
            return False
        self.last_id, self.updated_at = last, at
        self.state, self.reason = "HEALTHY", None
        return True

    def snapshot(self, now=None):
        now = now or datetime.now(UTC)
        state = self.state
        if self.updated_at is not None and (now - self.updated_at).total_seconds() > 5:
            state = "STALE"
        bids = sorted(self.bids.items(), reverse=True)[:10]
        asks = sorted(self.asks.items())[:10]
        result = {
            "source": self.source,
            "instrument_id": self.instrument,
            "state": state,
            "reason": self.reason,
            "last_update_id": self.last_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "bids": [[str(p), str(q)] for p, q in bids],
            "asks": [[str(p), str(q)] for p, q in asks],
            "metrics": None,
        }
        if state == "HEALTHY" and bids and asks:
            bp, bq = bids[0]
            ap, aq = asks[0]
            metrics = {
                "spread": str(ap - bp),
                "mid": str((ap + bp) / 2),
                "microprice": str((ap * bq + bp * aq) / (bq + aq)),
            }
            for n in (5, 10):
                b = sum((q for _, q in bids[:n]), D(0))
                a = sum((q for _, q in asks[:n]), D(0))
                metrics[f"top{n}_bid"] = str(b)
                metrics[f"top{n}_ask"] = str(a)
                metrics[f"top{n}_imbalance"] = str((b - a) / (b + a)) if b + a else None
            result["metrics"] = metrics
        return result
