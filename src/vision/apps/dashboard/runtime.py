"""Opt-in public analysis feed; broker integration uses the read-only bridge only."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from urllib.request import Request, build_opener

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from vision.market_data.adapters.binance import (
    INTERVALS,
    REST_BASE,
    WS_BASE,
    BinanceNormalizer,
    BinanceREST,
    MalformedMarketData,
    NoRedirect,
    Subscription,
)
from vision.market_data.instruments import refresh_public_spec


def start_public_epoch(state, symbol, keys):
    """A reconnect starts a new window; missing trades are never claimed recovered."""
    source = "binance.spot"
    state.hub.epochs[source] = state.hub.epochs.get(source, 0) + 1
    epoch = state.hub.epochs[source]
    for key in keys:
        state.hub.gate.reset(key)
    instrument = f"BINANCE:SPOT:{symbol}"
    state.events.pop(instrument, None)
    state.quotes.pop(instrument, None)
    for store in (state.flow, state.flow_faults, state.ofi, state.previous_quotes):
        for key in tuple(store):
            if instrument in key:
                del store[key]
    state.timeline.appendleft(
        {
            "id": f"public-epoch-{epoch}",
            "kind": "SOURCE_WINDOW_RESET",
            "source": source,
            "at": state.clock().isoformat(),
        }
    )
    state.version += 1
    return epoch


async def public_feed(state, symbol, intervals):
    subscriptions = [
        Subscription(symbol, ("trade", "quote", "bar") if i == 0 else ("bar",), interval)
        for i, interval in enumerate(intervals)
    ]
    routes = {stream: BinanceNormalizer(sub) for sub in subscriptions for stream in sub.streams}
    keys = tuple(
        f"binance.spot:BINANCE:SPOT:{symbol}:{kind}"
        + (f":{INTERVALS[sub.interval]}" if kind == "bar" else "")
        for sub in subscriptions
        for kind in sub.kinds
    )
    failures = 0
    while True:
        try:
            state.connections["binance.spot"] = {
                "state": "WARMING_UP",
                "symbol": symbol,
                "reason": "LOADING_REST_HISTORY",
            }
            rest = BinanceREST()
            record = await asyncio.to_thread(refresh_public_spec, state.hub.registry, rest, symbol)
            for sub in subscriptions:
                state.hub.register(record.spec, sub, record=record)
            # Load REST history before opening the live socket. Otherwise the six
            # slow HTTP calls queue old trades and repeatedly trip freshness gates.
            history = []
            for sub in subscriptions:
                rows = await asyncio.to_thread(rest.bars, sub, limit=512)
                normalizer = BinanceNormalizer(sub)
                history.extend(normalizer.rest_bar(row, state.clock()) for row in rows)
            async with connect(
                f"{WS_BASE}/stream?streams={'/'.join(routes)}",
                proxy=None,
                max_queue=128,
                max_size=1000000,
                open_timeout=10,
                close_timeout=5,
            ) as ws:
                epoch = start_public_epoch(state, symbol, keys)
                state.connections["binance.spot"] = {
                    "state": "WARMING_UP",
                    "symbol": symbol,
                    "source_epoch": epoch,
                    "reason": "NEW_CONTINUITY_WINDOW",
                }
                state.history(
                    [replace(e, delivery_kind="backfill", source_epoch=epoch) for e in history if e]
                )
                failures = 0
                async with asyncio.timeout(23 * 3600):
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), 30)
                        try:
                            envelope = json.loads(raw)
                            normalizer = routes[envelope["stream"]]
                            event = normalizer.message(envelope, state.clock())
                        except (KeyError, TypeError, ValueError, MalformedMarketData):
                            state.hub.malformed(keys)
                            continue
                        if event is None:
                            continue
                        event = replace(event, source_epoch=epoch)
                        decision = state.hub.ingest(event)
                        for published in state.hub.bus.drain():
                            state.admitted(published)
                        if decision.status.value == "gap":
                            raise ValueError("Public continuity window must restart")
                        state.connections["binance.spot"] = {
                            "state": "RECEIVING",
                            "symbol": symbol,
                            "source_epoch": epoch,
                            "last_received": event.received_ts.isoformat(),
                            "rejected": dict(state.hub.rejected),
                        }
        except (
            ValueError,
            RuntimeError,
            OSError,
            TimeoutError,
            ConnectionClosed,
            InvalidHandshake,
        ):
            state.hub.gate.disconnected(keys)
            failures += 1
            delay = min(2 ** min(failures, 5), 30)
            state.connections["binance.spot"] = {
                "state": "RECONNECTING",
                "symbol": symbol,
                "reason": "PUBLIC_FEED_OR_CONTINUITY_FAILURE",
                "retry_seconds": delay,
            }
            await asyncio.sleep(delay)


def depth_snapshot(symbol):
    # Dedicated fixed public depth endpoint; never adds orders to the existing adapter.
    opener = build_opener(NoRedirect())
    req = Request(f"{REST_BASE}/api/v3/depth?symbol={symbol}&limit=5000")
    with opener.open(req, timeout=10) as response:
        raw = response.read(2000001)
    if len(raw) > 2000000:
        raise ValueError("Depth snapshot exceeds limit")
    return json.loads(raw)


async def depth_feed(state, symbol):
    instrument = f"BINANCE:SPOT:{symbol}"
    book = state.book(instrument)
    attempt = 0
    while True:
        try:
            async with connect(
                f"{WS_BASE}/ws/{symbol.lower()}@depth@100ms",
                proxy=None,
                max_queue=128,
                max_size=1000000,
                open_timeout=10,
            ) as ws:
                # Socket queues deltas while REST snapshot is in flight, with a finite bound.
                book.snapshot_load(await asyncio.to_thread(depth_snapshot, symbol))
                async with asyncio.timeout(23 * 3600):
                    while True:
                        value = json.loads(await asyncio.wait_for(ws.recv(), 15))
                        if value.get("s") != symbol or value.get("e") != "depthUpdate":
                            raise ValueError("Wrong depth identity")
                        if book.delta(value, datetime.now(UTC)):
                            state.version += 1
                        elif book.last_id is None:
                            raise ValueError("Depth recovery required")
        except (
            OSError,
            ValueError,
            RuntimeError,
            TimeoutError,
            ConnectionClosed,
            InvalidHandshake,
        ):
            book.invalidate("DEPTH_RECONNECT_REQUIRED")
            attempt = min(attempt + 1, 4)
            await asyncio.sleep(min(2**attempt, 16))


async def broker_feed(state, client):
    while True:
        try:
            state.broker = await asyncio.to_thread(client.snapshot)
            projection = state.broker_projection()
            state.connections["mt5"] = {
                "state": projection["state"],
                "reasons": projection.get("reasons", []),
            }
        except (RuntimeError, ValueError):
            state.connections["mt5"] = {"state": "UNAVAILABLE", "reason": "BRIDGE_UNAVAILABLE"}
        await asyncio.sleep(2)


async def native_broker_feed(state, monitor):
    while True:
        try:
            state.broker = await asyncio.to_thread(monitor.reader.snapshot)
            projection = state.broker_projection()
            state.connections["mt5"] = {
                "state": projection["state"],
                "reasons": projection.get("reasons", []),
            }
        except (ValueError, RuntimeError, OSError):
            state.broker = None
            state.connections["mt5"] = {
                "state": "UNAVAILABLE",
                "reason": "PINNED_DEMO_SNAPSHOT_UNAVAILABLE",
            }
        await asyncio.sleep(2)
