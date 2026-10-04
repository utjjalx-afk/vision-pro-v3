"""Opt-in bounded public market-data runner; no account or execution capabilities."""

import asyncio
import json
import math
import sys
from contextlib import aclosing

from vision.market_data.adapters.binance import BinanceREST, BinanceWebSocket, Subscription
from vision.market_data.adapters.bybit import BybitNormalizer, BybitREST, BybitWebSocket, topics
from vision.market_data.failover import controlled_stream
from vision.market_data.hub import MarketDataHub
from vision.market_data.instruments import refresh_public_spec


async def _websocket(hub: MarketDataHub, iterator, args) -> int:
    count = 0
    try:
        async with asyncio.timeout(args.duration), aclosing(iterator) as iterator:
            async for event in iterator:
                print(json.dumps(event.to_dict(), sort_keys=True), flush=True)
                count += 1
                if count >= args.max_events:
                    break
    except TimeoutError:
        pass
    return count


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
    provider = getattr(args, "provider", "binance")
    if provider != "binance":
        topics(subscription)
    if provider == "failover" and (args.transport != "ws" or "quote" not in kinds):
        raise ValueError("Controlled failover requires WebSocket quotes and matching streams")
    if provider == "bybit" and args.transport == "rest" and kinds != ("bar",):
        raise ValueError("Bybit REST snapshot currently supports closed bars only")
    rest = BybitREST() if provider == "bybit" else BinanceREST()
    hub = MarketDataHub()
    emitted = 0
    if args.transport == "rest":
        if provider == "bybit":
            record = refresh_public_spec(hub.registry, rest, subscription.symbol)
            hub.register(record.spec, subscription, record=record)
            normalizer = BybitNormalizer(subscription)
            events = [
                normalizer.rest_bar(row, hub.clock()) for row in rest.bars(subscription, limit=2)
            ]
            closed = [event for event in events if event is not None]
            if closed:
                hub.ingest(max(closed, key=lambda event: event.sequence))
            events = hub.bus.drain()
        else:
            events = hub.snapshot(rest, subscription, limit=args.max_events)
        for event in events[: args.max_events]:
            print(json.dumps(event.to_dict(), sort_keys=True), flush=True)
            emitted += 1
    else:
        record = refresh_public_spec(hub.registry, rest, subscription.symbol)
        hub.register(record.spec, subscription, record=record)
        adapter = (
            BybitWebSocket(subscription) if provider == "bybit" else BinanceWebSocket(subscription)
        )
        iterator = hub.stream(adapter, rest=rest)
        if provider == "failover":
            standby_rest = BybitREST()
            standby_record = refresh_public_spec(hub.registry, standby_rest, subscription.symbol)
            hub.register(standby_record.spec, subscription, record=standby_record)
            iterator = controlled_stream(
                hub, adapter, BybitWebSocket(subscription), rest, standby_rest
            )
        emitted = asyncio.run(_websocket(hub, iterator, args))
    print(json.dumps({"phase": "phase-7", "health": hub.status()}, sort_keys=True), file=sys.stderr)
    return 0 if emitted else 3
