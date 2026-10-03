import asyncio
import json
from contextlib import aclosing
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

from vision.core.bus.events import BackpressureError, EventBus
from vision.core.codec import event_from_dict
from vision.core.contracts import BarPayload, EventType, QuotePayload
from vision.market_data.adapters.binance import BinanceREST, Subscription, epoch_ms
from vision.market_data.adapters.bybit import (
    BybitNormalizer,
    BybitREST,
    BybitWebSocket,
    MalformedMarketData,
    MarketDataError,
    topics,
)
from vision.market_data.failover import ControlledFailover, controlled_stream
from vision.market_data.health.gate import stream_key
from vision.market_data.hub import MarketDataHub


def bybit_instrument(binance):
    return replace(binance, instrument_id="BYBIT:SPOT:BTCUSDT", venue="BYBIT_SPOT")


def trade_frame(now, ids=("uuid-1", "uuid-2")):
    return {
        "topic": "publicTrade.BTCUSDT",
        "type": "snapshot",
        "ts": epoch_ms(now),
        "data": [
            {
                "s": "BTCUSDT",
                "T": epoch_ms(now),
                "S": "Buy",
                "p": "100",
                "v": "0.001",
                "i": identity,
                "seq": 10,
            }
            for identity in ids
        ],
    }


def quote_frame(now, update=100):
    return {
        "topic": "orderbook.1.BTCUSDT",
        "type": "snapshot",
        "ts": epoch_ms(now),
        "data": {
            "s": "BTCUSDT",
            "u": update,
            "seq": 100,
            "b": [["100", "1"]],
            "a": [["100.1", "2"]],
        },
    }


def test_trade_batch_uuid_same_exchange_sequence(now, subscription, binance_instrument):
    normalizer = BybitNormalizer(subscription)
    events = normalizer.message(trade_frame(now), now)
    hub = MarketDataHub(clock=lambda: now)
    hub.register(bybit_instrument(binance_instrument), subscription)
    assert [event.sequence for event in events] == [1, 2]
    assert all(event.continuity == "unverified" for event in events)
    assert all(hub.ingest(event).accepted for event in events)
    replay = normalizer.message(trade_frame(now), now)
    assert all(hub.ingest(event).reasons == ("duplicate",) for event in replay)


def test_quote_idle_snapshot_and_service_restart(now, subscription, binance_instrument):
    normalizer = BybitNormalizer(subscription)
    hub = MarketDataHub(clock=lambda: now + timedelta(seconds=2))
    hub.register(bybit_instrument(binance_instrument), subscription)
    for i, update in enumerate((100, 100, 1)):
        event = normalizer.message(
            quote_frame(now + timedelta(seconds=i), update), now + timedelta(seconds=i)
        )[0]
        assert hub.ingest(event).accepted
        assert event.continuity == "snapshot"


@pytest.mark.parametrize("change", ["symbol", "topic", "delta", "crossed", "empty", "missing"])
def test_bad_quote_rejected(now, subscription, change):
    frame = quote_frame(now)
    if change == "symbol":
        frame["data"]["s"] = "ETHUSDT"
    if change == "topic":
        frame["topic"] = "orderbook.50.BTCUSDT"
    if change == "delta":
        frame["type"] = "delta"
    if change == "crossed":
        frame["data"]["b"][0][0] = "200"
    if change == "empty":
        frame["data"]["a"] = []
    if change == "missing":
        del frame["data"]["u"]
    with pytest.raises(MalformedMarketData):
        BybitNormalizer(subscription).message(frame, now)


@pytest.mark.parametrize("interval", ["1s", "8h", "3d"])
def test_incompatible_interval_rejected(interval):
    with pytest.raises(ValueError):
        topics(Subscription("BTCUSDT", interval=interval))


def test_control_ack_fail_closed(now, subscription):
    normalizer = BybitNormalizer(subscription)
    assert normalizer.message({"op": "pong"}, now) == []
    assert normalizer.message({"op": "subscribe", "success": True}, now) == []
    with pytest.raises(MarketDataError):
        normalizer.message({"op": "subscribe", "success": False}, now)
    with pytest.raises(MarketDataError):
        normalizer.message({"op": "subscribe"}, now)


