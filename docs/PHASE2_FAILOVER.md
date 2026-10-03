# Phase 2: public Spot redundancy

Phase 2 extends the Phase-1 data layer. Live execution, MT5, paper fills and agents
are unimplemented. All four enabling flags reject true or ambiguous values before
network access. Default startup remains offline.

## Provider contracts

Bybit uses fixed public V5 Spot endpoints for instrument metadata and closed REST
candles, plus public trade, L1 snapshot and confirmed candle WebSocket topics.
Subscription rejection stops the stream. Application pings run every 20 seconds;
control traffic cannot conceal an idle market feed. Open candle updates count as
transport activity but never become finalized canonical bars. Reconnect budgets,
timeouts, response sizes and queues are bounded. No regional routing workaround,
authenticated request or trading endpoint is implemented.

Bybit trade IDs may be UUIDs or numeric strings. Their exchange cross-sequences
can repeat, so `sequence` is a local delivery ordinal and `continuity=unverified`.
The stable event ID deduplicates replays across reconnects. No claim of complete
trade history is made. L1 is a complete snapshot; publication timestamps order it,
including unchanged idle snapshots and service update-ID resets. Full depth and
delta reconstruction are outside this phase.

Instrument IDs remain `BINANCE:SPOT:<symbol>` and `BYBIT:SPOT:<symbol>` throughout
selection. Tick sizes may differ. Bybit `lot_size` records base precision;
`minimum_quantity=0` represents no current quantity constraint in this data-only
stub, since Bybit deprecated minOrderQty in favor of amount constraints. This
InstrumentSpec is not an execution sizing contract.

Sources: [Bybit trades](https://bybit-exchange.github.io/docs/v5/websocket/public/trade),
[L1 snapshots](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook),
[candles](https://bybit-exchange.github.io/docs/v5/websocket/public/kline),
[connection lifecycle](https://bybit-exchange.github.io/docs/v5/ws/connect),
[instrument metadata](https://bybit-exchange.github.io/docs/v5/market/instrument),
[REST candles](https://bybit-exchange.github.io/docs/v5/market/kline).
Implementation is original; no SDK source is vendored.

## Controlled selection

`--provider failover` starts both public providers with matching subscriptions.
The controller checks identical Spot base/quote currencies, asset class, symbol
and contract size. A quote subscription is mandatory. Unsupported Bybit intervals
(including Binance 1s, 8h and 3d) fail before networking.

Binance starts as primary. Every requested stream must have an accepted event,
no gap, no disconnect, no unresolved rejection, and a current receipt before a
source is usable. Candles add their duration to the idle allowance. Initial bar
subscriptions therefore warm up until both feeds publish a closed candle.

Quotes received within two seconds of one another and within five seconds of now
provide comparison evidence. Relative midpoint divergence above 50 basis points
latches all selected output closed until an explicit restart/review. Agreement
evidence expires after 30 seconds. A failed primary switches to Bybit only while
standby streams are usable and agreement evidence remains fresh. Missing evidence,
expired evidence, stale standby, malformed data, incompatible metadata and gaps
produce no selected output. Once switched, there is no automatic failback.

The bounded transition journal records the selection epoch and actual venue IDs.
Canonical `source_epoch` records each successful source connection, including
reconnects. Selection never combines exchange sequences or renames Bybit data as
Binance. An epoch is provenance, not evidence that lost trades were recovered.
Standby events are health observations; only selected events go to CLI stdout.

## Candle recovery and reconnects

Binance trade and both providers' candle cursors survive reconnects. Binance trade
gaps remain latched; this phase does not backfill trades. Bybit trades retain
explicitly unverified continuity. Quote snapshots restore current state without
claiming historical updates. Candle gaps trigger same-venue REST recovery in WS
mode, limited to 1000 missing candles in one request. Longer gaps stay closed.

Recovery rejects partial, duplicate, misaligned, open, malformed or wrong-venue
ranges. It sorts provider rows and requires every expected open time. Only after
the complete range validates, the pending live candle remains fresh and the bus
has space does it commit the repaired cursor atomically. Historical candles retain
their historical timestamps and `delivery_kind=backfill` in a separate bounded
in-memory recovery journal (`hub.recovered`); they never masquerade as fresh live
bus events. The pending current candle publishes once. Cancellation cannot mutate
the gate from a background REST thread. There is no durable history store yet.

## Run and validate

```bash
python -m vision market-data --provider bybit --streams trade,quote --duration 30
python -m vision market-data --provider bybit --transport rest --streams bar
python -m vision market-data --provider failover --streams trade,quote --duration 30
python -m pytest
docker build -f docker/Dockerfile.test -t vision-pro-v3-tests .
docker run --rm --network none vision-pro-v3-tests
```

Windows/Linux Python 3.11/3.12 CI runs the full deterministic suite, lint, schema
tests, package install and platform scripts. Docker CI runs that same fixture suite
offline plus container guards/diagnostics. Public exchange smoke checks are
explicit local checks outside CI; regional blocks are reported rather than bypassed.
