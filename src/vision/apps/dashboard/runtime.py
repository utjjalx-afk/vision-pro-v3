"""Opt-in public analysis feed; broker integration uses the read-only bridge only."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from urllib.request import Request, build_opener

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from vision.market_data.adapters.binance import (
    REST_BASE,
    WS_BASE,
    BinanceNormalizer,
    BinanceREST,
    BinanceWebSocket,
    NoRedirect,
    Subscription,
)
from vision.market_data.instruments import refresh_public_spec


async def public_feed(state, symbol, intervals):
    async def stream(interval, primary):
        subscription = Subscription(
            symbol, ("trade", "quote", "bar") if primary else ("bar",), interval
        )
        rest = BinanceREST()
        try:
            record = await asyncio.to_thread(refresh_public_spec, state.hub.registry, rest, symbol)
            state.hub.register(record.spec, subscription, record=record)
            rows = await asyncio.to_thread(rest.bars, subscription, limit=512)
            normalizer = BinanceNormalizer(subscription)
            history = [normalizer.rest_bar(row, state.clock()) for row in rows]
            state.history([replace(e, delivery_kind="backfill") for e in history if e])
            state.connections["binance.spot"] = {"state": "CONNECTING", "symbol": symbol}
            recovered_ids = set()
            async for event in state.hub.stream(BinanceWebSocket(subscription), rest=rest):
                recovery = [
                    e
                    for e in state.hub.recovered
                    if (e.event_id, e.source_epoch) not in recovered_ids
                ]
                if recovery:
                    state.history(recovery)
                    recovered_ids = {(e.event_id, e.source_epoch) for e in state.hub.recovered}
                state.admitted(event)
                state.connections["binance.spot"] = {
                    "state": "RECEIVING",
                    "symbol": symbol,
                    "last_received": event.received_ts.isoformat(),
                }
        except (ValueError, RuntimeError, OSError):
            state.connections["binance.spot"] = {
                "state": "UNAVAILABLE",
                "reason": "PUBLIC_FEED_FAILED",
            }

    await asyncio.gather(*(stream(interval, i == 0) for i, interval in enumerate(intervals)))


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
    for attempt in range(5):
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