def test_bybit_candle_closed_only_and_publication_time(now, subscription):
    row = {
        "start": epoch_ms(now) - 62000,
        "end": epoch_ms(now) - 2001,
        "interval": "1",
        "confirm": False,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100",
        "volume": "5",
        "timestamp": epoch_ms(now) - 30000,
    }
    frame = {"topic": "kline.1.BTCUSDT", "type": "snapshot", "ts": epoch_ms(now), "data": [row]}
    normalizer = BybitNormalizer(subscription)
    assert normalizer.message(frame, now) == []
    row["confirm"] = True
    event = normalizer.message(frame, now)[0]
    assert event.source_ts == now
    row["end"] += 1
    with pytest.raises(MalformedMarketData):
        normalizer.message(frame, now)


def test_wire_provenance_roundtrip(now, subscription):
    event = replace(BybitNormalizer(subscription).message(trade_frame(now), now)[0], source_epoch=3)
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/canonical-market-event.schema.json").read_text()
    )
    jsonschema.validate(event.to_dict(), schema)
    assert event_from_dict(event.to_dict()) == event


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_epoch", True),
        ("source_epoch", -1),
        ("continuity", "complete"),
        ("delivery_kind", "unknown"),
        ("provider_sequence", " "),
    ],
)
def test_invalid_provenance(market_event, field, value):
    with pytest.raises(ValueError):
        replace(market_event, **{field: value})


def setup_selector(now, binance_instrument, market_event):
    clock = [now]
    hub = MarketDataHub(clock=lambda: clock[0])
    sub = Subscription("BTCUSDT", ("quote",))
    hub.register(binance_instrument, sub)
    hub.register(bybit_instrument(binance_instrument), sub)
    selector = ControlledFailover(hub, binance_instrument.instrument_id, "BYBIT:SPOT:BTCUSDT")

    def quote(venue, sequence, price="100"):
        p = Decimal(price)
        return replace(
            market_event,
            event_id=f"{venue}-{sequence}",
            source=f"{venue.lower()}.spot",
            instrument_id=f"{venue}:SPOT:BTCUSDT",
            event_type=EventType.QUOTE,
            sequence=sequence,
            source_ts=clock[0],
            received_ts=clock[0],
            payload=QuotePayload(p, p + Decimal("0.1"), Decimal(1), Decimal(1)),
            continuity="snapshot",
        )

    def observe(event):
        assert hub.ingest(event).accepted
        hub.bus.drain()
        return selector.observe(event)

    return hub, selector, clock, quote, observe


def test_controlled_failover_retains_venue_no_automatic_failback(
    now, binance_instrument, market_event
):
    hub, selector, clock, quote, observe = setup_selector(now, binance_instrument, market_event)
    primary = quote("BINANCE", 1)
    assert observe(primary) == primary
    assert observe(quote("BYBIT", 1)) is None
    hub.gate.disconnected((stream_key(primary),))
    standby = quote("BYBIT", 2)
    assert observe(standby) == standby
    assert selector.selection_epoch == 1
    assert observe(quote("BINANCE", 2)) is None
    assert selector.active == "BYBIT:SPOT:BTCUSDT"
    hub.gate.disconnected((stream_key(standby),))
    assert observe(quote("BINANCE", 3)) is None


@pytest.mark.parametrize(
    "failure", ["divergence", "no_evidence", "expired", "stale_standby", "malformed"]
)
def test_failover_closed_without_trust(now, binance_instrument, market_event, failure):
    hub, selector, clock, quote, observe = setup_selector(now, binance_instrument, market_event)
    primary = quote("BINANCE", 1)
    if failure != "no_evidence":
        observe(primary)
    observe(quote("BYBIT", 1, "110" if failure == "divergence" else "100"))
    hub.gate.disconnected((stream_key(primary),))
    if failure == "expired":
        clock[0] += timedelta(seconds=31)
    if failure == "stale_standby":
        clock[0] += timedelta(seconds=16)
        assert not selector.usable("BYBIT:SPOT:BTCUSDT")
        return
    if failure == "malformed":
        hub.malformed((stream_key(quote("BYBIT", 1)),))
        assert selector.observe(quote("BYBIT", 2)) is None
        return
    assert observe(quote("BYBIT", 2)) is None
    assert selector.active == "BINANCE:SPOT:BTCUSDT"


