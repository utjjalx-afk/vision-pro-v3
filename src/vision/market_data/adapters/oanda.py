"""Authorized OANDA v20 GET-only data. No orders, account balances or execution API."""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from vision.analysis.contracts import wire
from vision.core.codec import decimal_string
from vision.core.contracts import (
    AssetClass,
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    QuotePayload,
    _utc,
)
from vision.core.instruments import CanonicalSymbol, InstrumentRecord, digest
from vision.market_data.adapters.binance import MarketDataError, NoRedirect, integer
from vision.strategies.dsl import strict_json

SYMBOLS = {
    "EUR_USD": CanonicalSymbol(AssetClass.FOREX, "EUR", "USD"),
    "XAU_USD": CanonicalSymbol(AssetClass.METALS, "XAU", "USD"),
    "XAG_USD": CanonicalSymbol(AssetClass.METALS, "XAG", "USD"),
}
GRANULARITIES = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


def symbol_name(value):
    if value not in SYMBOLS:
        raise ValueError("Unsupported explicit OANDA instrument mapping")
    return value


def source_name(environment):
    if environment not in {"practice", "live"}:
        raise ValueError("Explicit OANDA data environment required")
    return f"oanda.{environment}"


def provider_time(value):
    """Preserve exact nanoseconds in provider_sequence; UTC datetime floors to microseconds."""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("Bounded RFC3339 provider timestamp required")
    return datetime.fromisoformat(value).astimezone(UTC)


def time_sequence(value):
    at = provider_time(value)
    # Convert to exact epoch nanoseconds rather than float timestamps.
    delta = at - datetime(1970, 1, 1, tzinfo=UTC)
    microseconds = delta // timedelta(microseconds=1)
    fraction = re.search(r"\.(\d+)", value)
    remainder = int(fraction[1].ljust(9, "0")[6:]) if fraction else 0
    return microseconds * 1000 + remainder


@dataclass(frozen=True)
class MarketMetadata:
    """Price/quantity display metadata is not an execution InstrumentSpec."""

    symbol: str
    environment: str
    observed_at: datetime
    pip_location: int
    display_precision: int
    trade_units_precision: int
    minimum_trade_size: Decimal
    metadata_digest: str

    def __post_init__(self):
        symbol_name(self.symbol)
        source_name(self.environment)
        _utc(self.observed_at)
        if type(self.pip_location) is not int or not -12 <= self.pip_location <= 6:
            raise ValueError("Invalid pip location")
        for value in (self.display_precision, self.trade_units_precision):
            if type(value) is not int or not 0 <= value <= 12:
                raise ValueError("Invalid metadata precision")
        if (
            not isinstance(self.minimum_trade_size, Decimal)
            or not self.minimum_trade_size.is_finite()
            or self.minimum_trade_size <= 0
            or len(self.metadata_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.metadata_digest)
        ):
            raise ValueError("Explicit positive size and metadata digest required")

    @property
    def canonical(self):
        return SYMBOLS[self.symbol]

    @property
    def source(self):
        return source_name(self.environment)

    @property
    def instrument_id(self):
        return f"OANDA:{self.environment.upper()}:{self.symbol}"

    @property
    def pip_size(self):
        return Decimal((0, (1,), self.pip_location))

    @property
    def display_quantum(self):
        return Decimal((0, (1,), -self.display_precision))

    @property
    def revision(self):
        return digest(wire(self))

    def verify_record(self, record):
        """An independently verified full record may be integrated, never manufactured here."""
        if (
            not isinstance(record, InstrumentRecord)
            or record.spec.instrument_id != self.instrument_id
            or record.spec.venue != "OANDA_V20"
            or record.spec.symbol != self.symbol
            or record.canonical != self.canonical
            or record.provenance.source != self.source
            or record.provenance.metadata_digest != self.metadata_digest
        ):
            raise ValueError("Verified execution economics must match market metadata provenance")
        return record


