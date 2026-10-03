# Phase 1: public market-data foundation

The current authorized scope is Canonical Events, Market Data Hub, Binance public
REST/WebSocket, and Data Health Gate on `phase/market-data-foundation`. It proceeds
under the user's explicit authorization while acknowledging the missing complete
frozen-spec source. No new full architecture freeze is claimed.

| Component | Implemented |
| --- | --- |
| Contracts | Immutable dataclasses, exact decimals, UTC, stable identities, strict JSON codec |
| Hub | Instrument/subscription registry, admission, counters, bounded quarantine/FIFO |
| REST | Public exchangeInfo, aggregate trades and klines; GET allowlist, pacing, timeout/size limits, bounded retries |
| WebSocket | Public combined streams, bounded queue/frames, protocol ping/pong, idle detection, finite reconnects and cleanup |
| Health | Source/receipt freshness, future skew, duplicate/order/gap rejection, idle/disconnected status |
| CLI | Explicit bounded public-data mode, offline default; no execution capabilities |

## Upstream references

- [Market-data-only endpoints](https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only)
- [REST market data](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)
- [Exchange metadata](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints)
- [WebSocket streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)
- [WebSocket client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)

REST is restricted to `data-api.binance.vision`; WS uses `data-stream.binance.vision:443`.
There are no account/order endpoints, signing, authentication headers, credentials,
user-data streams, or MT5 dependencies. REST redirects are rejected. Regional
restrictions and exchange/network failures can still block public access.

## Identity and timing

Instrument identity is `BINANCE:SPOT:<symbol>`. ExchangeInfo maps base/quote assets,
PRICE_FILTER tick size, and LOT_SIZE increment/minimum quantity. Spot contract size
is one base-asset unit; this is not a risk or broker sizing model.

Trade IDs derive from symbol and aggregate trade ID, consistently across REST/WS.
Quote IDs use update ID. Candle IDs include symbol, interval, and open timestamp.
Receive timestamps record local receipt. Source timestamps use trade time, WS
candle event time, and REST candle close time. Only finalized bars enter the bus.

Spot bookTicker has no exchange timestamp. Its source and receipt time are equal
with `timestamp_basis=receipt`. Quotes are accepted as degraded with
`exchange_timestamp_unavailable`; their source latency cannot be certified.
Consumers must preserve that signal rather than treating them as fully healthy.

Wire v1 gains optional provenance, trade-maker, and candle-open fields. New output
emits provenance; legacy v1 input without it defaults to exchange timing. This
compatibility assumption is never used by the Binance quote adapter.

## Health and continuity

The registry allows 64 streams by default. State is isolated by source, instrument,
event kind, and candle interval. Aggregate trades require consecutive IDs; candles
require consecutive interval-open times. Quote update IDs may skip normally.

Defaults: source age 5 seconds, receive age 5 seconds, future tolerance 2 seconds,
idle timeout 15 seconds. Finalized-bar source age and idle allowance additionally
include one interval. Low-activity symbols may become stale without exchange failure.
These thresholds are configurable through HealthPolicy. Snapshots compute idle
health even when no new event arrives; no background health service is implemented.

Duplicate cache is bounded to 10,000 identities; sequence cursors continue rejecting
old events after cache eviction. Quarantine retains 128 identity/reason summaries,
without raw message bodies. The event bus holds 1024 events by default. Backpressure
raises before committing acceptance/dedup cursors, allowing drain and retry.
The FIFO is an in-process single-consumer boundary, not durable storage. Hub/gate
state is owned by one event loop and is not thread-safe.

Gaps latch the affected stream closed across reconnects. `gate.reset(stream_key)`
explicitly starts a new continuity epoch and clears its cursor/cache; it does not
repair missing data. REST exposes bounded aggregate-trade `fromId` retrieval for
future reconciliation. Automatic gap backfill and full depth reconstruction are
not implemented. REST snapshot mode admits recent trades and the latest closed
bar; older history is not published as healthy live data.

WS has three reconnect attempts by default, exponential delays, 30-second receive
idle detection, bounded queues/frames, and rotation before 24-hour connection expiry.
The library answers server ping frames; additional client pings are disabled.
Cancellation closes the socket without retry. Disconnect/malformed signals apply
only to the affected adapter's streams. Normal shutdown leaves status disconnected.

## Validation and limits

Deterministic synthetic tests cover normalization/schema round-trips, malformed
input, precision, finalization, isolation, freshness/gaps, retry cooldowns, reconnect
exhaustion, cancellation, backpressure, and execution flags. Explicit live public
smoke checks cover metadata/trades/quotes and REST/WS closed candles outside CI.

State reduction, analysis/strategies, other providers, Forex/Metals ingestion,
database persistence, paper fills, and live/MT5 execution remain unimplemented.
The missing full frozen spec remains a provenance/alignment limitation, not an
authentication or coding blocker for this authorized scope.