def test_divergence_latch_does_not_auto_clear(now, binance_instrument, market_event):
    hub, selector, clock, quote, observe = setup_selector(now, binance_instrument, market_event)
    observe(quote("BINANCE", 1))
    assert observe(quote("BYBIT", 1, "200")) is None
    assert observe(quote("BYBIT", 2)) is None
    assert observe(quote("BINANCE", 2)) is None
    assert selector.divergent


def test_economics_and_stream_mismatch(now, binance_instrument):
    hub = MarketDataHub(clock=lambda: now)
    sub = Subscription("BTCUSDT", ("quote",))
    hub.register(binance_instrument, sub)
    standby = replace(bybit_instrument(binance_instrument), base_currency="ETH")
    hub.register(standby, sub)
    with pytest.raises(ValueError):
        ControlledFailover(hub, binance_instrument.instrument_id, standby.instrument_id)
    hub.instruments[standby.instrument_id] = bybit_instrument(binance_instrument)
    hub.register(standby, Subscription("BTCUSDT", ("trade",)))
    with pytest.raises(ValueError):
        ControlledFailover(hub, binance_instrument.instrument_id, standby.instrument_id)


class RecoveryREST(BinanceREST):
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def bars(self, subscription, **kwargs):
        self.calls.append(kwargs)
        return self.rows


def recovery_case(now, binance_instrument, market_event):
    hub = MarketDataHub(clock=lambda: now)
    sub = Subscription("BTCUSDT", ("bar",))
    hub.register(binance_instrument, sub)
    opening = epoch_ms(now) - 182000

    def bar(start, source):
        return replace(
            market_event,
            event_id=f"bar-{start}",
            event_type=EventType.BAR,
            sequence=start,
            source_ts=source,
            source_epoch=2,
            payload=BarPayload(
                *(Decimal(v) for v in ("100", "101", "99", "100", "5")),
                60,
                now - timedelta(milliseconds=epoch_ms(now) - start),
            ),
        )

    first = bar(opening, now - timedelta(seconds=120))
    # Historical accepted cursor seeded for deterministic outage recovery.
    hub.gate.commit(first)
    current = bar(opening + 120000, now)
    assert hub.ingest(current).status.value == "gap"
    missing = [
        opening + 60000,
        "100",
        "101",
        "99",
        "100",
        "5",
        opening + 119999,
        "0",
        1,
        "0",
        "0",
        "0",
    ]
    return hub, sub, current, missing


def test_atomic_backfill_never_masquerades_as_live(now, binance_instrument, market_event):
    hub, sub, current, row = recovery_case(now, binance_instrument, market_event)
    rest = RecoveryREST([row])
    assert hub.recover_bar(current, rest, sub).accepted
    assert hub.bus.drain() == [current]
    assert len(hub.recovered) == 1
    historical = hub.recovered[0]
    assert historical.delivery_kind == "backfill" and historical.source_epoch == 2
    assert not hub.ingest(historical).accepted
    assert hub.gate.streams[stream_key(current)].last_event == current
    assert rest.calls[0]["limit"] == 1


