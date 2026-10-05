"""Native DEMO chart/history inspection only; never part of execution approval."""

from datetime import UTC, datetime, timedelta
from threading import RLock
from time import monotonic

from vision.apps.dashboard.indicators import calculate
from vision.broker.mt5_bridge import CANONICAL, native_decimal
from vision.core.contracts import BarPayload

SYMBOLS = {
    "XAUUSD": "metals:XAU/USD",
    "XAGUSD": "metals:XAG/USD",
    "EURUSD": "forex:EUR/USD",
    "BTCUSD": "crypto:BTC/USD",
}
INTERVAL_NAMES = {
    60: "TIMEFRAME_M1",
    300: "TIMEFRAME_M5",
    900: "TIMEFRAME_M15",
    3600: "TIMEFRAME_H1",
    14400: "TIMEFRAME_H4",
    86400: "TIMEFRAME_D1",
}


class DemoMarketReader:
    def __init__(self, reader):
        self.reader, self.cache, self.lock = reader, {}, RLock()
        snapshot = reader.snapshot()
        self.identity = snapshot.account.identity
        self.specs = {s.symbol: s for s in snapshot.specs}
        if set(self.specs) != set(SYMBOLS):
            raise ValueError("Exact four native demo symbols required")
        self.instruments = [
            {
                "instrument_id": f"MT5:DEMO:{name}",
                "symbol": name,
                "venue": "MT5_DEMO",
                "tick_size": str(spec.tick_size),
                "base_currency": CANONICAL[spec.canonical][0],
                "quote_currency": spec.profit_currency,
            }
            for name, spec in self.specs.items()
        ]

    def market(self, instrument, seconds, *, before=None):
        name = instrument.removeprefix("MT5:DEMO:")
        if name not in SYMBOLS or seconds not in INTERVAL_NAMES:
            raise ValueError("Unsupported exact demo chart selection")
        with self.lock, self.reader.lock:
            account = self.reader.account()  # Always checks DEMO, connected and pinned identity.
            if account.identity != self.identity:
                raise ValueError("Demo research account changed")
            key = (name, seconds, before)
            now = self.reader.clock()
            previous = self.cache.get(key)
            if previous and monotonic() - previous[0] < 5:
                elapsed = monotonic() - previous[0]
                return {
                    **previous[1],
                    "receipt_age_seconds": elapsed,
                    "quote_age_seconds": previous[1]["quote_age_seconds"] + elapsed,
                }
            native = self.reader._native
            info, tick = native.symbol_info(name), native.symbol_info_tick(name)
            if (
                info is None
                or tick is None
                or (info.currency_base, info.currency_profit) != CANONICAL[SYMBOLS[name]]
            ):
                raise ValueError("Exact native symbol identity unavailable")
            if getattr(info, "chart_mode", None) != 0:
                raise ValueError("Native BID chart basis must be verified")
            frame = getattr(native, INTERVAL_NAMES[seconds])
            if before is None:
                rows = native.copy_rates_from_pos(name, frame, 1, 512)  # Skip forming bar.
            else:
                rows = native.copy_rates_from(
                    name, frame, datetime.fromtimestamp(before - 1, UTC), 512
                )
            if rows is None or not 0 < len(rows) <= 512:
                raise ValueError("Native chart history unavailable")
            payloads, candles = [], []
            seen = set()
            for row in rows:
                opening = int(row["time"])
                if opening in seen or seen and opening <= max(seen):
                    raise ValueError("Native chart times must be unique and increasing")
                seen.add(opening)
                values = {
                    key: native_decimal(float(row[key])) for key in ("open", "high", "low", "close")
                }
                volume = native_decimal(int(row["tick_volume"]))
                payloads.append(
                    BarPayload(
                        *values.values(),
                        volume,
                        seconds,
                        datetime.fromtimestamp(opening, UTC),
                        price_basis="bid",
                        volume_basis="price_count",
                    )
                )
                candles.append(
                    {
                        "time": opening,
                        **{k: str(v) for k, v in values.items()},
                        "volume": str(volume),
                        "event_id": f"mt5-demo:{name}:{seconds}:{opening}",
                        "delivery_kind": "broker_history_unverified_clock",
                    }
                )
            if self.reader.account().identity != self.identity:
                raise ValueError("Demo account changed during history read")
            source = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(tick.time_msc))
            age = (now - source).total_seconds()
            spec = next(s for s in self.instruments if s["symbol"] == name)
            result = {
                "instrument_id": instrument,
                "spec": spec,
                "source": "mt5.demo",
                "source_epoch": 0,
                "health": "BLOCKED" if not 0 <= age <= 5 else "DEMO_MONITOR",
                "new_signals_permitted": False,
                "candle_health": "BROKER_HISTORY_ONLY",
                "quote_timestamp_basis": "unverified_broker_clock",
                "clock_warning": (
                    "Native broker timestamps preserved; "
                    "chart history never authorizes sizing or orders."
                ),
                "quote_age_seconds": age,
                "receipt_age_seconds": 0,
                "candle_age_seconds": (
                    now - datetime.fromtimestamp(candles[-1]["time"] + seconds, UTC)
                ).total_seconds(),
                "candles": candles,
                "indicators": {
                    k: [
                        {"time": candles[i]["time"], "value": str(v)}
                        for i, v in enumerate(values)
                        if v is not None
                    ]
                    for k, values in calculate(payloads).items()
                    if not k.startswith("_")
                },
                "quote": {
                    "bid": str(native_decimal(tick.bid)),
                    "ask": str(native_decimal(tick.ask)),
                    "source_at": source.isoformat(),
                    "observed_at": now.isoformat(),
                },
                "flow": {
                    "candles": [],
                    "trades": [],
                    "health": "UNAVAILABLE",
                    "reason": "MT5_QUOTES_HAVE_NO_AGGRESSOR_TRADES",
                    "ofi": None,
                },
                "depth": {
                    "state": "UNAVAILABLE",
                    "reason": "NO_VERIFIED_NATIVE_DEPTH",
                    "source": "mt5.demo",
                    "bids": [],
                    "asks": [],
                    "metrics": None,
                },
                "derivatives": {"funding": None, "open_interest": None},
            }
            self.cache[key] = monotonic(), result
            while len(self.cache) > 48:
                del self.cache[next(iter(self.cache))]
            return result

    def deals_report(self):
        """Bounded native history query; uncertain clock attribution stays explicit."""
        with self.lock, self.reader.lock:
            account = self.reader.account()
            if account.identity != self.identity:
                raise ValueError("Demo research account changed")
            now = self.reader.clock()
            begin = now - timedelta(days=2)
            rows = self.reader._native.history_deals_get(begin, now)
            if rows is None or len(rows) > 5000:
                raise ValueError("Bounded native deal history unavailable")
            values = []
            for row in rows:
                values.append(
                    {
                        "id": self.reader._id(row.ticket),
                        "position_id": self.reader._id(row.position_id),
                        "symbol": row.symbol,
                        "type": row.type,
                        "entry": row.entry,
                        "native_time_msc": row.time_msc,
                        **{
                            k: str(native_decimal(getattr(row, k)))
                            for k in ("volume", "price", "profit", "commission", "swap", "fee")
                        },
                    }
                )
            if self.reader.account().identity != self.identity:
                raise ValueError("Demo account changed during history query")
            return {
                "state": "UNVERIFIED_PERIOD_ATTRIBUTION",
                "query_from_utc": begin.isoformat(),
                "query_to_utc": now.isoformat(),
                "account_currency": account.currency,
                "reported_deals": values,
                "reason": (
                    "Native history preserved; broker-clock reconciliation "
                    "required for daily PnL attribution"
                ),
            }
