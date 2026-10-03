"""Opt-in bounded public market-data runner; no account or execution capabilities."""

import asyncio
import json
import math
import sys
from contextlib import aclosing

from vision.market_data.adapters.binance import BinanceREST, BinanceWebSocket, Subscription
from vision.market_data.hub import MarketDataHub


async def _websocket(hub: MarketDataHub, adapter: BinanceWebSocket, args) -> None:
    count = 0
    try:
        async with asyncio.timeout(args.duration), aclosing(hub.stream(adapter)) as iterator:
            async for event in iterator:
                print(json.dumps(event.to_dict(), sort_keys=True), flush=True)
                count += 1
                if count >= args.max_events:
                    break
    except TimeoutError:
        pass


def run_market_data(args) -> int:
    if not math.isfinite(args.duration) or not 0 < args.duration <= 3600:
        raise ValueError("Duration must be between 0 and 3600 seconds")
    if not 1 <= args.max_events <= 1000:
        raise ValueError("Max events must be between 1 and 1000")
    kinds = (
        tuple(args.streams.split(","))
        if args.streams
        else (("trade",) if args.transport == "rest" else ("trade", "quote", "bar"))
    )
    subscription = Subscription(args.symbol, kinds, args.interval)
    rest = BinanceREST()
    hub = MarketDataHub()
    if args.transport == "rest":
        for event in hub.snapshot(rest, subscription, limit=args.max_events)[: args.max_events]:
            print(json.dumps(event.to_dict(), sort_keys=True), flush=True)
    else:
        hub.register(rest.instrument(subscription.symbol), subscription)
        adapter = BinanceWebSocket(subscription)
        asyncio.run(_websocket(hub, adapter, args))
    print(json.dumps({"phase": "phase-1", "health": hub.status()}, sort_keys=True), file=sys.stderr)
    return 0 if hub.accepted else 3