def test_rest_recovery_arrives_after_pending_live_candle(now, binance_instrument, market_event):
    hub, sub, current, row = recovery_case(now, binance_instrument, market_event)
    clock = [now]
    hub.clock = lambda: clock[0]

    class DelayedREST(RecoveryREST):
        def bars(self, subscription, **kwargs):
            clock[0] += timedelta(seconds=1)
            return super().bars(subscription, **kwargs)

    assert hub.recover_bar(current, DelayedREST([row]), sub).accepted
    assert hub.recovered[0].received_ts == now + timedelta(seconds=1)
    assert hub.gate.streams[stream_key(current)].last_event == current


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "wrong_range",
        "bad_price",
        "huge",
        "backpressure",
        "stale_current",
        "wrong_provider",
    ],
)
def test_backfill_failure_leaves_gap_and_cursor(now, binance_instrument, market_event, failure):
    hub, sub, current, row = recovery_case(now, binance_instrument, market_event)
    before = hub.gate.streams[stream_key(current)].last_event
    rows = [row]
    if failure == "missing":
        rows = []
    if failure == "duplicate":
        rows = [row, row]
    if failure == "wrong_range":
        row[0] += 60000
        row[6] += 60000
    if failure == "bad_price":
        row[1] = "NaN"
    if failure == "huge":
        current = replace(current, sequence=current.sequence + 1001 * 60000)
    if failure == "stale_current":
        current = replace(current, received_ts=now - timedelta(seconds=10))
    if failure == "backpressure":
        hub.bus = EventBus(1)
        hub.bus.publish(market_event)
    rest = BybitREST() if failure == "wrong_provider" else RecoveryREST(rows)
    with pytest.raises((ValueError, BackpressureError)):
        hub.recover_bar(current, rest, sub)
    assert hub.gate.streams[stream_key(current)].gap
    assert hub.gate.streams[stream_key(current)].last_event == before
    assert not hub.recovered


class Socket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        value = next(self.frames, None)
        if value is None:
            await asyncio.Event().wait()
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


def test_bybit_reconnect_epochs_uuid_dedup_and_cleanup(now, binance_instrument):
    async def run():
        sub = Subscription("BTCUSDT", ("trade",))
        sockets = [
            Socket(
                [{"op": "subscribe", "success": True}, trade_frame(now, ("uuid-1",)), OSError()]
            ),
            Socket([{"op": "subscribe", "success": True}, trade_frame(now, ("uuid-1", "uuid-2"))]),
        ]
        iterator = iter(sockets)

        async def sleep(_):
            pass

        adapter = BybitWebSocket(
            sub, connect_factory=lambda *a, **k: next(iterator), clock=lambda: now, sleep=sleep
        )
        hub = MarketDataHub(clock=lambda: now)
        hub.register(bybit_instrument(binance_instrument), sub)
        events = []
        async with aclosing(hub.stream(adapter)) as stream:
            async for event in stream:
                events.append(event)
                if len(events) == 2:
                    break
        assert [event.source_epoch for event in events] == [1, 2]
        assert hub.rejected["duplicate"] == 1
        assert all(socket.closed for socket in sockets)
        assert sockets[0].sent[0] == {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}
        assert hub.gate.snapshot(now)[stream_key(events[-1])] == "disconnected"

    asyncio.run(run())


def test_bybit_rest_fixed_public_allowlist_and_bounds():
    rest = BybitREST()
    with pytest.raises(ValueError):
        rest.get("order/create")
    with pytest.raises(ValueError):
        rest.get("kline", {"category": "linear"})
    with pytest.raises(ValueError):
        rest.bars(Subscription("BTCUSDT"), limit=1001)


def test_dual_stream_cancellation_closes_producers(now, binance_instrument):
    async def run():
        sub = Subscription("BTCUSDT", ("quote",))
        hub = MarketDataHub(clock=lambda: now)
        hub.register(binance_instrument, sub)
        hub.register(bybit_instrument(binance_instrument), sub)
        socket = Socket([{"op": "subscribe", "success": True}, quote_frame(now)])
        standby = BybitWebSocket(sub, connect_factory=lambda *a, **k: socket, clock=lambda: now)
        from vision.market_data.adapters.binance import BinanceWebSocket

        primary = BinanceWebSocket(
            sub, connect_factory=lambda *a, **k: Socket([]), clock=lambda: now
        )
        stream = controlled_stream(hub, primary, standby, BinanceREST(), BybitREST())
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stream.aclose()
        assert socket.closed

    asyncio.run(run())


def test_old_epoch_rejected_without_resetting_cursor(now, binance_instrument, market_event):
    hub = MarketDataHub(clock=lambda: now)
    hub.register(binance_instrument, Subscription("BTCUSDT", ("trade",)))
    first = replace(market_event, source_epoch=2)
    assert hub.ingest(first).accepted
    delayed = replace(first, event_id="delayed", sequence=first.sequence + 1, source_epoch=1)
    assert hub.ingest(delayed).reasons == ("source_epoch_regression",)
    assert hub.gate.streams[stream_key(first)].last_event == first


