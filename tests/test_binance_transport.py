import asyncio
import io
import json
from contextlib import suppress
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from vision.market_data.adapters.binance import BinanceREST, BinanceWebSocket, MarketDataError
from vision.market_data.hub import MarketDataHub


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result if isinstance(result, bytes) else json.dumps(result).encode())


def client(results, **kwargs):
    opener = Opener(results)
    sleeps = []
    rest = BinanceREST(opener=opener, sleep=sleeps.append, min_request_interval=0, **kwargs)
    return rest, opener, sleeps


def test_rest_is_public_get_only_and_no_credentials(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic-do-not-use")
    rest, opener, _ = client([{"serverTime": 100}])
    assert rest.get("time") == {"serverTime": 100}
    request = opener.requests[0]
    assert request.full_url == "https://data-api.binance.vision/api/v3/time"
    assert request.get_method() == "GET"
    assert not any(
        "key" in key.lower() or "authorization" in key.lower()
        for key, value in request.header_items()
    )
    with pytest.raises(ValueError, match="allowlist"):
        rest.get("order")
    assert len(opener.requests) == 1


def test_exchange_filters_map_to_instrument():
    rest, _, _ = client(
        [
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                        ],
                    }
                ]
            }
        ]
    )
    instrument = rest.instrument("BTCUSDT")
    assert instrument.instrument_id == "BINANCE:SPOT:BTCUSDT"
    assert str(instrument.tick_size) == "0.01"
    assert str(instrument.lot_size) == "0.00001"


def rate_error(seconds, code=429):
    headers = Message()
    headers["Retry-After"] = str(seconds)
    return HTTPError("https://data-api.binance.vision", code, "synthetic", headers, io.BytesIO())


def test_retry_after_is_honored():
    rest, opener, sleeps = client([rate_error(3), []])
    assert rest.trades("BTCUSDT") == []
    assert sleeps == [3]
    assert len(opener.requests) == 2


def test_long_cooldown_does_not_retry_early():
    rest, opener, sleeps = client([rate_error(120)])
    with pytest.raises(MarketDataError, match="longer cooldown"):
        rest.trades("BTCUSDT")
    assert len(opener.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "failure",
    [
        URLError("synthetic-sensitive-proxy-detail"),
        rate_error(3, code=451),
        b"invalid-json",
        b"x" * 2_000_001,
    ],
    ids=["connection", "regional-block", "invalid-json", "oversized"],
)
def test_remote_errors_are_bounded_and_sanitized(failure):
    rest, opener, _ = client([failure], attempts=1)
    with pytest.raises(MarketDataError) as error:
        rest.get("time")
    assert "synthetic-sensitive-proxy-detail" not in str(error.value)
    assert len(opener.requests) == 1


def test_trade_pagination_and_utc_bar_query(subscription):
    rest, opener, _ = client([[], []])
    rest.trades("BTCUSDT", limit=5, from_id=10)
    rest.bars(subscription, limit=2)
    assert "fromId=10" in opener.requests[0].full_url
    assert "timeZone=0" in opener.requests[1].full_url
    with pytest.raises(ValueError):
        rest.trades("BTCUSDT", limit=1001)


class Socket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def recv(self):
        if self.frames:
            frame = self.frames.pop(0)
            if isinstance(frame, Exception):
                raise frame
            return frame
        await asyncio.Event().wait()


def test_stream_reconnects_normalizes_and_closes(subscription, raw_trade, now):
    socket = Socket([json.dumps(raw_trade)])
    calls = []
    transitions = []

    def connect(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("synthetic connection failure")
        return socket

    async def no_sleep(delay):
        pass

    adapter = BinanceWebSocket(
        subscription,
        connect_factory=connect,
        clock=lambda: now,
        sleep=no_sleep,
        on_connection=transitions.append,
    )

    async def run():
        iterator = adapter.events()
        try:
            event = await anext(iterator)
            assert event.sequence == 100
        finally:
            await iterator.aclose()

    asyncio.run(run())
    assert len(calls) == 2
    assert all(url.startswith("wss://data-stream.binance.vision:443/") for url in calls)
    assert adapter.reconnect_count == 1
    assert socket.closed
    assert transitions[-1] is False


def test_idle_timeout_exhausts_finite_retry_budget(subscription):
    sockets = []

    def connect(url, **kwargs):
        socket = Socket([TimeoutError()])
        sockets.append(socket)
        return socket

    async def no_sleep(delay):
        pass

    async def run():
        adapter = BinanceWebSocket(
            subscription, connect_factory=connect, sleep=no_sleep, max_reconnects=2
        )
        with pytest.raises(MarketDataError, match="budget exhausted"):
            await anext(adapter.events())

    asyncio.run(run())
    assert len(sockets) == 3
    assert all(socket.closed for socket in sockets)


def test_cancellation_closes_socket_without_retry(subscription):
    socket = Socket([])
    adapter = BinanceWebSocket(subscription, connect_factory=lambda *args, **kwargs: socket)

    async def run():
        iterator = adapter.events()
        pending = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await iterator.aclose()

    asyncio.run(run())
    assert socket.closed
    assert adapter.reconnect_count == 0


def test_transport_to_hub_quarantines_malformed_and_duplicate(
    subscription, raw_trade, now, binance_instrument
):
    socket = Socket(
        ["{", json.dumps(raw_trade), json.dumps(raw_trade), json.dumps({**raw_trade, "a": 101})]
    )
    adapter = BinanceWebSocket(
        subscription, connect_factory=lambda *args, **kwargs: socket, clock=lambda: now
    )
    hub = MarketDataHub(clock=lambda: now)
    hub.register(binance_instrument, subscription)

    async def run():
        iterator = hub.stream(adapter)
        try:
            accepted = [await anext(iterator), await anext(iterator)]
            assert [event.sequence for event in accepted] == [100, 101]
        finally:
            await iterator.aclose()

    asyncio.run(run())
    assert hub.rejected == {"malformed": 1, "duplicate": 1}
    assert socket.closed
    assert all(value == "disconnected" for value in hub.status()["streams"].values())
