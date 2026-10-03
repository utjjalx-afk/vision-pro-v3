"""Bybit V5 Spot public REST and snapshot streams; never authenticated."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from decimal import Decimal

from websockets.exceptions import ConnectionClosed, InvalidHandshake

from vision.core.codec import decimal_string
from vision.core.contracts import (
    AssetClass,
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    InstrumentSpec,
    QuotePayload,
    TradePayload,
    _utc,
)
from vision.market_data.adapters.binance import (
    INTERVALS,
    BinanceREST,
    BinanceWebSocket,
    MalformedMarketData,
    MarketDataError,
    Subscription,
    _limit,
    epoch_ms,
    integer,
    symbol_name,
    timestamp,
)

SOURCE = "bybit.spot"
WS_BASE = "wss://stream.bybit.com/v5/public/spot"
INTERVAL_CODES = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
}


def topics(subscription: Subscription) -> tuple[str, ...]:
    if subscription.interval not in INTERVAL_CODES:
        raise ValueError("Interval is unsupported by Bybit Spot")
    names = {
        "trade": "publicTrade",
        "quote": "orderbook.1",
        "bar": f"kline.{INTERVAL_CODES[subscription.interval]}",
    }
    return tuple(f"{names[kind]}.{subscription.symbol}" for kind in subscription.kinds)


def string_integer(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("Expected bounded timestamp string")
    return int(value)


class BybitREST(BinanceREST):
    BASE = "https://api.bybit.com/v5/market"
    PATHS = {"instruments-info", "kline", "recent-trade", "time"}

    def get(self, endpoint: str, params: dict | None = None) -> dict:
        parameters = {"category": "spot", **(params or {})}
        if parameters["category"] != "spot":
            raise ValueError("Only public Spot data is supported")
        result = super().get(endpoint, parameters)
        if not isinstance(result, dict) or type(result.get("retCode")) is not int:
            raise MalformedMarketData("Invalid Bybit REST envelope")
        if result["retCode"] != 0:
            raise MarketDataError("Bybit rejected the public-data request")
        data = result.get("result")
        if not isinstance(data, dict) or data.get("category", "spot") != "spot":
            raise MalformedMarketData("Invalid Bybit Spot response")
        return data

    def instrument(self, symbol: str) -> InstrumentSpec:
        symbol_name(symbol)
        return self.parse_instrument(symbol, self.get("instruments-info", {"symbol": symbol}))

    def parse_instrument(self, symbol: str, data) -> InstrumentSpec:
        symbol_name(symbol)
        try:
            rows = data["list"]
            if len(rows) != 1 or rows[0]["symbol"] != symbol or rows[0]["status"] != "Trading":
                raise ValueError("Unavailable instrument")
            row = rows[0]
            return InstrumentSpec(
                f"BYBIT:SPOT:{symbol}",
                "BYBIT_SPOT",
                symbol,
                AssetClass.CRYPTO,
                row["baseCoin"],
                row["quoteCoin"],
                decimal_string(row["priceFilter"]["tickSize"]),
                decimal_string(row["lotSizeFilter"]["basePrecision"]),
                # Spot's deprecated minOrderQty is not a current order constraint.
                Decimal("0"),
                Decimal("1"),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            raise MalformedMarketData("Unsupported Bybit instrument metadata") from None

    def bars(
        self,
        subscription: Subscription,
        *,
        limit: int = 100,
        start: int | None = None,
        end: int | None = None,
    ) -> list:
        topics(subscription)
        data = self.get(
            "kline",
            {
                "symbol": subscription.symbol,
                "interval": INTERVAL_CODES[subscription.interval],
                "limit": _limit(limit),
                **({"start": integer(start)} if start is not None else {}),
                **({"end": integer(end)} if end is not None else {}),
            },
        )
        if data.get("symbol") != subscription.symbol or not isinstance(data.get("list"), list):
            raise MalformedMarketData("Invalid Bybit candle response")
        return data["list"]


class BybitNormalizer:
    def __init__(self, subscription: Subscription):
        self.subscription = subscription
        self.topics = topics(subscription)
        self.trade_cursor = 0  # Local delivery order, explicitly NOT an exchange sequence.

    def event(self, kind, identity, sequence, source_ms, received, payload, continuity):
        return CanonicalMarketEvent(
            f"{SOURCE}:{self.subscription.symbol}:{kind.value}:{identity}",
            SOURCE,
            f"BYBIT:SPOT:{self.subscription.symbol}",
            kind,
            timestamp(source_ms),
            received,
            sequence,
            payload,
            provider_sequence=str(identity),
            continuity=continuity,
        )

    def rest_bar(self, row: list, received) -> CanonicalMarketEvent | None:
        if not isinstance(row, list) or len(row) != 7:
            raise MalformedMarketData("Invalid Bybit REST candle")
        opening = string_integer(row[0])
        closing = opening + INTERVALS[self.subscription.interval] * 1000 - 1
        if closing >= epoch_ms(received):
            return None
        return self.event(
            EventType.BAR,
            f"{self.subscription.interval}:{opening}",
            opening,
            closing,
            received,
            BarPayload(
                *(decimal_string(row[i]) for i in (1, 2, 3, 4, 5)),
                INTERVALS[self.subscription.interval],
                timestamp(opening),
            ),
            "contiguous",
        )

    def message(self, raw, received) -> list[CanonicalMarketEvent]:
        try:
            _utc(received)
            if isinstance(raw, (str, bytes)) and len(raw) > 1_000_000:
                raise ValueError("Oversize message")
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            if "op" in data:
                if data["op"] not in {"subscribe", "ping", "pong"}:
                    raise ValueError("Unknown control operation")
                if data.get("success") is False:
                    raise MarketDataError("Bybit public subscription rejected")
                if data["op"] == "subscribe" and data.get("success") is not True:
                    raise MarketDataError("Bybit public subscription was not confirmed")
                return []
            topic = data["topic"]
            if topic not in self.topics or data["type"] != "snapshot":
                raise ValueError("Unexpected topic or delta")
            publication = integer(data["ts"])
            if topic.startswith("publicTrade."):
                rows = data["data"]
                if not isinstance(rows, list) or not 1 <= len(rows) <= 1024:
                    raise ValueError("Invalid trade batch")
                result = []
                for row in rows:
                    if row["s"] != self.subscription.symbol or row["S"] not in {"Buy", "Sell"}:
                        raise ValueError("Wrong trade")
                    identity = row["i"]
                    if not isinstance(identity, str) or not re.fullmatch(
                        r"[A-Za-z0-9-]{1,100}", identity
                    ):
                        raise ValueError("Invalid trade ID")
                    self.trade_cursor += 1
                    result.append(
                        self.event(
                            EventType.TRADE,
                            identity,
                            self.trade_cursor,
                            integer(row["T"]),
                            received,
                            TradePayload(
                                decimal_string(row["p"]),
                                decimal_string(row["v"]),
                                row["S"] == "Sell",
                            ),
                            "unverified",
                        )
                    )
                return result
            if topic.startswith("orderbook.1."):
                row = data["data"]
                if row["s"] != self.subscription.symbol or len(row["b"]) != 1 or len(row["a"]) != 1:
                    raise ValueError("Expected complete L1 snapshot")
                update = integer(row["u"])
                quote = QuotePayload(
                    decimal_string(row["b"][0][0]),
                    decimal_string(row["a"][0][0]),
                    decimal_string(row["b"][0][1]),
                    decimal_string(row["a"][0][1]),
                )
                # Publication timestamp survives service update-ID resets and idle snapshots.
                return [
                    self.event(
                        EventType.QUOTE,
                        f"{update}:{publication}",
                        publication,
                        publication,
                        received,
                        quote,
                        "snapshot",
                    )
                ]
            rows = data["data"]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 1024:
                raise ValueError("Invalid candle batch")
            result = []
            for row in rows:
                if (
                    row["interval"] != INTERVAL_CODES[self.subscription.interval]
                    or type(row["confirm"]) is not bool
                ):
                    raise ValueError("Wrong candle interval or confirmation")
                if not row["confirm"]:
                    continue
                opening = integer(row["start"])
                if (
                    integer(row["end"])
                    != opening + INTERVALS[self.subscription.interval] * 1000 - 1
                    or row["end"] > publication
                ):
                    raise ValueError("Invalid candle duration")
                result.append(
                    self.event(
                        EventType.BAR,
                        f"{self.subscription.interval}:{opening}",
                        opening,
                        publication,
                        received,
                        BarPayload(
                            *(
                                decimal_string(row[k])
                                for k in ("open", "high", "low", "close", "volume")
                            ),
                            INTERVALS[self.subscription.interval],
                            timestamp(opening),
                        ),
                        "contiguous",
                    )
                )
            return result
        except (KeyError, TypeError, ValueError, OverflowError, UnicodeDecodeError):
            raise MalformedMarketData("Invalid Bybit market message") from None


class BybitWebSocket(BinanceWebSocket):
    def __init__(self, subscription: Subscription, **kwargs):
        super().__init__(subscription, **kwargs)
        self.normalizer = BybitNormalizer(subscription)

    async def events(self) -> AsyncIterator[CanonicalMarketEvent]:
        for attempt in range(self.max_reconnects + 1):
            try:
                async with self._connect(
                    WS_BASE,
                    ping_interval=None,
                    open_timeout=10,
                    close_timeout=5,
                    max_size=1_000_000,
                    max_queue=16,
                    proxy=None,
                ) as connection:
                    await connection.send(
                        json.dumps({"op": "subscribe", "args": list(self.normalizer.topics)})
                    )
                    self.on_connection(True)
                    loop = asyncio.get_running_loop()
                    expires = loop.time() + self.lifetime
                    last_data = loop.time()
                    next_ping = loop.time() + 20
                    confirmed = False
                    pending = asyncio.create_task(connection.recv())
                    try:
                        while loop.time() < expires:
                            timeout = (
                                min(next_ping, last_data + self.idle_timeout, expires) - loop.time()
                            )
                            done, _ = await asyncio.wait({pending}, timeout=max(0, timeout))
                            if pending in done:
                                raw = pending.result()
                                pending = asyncio.create_task(connection.recv())
                                try:
                                    envelope = json.loads(raw)
                                    events = self.normalizer.message(envelope, self._clock())
                                except MalformedMarketData:
                                    self.malformed_count += 1
                                    self.on_malformed()
                                    continue
                                except (json.JSONDecodeError, UnicodeDecodeError):
                                    self.malformed_count += 1
                                    self.on_malformed()
                                    continue
                                if envelope.get("op") == "subscribe":
                                    confirmed = True
                                if "topic" in envelope:
                                    last_data = loop.time()
                                    if not confirmed:
                                        raise MarketDataError(
                                            "Bybit data arrived before subscription confirmation"
                                        )
                                for event in events:
                                    yield event
                            if loop.time() >= last_data + self.idle_timeout:
                                raise TimeoutError
                            if loop.time() >= next_ping:
                                await connection.send('{"op":"ping"}')
                                next_ping = loop.time() + 20
                    finally:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                pass
            finally:
                self.on_connection(False)
            if attempt < self.max_reconnects:
                self.reconnect_count += 1
                await self._sleep(min(2**attempt, 30))
        raise MarketDataError("Public WebSocket reconnect budget exhausted")