def parse_metadata(row, environment, observed_at):
    symbol = symbol_name(row["name"])
    expected = "CURRENCY" if symbol == "EUR_USD" else "METAL"
    if row["type"] != expected:
        raise ValueError("Provider instrument type disagrees with canonical mapping")
    # Project only public instrument fields. Never retain an authorized response/account body.
    projected = {
        k: row[k]
        for k in (
            "name",
            "type",
            "pipLocation",
            "displayPrecision",
            "tradeUnitsPrecision",
            "minimumTradeSize",
        )
    }
    return MarketMetadata(
        symbol,
        environment,
        observed_at,
        row["pipLocation"],
        row["displayPrecision"],
        row["tradeUnitsPrecision"],
        decimal_string(row["minimumTradeSize"]),
        digest(projected),
    )


class OandaNormalizer:
    def __init__(self, symbol, environment, *, source_epoch):
        self.symbol = symbol_name(symbol)
        self.source = source_name(environment)
        self.instrument_id = f"OANDA:{environment.upper()}:{symbol}"
        if type(source_epoch) is not int or source_epoch < 1:
            raise ValueError("Explicit positive source epoch required")
        self.source_epoch = source_epoch

    def quote(self, row, received_at):
        if row["instrument"] != self.symbol or row["status"] != "tradeable":
            raise ValueError("Wrong instrument or unavailable provider quote")
        _utc(received_at)
        ladders = []
        for side in ("bids", "asks"):
            rows = row[side]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
                raise ValueError("Unavailable or excessive quote depth")
            levels = [(decimal_string(v["price"]), integer(v["liquidity"])) for v in rows]
            if any(price <= 0 or size <= 0 for price, size in levels):
                raise ValueError("No usable liquidity")
            prices = [v[0] for v in levels]
            if prices != sorted(prices, reverse=side == "bids") or len(set(prices)) != len(prices):
                raise ValueError("Invalid price ladder")
            ladders.append(levels[0])
        bid, ask = ladders
        payload = QuotePayload(bid[0], ask[0], Decimal(bid[1]), Decimal(ask[1]))
        sequence = time_sequence(row["time"])
        identity = digest(
            {
                "source": self.source,
                "instrument": self.instrument_id,
                "time": row["time"],
                "payload": wire(payload),
            }
        )
        return CanonicalMarketEvent(
            identity,
            self.source,
            self.instrument_id,
            EventType.QUOTE,
            provider_time(row["time"]),
            received_at,
            sequence,
            payload,
            source_epoch=self.source_epoch,
            provider_sequence=row["time"],
            continuity="snapshot",
        )

    def candles(self, response, received_at, *, granularity, delivery_kind):
        _utc(received_at)
        if granularity not in GRANULARITIES or delivery_kind not in {"live", "backfill"}:
            raise ValueError("Explicit supported candle semantics required")
        if response["instrument"] != self.symbol or response["granularity"] != granularity:
            raise ValueError("Candle response mismatch")
        rows = response["candles"]
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("Candle response exceeds bound")
        events = []
        seconds = GRANULARITIES[granularity]
        previous = None
        for row in rows:
            if type(row["complete"]) is not bool:
                raise ValueError("Explicit completion flag required")
            open_at = provider_time(row["time"])
            sequence = (open_at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
            if sequence % (seconds * 1000) or time_sequence(row["time"]) % 1000000000:
                raise ValueError("Candle open is not on fixed UTC grid")
            if previous is not None and open_at <= previous:
                raise ValueError("Candle response is duplicated or unordered")
            previous = open_at
            if not row["complete"]:
                continue
            close_at = open_at + timedelta(seconds=seconds)
            if close_at > received_at:
                raise ValueError("Future complete candle")
            volume = Decimal(integer(row["volume"]))
            for side in ("bid", "ask"):
                data = row[side]
                if set(data) != {"o", "h", "l", "c"}:
                    raise ValueError("Exact OHLC candle component required")
                payload = BarPayload(
                    *(decimal_string(data[k]) for k in ("o", "h", "l", "c")),
                    volume,
                    seconds,
                    open_at,
                    price_basis=side,
                    volume_basis="price_count",
                )
                identity = digest(
                    {
                        "source": self.source,
                        "instrument": self.instrument_id,
                        "open": row["time"],
                        "payload": wire(payload),
                    }
                )
                events.append(
                    CanonicalMarketEvent(
                        identity,
                        self.source,
                        self.instrument_id,
                        EventType.BAR,
                        close_at,
                        received_at,
                        sequence,
                        payload,
                        source_epoch=self.source_epoch,
                        provider_sequence=row["time"],
                        continuity="unverified",
                        delivery_kind=delivery_kind,
                    )
                )
            bid, ask = events[-2:]
            if any(
                getattr(bid.payload, k) > getattr(ask.payload, k)
                for k in ("open", "high", "low", "close")
            ):
                raise ValueError("Crossed candle components")
        return tuple(events)


class OandaREST:
    """Fixed hosts, GET-only typed endpoints, no redirects, bounded replies and safe errors."""

    def __init__(
        self,
        *,
        token,
        account_id,
        environment,
        opener=None,
        timeout=10,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        import os

        from vision.config import load_settings

        load_settings(os.environ)
        source_name(environment)
        if not isinstance(token, str) or not 1 <= len(token) <= 512 or re.search(r"\s", token):
            raise ValueError("Runtime authorization token required")
        if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", account_id):
            raise ValueError("Runtime account identifier required")
        if type(timeout) not in {int, float} or not 0 < timeout <= 30:
            raise ValueError("Bounded request timeout required")
        self._token, self._account_id = token, account_id
        self.environment = environment
        self._base = (
            "https://api-fxpractice.oanda.com"
            if environment == "practice"
            else "https://api-fxtrade.oanda.com"
        )
        self._opener = opener or build_opener(NoRedirect())
        self.timeout, self._sleep, self._monotonic = timeout, sleep, monotonic
        self._last_request = None

    def _get(self, path, params):
        allowed = {
            f"/v3/accounts/{self._account_id}/instruments",
            f"/v3/accounts/{self._account_id}/pricing",
        } | {f"/v3/instruments/{symbol}/candles" for symbol in SYMBOLS}
        if path not in allowed:
            raise ValueError("Endpoint outside the authorized market-data allowlist")
        if self._last_request is not None:
            delay = 0.25 - (self._monotonic() - self._last_request)
            if delay > 0:
                self._sleep(delay)
        self._last_request = self._monotonic()
        request = Request(
            self._base + path + "?" + urlencode(params),
            method="GET",
            headers={
                "Authorization": "Bearer " + self._token,
                "Accept-Datetime-Format": "RFC3339",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as reply:
                if reply.status != 200:
                    raise MarketDataError("OANDA data request rejected")
                raw = reply.read(2000001)
            if len(raw) > 2000000:
                raise MarketDataError("OANDA response size exceeded")
            return strict_json(raw.decode("utf-8"), limit=2000000)
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            ValueError,
            UnicodeError,
            RecursionError,
        ):
            # Never propagate raw response body, request URL, account or token.
            raise MarketDataError("OANDA data request failed") from None

    def metadata(self, symbol):
        symbol_name(symbol)
        return self._get(f"/v3/accounts/{self._account_id}/instruments", {"instruments": symbol})

    def pricing(self, symbol):
        symbol_name(symbol)
        return self._get(
            f"/v3/accounts/{self._account_id}/pricing",
            {
                "instruments": symbol,
                "includeHomeConversions": "false",
                "includeUnitsAvailable": "false",
            },
        )

    def candles(self, symbol, granularity, *, start=None, end=None, count=100):
        symbol_name(symbol)
        if granularity not in GRANULARITIES or type(count) is not int or not 1 <= count <= 1000:
            raise ValueError("Unsupported or unbounded candles request")
        params = {
            "granularity": granularity,
            "price": "BA",
            "smooth": "false",
            "dailyAlignment": 0,
            "alignmentTimezone": "UTC",
            "weeklyAlignment": "Monday",
        }
        if (start is None) != (end is None):
            raise ValueError("Explicit complete time range required")
        if start is None:
            params["count"] = count
        else:
            _utc(start)
            _utc(end)
            if (
                not timedelta(0)
                < end - start
                <= timedelta(seconds=GRANULARITIES[granularity] * 1000)
            ):
                raise ValueError("Historical candle range exceeds bound")
            params.update({"from": _utc(start), "to": _utc(end), "includeFirst": "true"})
        return self._get(f"/v3/instruments/{symbol}/candles", params)