def test_dual_provider_runner_switches_after_primary_disconnect(
    now, binance_instrument, market_event
):
    from vision.market_data.adapters.binance import BinanceNormalizer

    async def run():
        hub, selector, clock, quote, observe = setup_selector(now, binance_instrument, market_event)
        ready = asyncio.Event()
        disconnected = asyncio.Event()
        sub = Subscription("BTCUSDT", ("quote",))

        class Primary:
            subscription = sub
            normalizer = BinanceNormalizer(sub)

            async def events(self):
                self.on_connection(True)
                try:
                    yield quote("BINANCE", 1)
                    await ready.wait()
                    raise MarketDataError("Synthetic primary outage")
                finally:
                    self.on_connection(False)
                    disconnected.set()

        class Standby:
            subscription = sub
            normalizer = BybitNormalizer(sub)
            closed = False

            async def events(self):
                self.on_connection(True)
                try:
                    yield quote("BYBIT", 1)
                    ready.set()
                    await disconnected.wait()
                    yield quote("BYBIT", 2)
                    await asyncio.Event().wait()
                finally:
                    self.on_connection(False)
                    self.closed = True

        standby = Standby()
        events = []
        async with (
            asyncio.timeout(2),
            aclosing(
                controlled_stream(hub, Primary(), standby, BinanceREST(), BybitREST())
            ) as stream,
        ):
            async for event in stream:
                events.append(event)
                if len(events) == 2:
                    break
        assert [event.instrument_id for event in events] == [
            "BINANCE:SPOT:BTCUSDT",
            "BYBIT:SPOT:BTCUSDT",
        ]
        assert hub.failover.selection_epoch == 1
        assert standby.closed

    asyncio.run(run())


def test_bybit_rest_envelope_metadata_and_reverse_history(now, subscription):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return json.dumps(self.payload).encode()

    class Opener:
        def __init__(self):
            self.requests = []
            self.payload = None

        def open(self, request, **kwargs):
            self.requests.append(request)
            return Response(self.payload)

    opener = Opener()
    rest = BybitREST(opener=opener, min_request_interval=0)
    opener.payload = {
        "retCode": 0,
        "result": {
            "category": "spot",
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "priceFilter": {"tickSize": "0.1"},
                    "lotSizeFilter": {"basePrecision": "0.000001"},
                }
            ],
        },
    }
    instrument = rest.instrument("BTCUSDT")
    assert instrument.minimum_quantity == 0 and instrument.lot_size == Decimal("0.000001")
    assert opener.requests[0].full_url.startswith(
        "https://api.bybit.com/v5/market/instruments-info?category=spot"
    )
    assert opener.requests[0].get_method() == "GET"
    assert not any(
        "auth" in key.lower() or "key" in key.lower() for key in opener.requests[0].headers
    )
    start = epoch_ms(now) - 62000
    opener.payload = {
        "retCode": 0,
        "result": {
            "category": "spot",
            "symbol": "BTCUSDT",
            "list": [[str(start), "100", "101", "99", "100", "5", "500"]],
        },
    }
    rows = rest.bars(subscription, start=start, end=start + 59999, limit=1)
    event = BybitNormalizer(subscription).rest_bar(rows[0], now)
    assert event.sequence == start
    assert f"start={start}" in opener.requests[-1].full_url
    opener.payload["retCode"] = 10006
    with pytest.raises(MarketDataError):
        rest.bars(subscription)


def test_bybit_same_venue_gap_recovery(now, binance_instrument, market_event):
    hub, sub, current, row = recovery_case(now, binance_instrument, market_event)
    hub = MarketDataHub(clock=lambda: now)
    hub.register(bybit_instrument(binance_instrument), sub)
    current = replace(current, instrument_id="BYBIT:SPOT:BTCUSDT", source="bybit.spot")
    first = replace(current, sequence=current.sequence - 120000, event_id="first")
    hub.gate.commit(first)
    assert hub.ingest(current).status.value == "gap"

    class Rest(BybitREST):
        def bars(self, subscription, **kwargs):
            return [[str(row[0]), *row[1:6], "0"]]

    assert hub.recover_bar(current, Rest(), sub).accepted
    assert hub.recovered[0].source == "bybit.spot"
