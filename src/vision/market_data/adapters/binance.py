"""Binance Spot public-only REST/WS adapter and pure payload normalization.

References: Binance's market_data_only, market-data-endpoints and
web-socket-streams documentation. No authenticated or order endpoint exists here.
"""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from vision.core.codec import decimal_string
from vision.core.contracts import (
    AssetClass,
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    InstrumentSpec,
    QuotePayload,
    TimestampBasis,
    TradePayload,
    _utc,
)

REST_BASE = "https://data-api.binance.vision"
WS_BASE = "wss://data-stream.binance.vision:443"
SOURCE = "binance.spot"
INTERVALS = {
    "1s": 1,
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class MarketDataError(RuntimeError):
    """Safe diagnostic category; never includes raw remote bodies or credentials."""


class MalformedMarketData(ValueError):
    """A remote message failed its provider contract."""


def symbol_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{2,30}", value):
        raise ValueError("Use an uppercase Binance Spot symbol")
    return value


def integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative provider integer")
    return value


def timestamp(value: object) -> datetime:
    return _EPOCH + timedelta(milliseconds=integer(value))


def epoch_ms(value: datetime) -> int:
    _utc(value)
    return (value - _EPOCH) // timedelta(milliseconds=1)


@dataclass(frozen=True)
class Subscription:
    symbol: str
    kinds: tuple[str, ...] = ("trade", "quote", "bar")
    interval: str = "1m"

    def __post_init__(self):
        symbol_name(self.symbol)
        if not self.kinds or len(set(self.kinds)) != len(self.kinds):
            raise ValueError("Choose distinct nonempty stream kinds")
        if set(self.kinds) - {"trade", "quote", "bar"}:
            raise ValueError("Unknown market stream kind")
        if self.interval not in INTERVALS:
            raise ValueError("Unsupported fixed-duration UTC interval (calendar months excluded)")

    @property
    def streams(self) -> tuple[str, ...]:
        names = {"trade": "aggTrade", "quote": "bookTicker", "bar": f"kline_{self.interval}"}
        return tuple(f"{self.symbol.lower()}@{names[kind]}" for kind in self.kinds)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketDataError("Unexpected public-data redirect")


class BinanceREST:
    """Bounded, paced GET requests on a fixed public-data endpoint allowlist."""

    PATHS = {"exchangeInfo", "aggTrades", "klines", "time", "ping"}

    def __init__(
        self,
        *,
        opener=None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        attempts: int = 3,
        timeout: float = 10,
        min_request_interval: float = 0.25,
    ):
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("REST attempts must be between 1 and 5")
        if not 0 < timeout <= 30 or not 0 <= min_request_interval <= 30:
            raise ValueError("Invalid REST timeout or pacing")
        self._opener = opener or build_opener(NoRedirect())
        self._sleep = sleep
        self._monotonic = monotonic
        self.attempts = attempts
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self._last_request: float | None = None

    def get(self, endpoint: str, params: dict | None = None) -> object:
        if endpoint not in self.PATHS:
            raise ValueError("Endpoint is outside the public market-data allowlist")
        query = urlencode(params or {})
        url = f"{REST_BASE}/api/v3/{endpoint}" + (f"?{query}" if query else "")
        for attempt in range(self.attempts):
            if self._last_request is not None:
                delay = self.min_request_interval - (self._monotonic() - self._last_request)
                if delay > 0:
                    self._sleep(delay)
            self._last_request = self._monotonic()
            request = Request(url, headers={"Accept": "application/json"}, method="GET")
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    body = response.read(2_000_001)
                    if len(body) > 2_000_000:
                        raise MarketDataError("Public REST response exceeds size limit")
                    result = json.loads(body)
                    if isinstance(result, dict) and "code" in result:
                        raise MarketDataError("Binance rejected the public-data request")
                    return result
            except HTTPError as error:
                error.close()
                if error.code not in {429, 500, 502, 503, 504} or attempt + 1 == self.attempts:
                    raise MarketDataError(f"Public REST HTTP {error.code}") from None
                retry_after = (error.headers or {}).get("Retry-After", str(2**attempt))
                try:
                    delay = float(retry_after)
                except (ValueError, TypeError):
                    raise MarketDataError("Unsupported REST retry interval") from None
                if not 0 <= delay <= 30:
                    raise MarketDataError("REST rate limit requires a longer cooldown") from None
                self._sleep(delay)
            except (URLError, TimeoutError, OSError):
                if attempt + 1 == self.attempts:
                    raise MarketDataError("Public REST connection unavailable") from None
                self._sleep(2**attempt)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise MarketDataError("Invalid public REST JSON") from None
        raise MarketDataError("Public REST request exhausted")

    def instrument(self, symbol: str) -> InstrumentSpec:
        symbol_name(symbol)
        try:
            data = self.get("exchangeInfo", {"symbol": symbol})
            items = data["symbols"]
            if len(items) != 1:
                raise ValueError("Expected one instrument")
            item = items[0]
            if item["symbol"] != symbol or item["status"] != "TRADING":
                raise ValueError("Instrument unavailable")
            filters = {entry["filterType"]: entry for entry in item["filters"]}
            return InstrumentSpec(
                instrument_id=f"BINANCE:SPOT:{symbol}",
                venue="BINANCE_SPOT",
                symbol=symbol,
                asset_class=AssetClass.CRYPTO,
                base_currency=item["baseAsset"],
                quote_currency=item["quoteAsset"],
                tick_size=decimal_string(filters["PRICE_FILTER"]["tickSize"]),
                lot_size=decimal_string(filters["LOT_SIZE"]["stepSize"]),
                minimum_quantity=decimal_string(filters["LOT_SIZE"]["minQty"]),
                contract_size=Decimal("1"),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            raise MalformedMarketData("Unsupported Binance instrument metadata") from None

    def trades(self, symbol: str, *, limit: int = 100, from_id: int | None = None) -> list:
        params = {"symbol": symbol_name(symbol), "limit": _limit(limit)}
        if from_id is not None:
            params["fromId"] = integer(from_id)
        result = self.get("aggTrades", params)
        if not isinstance(result, list):
            raise MalformedMarketData("Expected public aggregate trade array")
        return result

    def bars(self, subscription: Subscription, *, limit: int = 100) -> list:
        result = self.get(
            "klines",
            {
                "symbol": subscription.symbol,
                "interval": subscription.interval,
                "timeZone": "0",
                "limit": _limit(limit),
            },
        )
        if not isinstance(result, list):
            raise MalformedMarketData("Expected public kline array")
        return result


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError("REST limit must be between 1 and 1000")
    return value


class BinanceNormalizer:
    def __init__(self, subscription: Subscription):
        self.subscription = subscription

    def _event(self, kind, seq, source_ts, received, payload, basis=TimestampBasis.EXCHANGE):
        interval = f":{self.subscription.interval}" if kind is EventType.BAR else ""
        return CanonicalMarketEvent(
            event_id=f"{SOURCE}:{self.subscription.symbol}:{kind.value}{interval}:{seq}",
            source=SOURCE,
            instrument_id=f"BINANCE:SPOT:{self.subscription.symbol}",
            event_type=kind,
            source_ts=source_ts,
            received_ts=received,
            sequence=seq,
            payload=payload,
            timestamp_basis=basis,
        )

    def trade(self, data: dict, received: datetime) -> CanonicalMarketEvent:
        if type(data["m"]) is not bool:
            raise ValueError("Invalid trade maker flag")
        return self._event(
            EventType.TRADE,
            integer(data["a"]),
            timestamp(data["T"]),
            received,
            TradePayload(decimal_string(data["p"]), decimal_string(data["q"]), data["m"]),
        )

    def rest_bar(self, row: list, received: datetime) -> CanonicalMarketEvent | None:
        if not isinstance(row, list) or len(row) != 12:
            raise ValueError("Invalid REST kline shape")
        opening, closing = integer(row[0]), integer(row[6])
        if closing >= epoch_ms(received):
            return None  # Only finalized candles enter the canonical bus.
        if closing != opening + INTERVALS[self.subscription.interval] * 1000 - 1:
            raise ValueError("Invalid UTC candle duration")
        return self._event(
            EventType.BAR,
            opening,
            timestamp(closing),
            received,
            BarPayload(
                *(decimal_string(row[index]) for index in (1, 2, 3, 4, 5)),
                INTERVALS[self.subscription.interval],
                timestamp(opening),
            ),
        )

    def message(self, raw: str | bytes | dict, received: datetime) -> CanonicalMarketEvent | None:
        try:
            _utc(received)
            if isinstance(raw, (str, bytes)) and len(raw) > 1_000_000:
                raise ValueError("Message exceeds size limit")
            envelope = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if not isinstance(envelope, dict):
                raise ValueError("Expected message object")
            if "stream" in envelope:
                if envelope["stream"] not in self.subscription.streams:
                    raise ValueError("Unsubscribed stream")
                data = envelope["data"]
            else:
                data = envelope
            if not isinstance(data, dict) or data.get("s") != self.subscription.symbol:
                raise ValueError("Wrong instrument")
            kind = data.get("e")
            names = {
                "aggTrade": "aggTrade",
                "kline": f"kline_{self.subscription.interval}",
                None: "bookTicker",
            }
            if "stream" in envelope and envelope["stream"] != (
                f"{self.subscription.symbol.lower()}@{names.get(kind, 'unsupported')}"
            ):
                raise ValueError("Stream envelope and event type disagree")
            if kind == "aggTrade" and "trade" in self.subscription.kinds:
                return self.trade(data, received)
            if kind is None and "quote" in self.subscription.kinds:
                return self._event(
                    EventType.QUOTE,
                    integer(data["u"]),
                    received,
                    received,
                    QuotePayload(*(decimal_string(data[key]) for key in ("b", "a", "B", "A"))),
                    TimestampBasis.RECEIPT,
                )
            if kind == "kline" and "bar" in self.subscription.kinds:
                kline = data["k"]
                if (
                    kline["s"] != self.subscription.symbol
                    or kline["i"] != self.subscription.interval
                ):
                    raise ValueError("Wrong candle stream")
                if type(kline["x"]) is not bool:
                    raise ValueError("Invalid candle finalization flag")
                if not kline["x"]:
                    return None
                opening, closing = integer(kline["t"]), integer(kline["T"])
                if closing != opening + INTERVALS[self.subscription.interval] * 1000 - 1:
                    raise ValueError("Invalid UTC candle duration")
                if closing > integer(data["E"]):
                    raise ValueError("Candle finalization precedes its close")
                return self._event(
                    EventType.BAR,
                    opening,
                    timestamp(data["E"]),
                    received,
                    BarPayload(
                        *(decimal_string(kline[key]) for key in ("o", "h", "l", "c", "v")),
                        INTERVALS[self.subscription.interval],
                        timestamp(opening),
                    ),
                )
            raise ValueError("Unsupported or unsubscribed event")
        except (KeyError, ValueError, TypeError, OverflowError, UnicodeDecodeError):
            raise MalformedMarketData("Invalid Binance market message") from None


class BinanceWebSocket:
    """Public combined stream with bounded queues, idle detection and finite retries."""

    def __init__(
        self,
        subscription: Subscription,
        *,
        connect_factory=connect,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        max_reconnects: int = 3,
        idle_timeout: float = 30,
        lifetime: float = 23 * 3600,
        on_connection: Callable[[bool], None] = lambda connected: None,
        on_malformed: Callable[[], None] = lambda: None,
    ):
        if type(max_reconnects) is not int or not 0 <= max_reconnects <= 10:
            raise ValueError("Invalid reconnect limit")
        if not 0 < idle_timeout <= 300 or not 0 < lifetime <= 23 * 3600:
            raise ValueError("Invalid stream timing limits")
        self.subscription = subscription
        self.normalizer = BinanceNormalizer(subscription)
        self._connect = connect_factory
        self._clock = clock
        self._sleep = sleep
        self.max_reconnects = max_reconnects
        self.idle_timeout = idle_timeout
        self.lifetime = lifetime
        self.on_connection = on_connection
        self.on_malformed = on_malformed
        self.malformed_count = 0
        self.reconnect_count = 0

    async def events(self) -> AsyncIterator[CanonicalMarketEvent]:
        url = f"{WS_BASE}/stream?streams={'/'.join(self.subscription.streams)}"
        for attempt in range(self.max_reconnects + 1):
            try:
                async with self._connect(
                    url,
                    ping_interval=None,
                    open_timeout=10,
                    close_timeout=5,
                    max_size=1_000_000,
                    max_queue=16,
                    proxy=None,
                ) as connection:
                    self.on_connection(True)
                    loop = asyncio.get_running_loop()
                    expires = loop.time() + self.lifetime
                    while True:
                        remaining = expires - loop.time()
                        if remaining <= 0:
                            break  # Rotate before Binance's connection lifetime limit.
                        raw = await asyncio.wait_for(
                            connection.recv(), min(self.idle_timeout, remaining)
                        )
                        try:
                            event = self.normalizer.message(raw, self._clock())
                        except MalformedMarketData:
                            self.malformed_count += 1
                            self.on_malformed()
                            continue
                        if event is not None:
                            yield event
            except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError):
                pass
            finally:
                self.on_connection(False)
            if attempt < self.max_reconnects:
                self.reconnect_count += 1
                await self._sleep(min(2**attempt, 30))
        raise MarketDataError("Public WebSocket reconnect budget exhausted")
