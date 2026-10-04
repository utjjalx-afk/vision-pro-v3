# Phase 10 — Forex and metals market data

Phase 10 adds an authorized OANDA v20 **read-only GET** adapter for `EUR_USD`,
`XAU_USD`, and `XAG_USD`, mapped explicitly to `forex:EUR/USD`, `metals:XAU/USD`,
and `metals:XAG/USD`. It does not extend strategy compilation or paper/live
execution to these assets. Main and prior phase branches remain unchanged.

## Provider and credentials

OANDA data requires an authorized account/token. Only runtime environment
variables `OANDA_API_TOKEN` and `OANDA_ACCOUNT_ID` supply credentials. No credentials,
account response, private account URL, balance, positions or execution endpoint is
persisted/exported. Practice/live data hosts are fixed; redirects are rejected.
Requests are paced, timeout/response bounds are enforced, and errors omit remote
bodies, credentials and request URLs. There are no order methods or automatic
retries on authentication failures. No account token was used for development.

Available endpoints: requested instrument metadata, pricing, and bid/ask candles.
The API's [pricing contract](https://developer.oanda.com/rest-live-v20/pricing-ep/)
requires authorization; this is not presented as an anonymous public feed.
Metal availability is account/division-dependent. Absence or non-tradeable pricing
is rejected rather than replaced with a fabricated quote.

## Canonical price semantics

Quotes preserve best bid/ask and published L1 liquidity, with provider timestamp,
environment-qualified source, source epoch and receipt time. RFC3339 offsets are
normalized to UTC; exact provider timestamp and nanosecond sequence remain in
provenance. Python datetime floors nanoseconds to microseconds explicitly.
Snapshot quotes do not claim every-tick continuity.

Fixed UTC intervals supported: M1, M5, M15 and H1. Daily/monthly/DST-aligned candle
durations are unsupported. Bid and ask candles have separate event identities
and stream cursors. `BarPayload.price_basis` declares `bid` or `ask` and
`volume_basis=price_count`; OANDA volume counts prices, not traded base volume,
per its [candle definition](https://developer.oanda.com/rest-live-v20/instrument-df/).
Existing crypto wire output and hashes remain unchanged when these optional
fields are absent. No midpoint candle or guessed spread is synthesized.

Only finalized candles are normalized; duplicate, unordered, off-grid, crossed,
malformed and future finalized candles are rejected. Historical candles are
`backfill`; the explicit snapshot command admits only the latest finalized pair
through the existing bounded bus and freshness gate. It is a bounded snapshot,
not a new autonomous daemon or streaming implementation.

## Metadata and execution economics

`MarketMetadata` records public projected instrument fields, canonical mapping,
provider/environment, observed time and immutable digest/revision. Pip size is
exactly `10^pipLocation`. Display quantum is `10^-displayPrecision` and is **not**
an inferred execution tick. `tradeUnitsPrecision` and `minimumTradeSize` are
preserved, per OANDA's [instrument definitions](https://developer.oanda.com/rest-live-v20/primitives-df/).

The mandatory contract-size field in the execution InstrumentSpec cannot be
populated safely from price metadata. The adapter therefore registers market
metadata separately and creates no InstrumentRecord automatically. An independently
verified complete record can be supplied through `register_market(record=...)`
only with matching identity, canonical mapping and metadata provenance. Metadata
changes invalidate an older current execution record. There is no XAU 100-unit
fallback, margin inference or broker sizing. Without verified economics,
`execution_spec_ready=false` and existing portfolio economics fail closed.

## Calendar and health

Calendars are immutable structured artifacts with explicit timezone, weekly
half-open local-minute windows, dated holiday/early-close overrides, bounded UTC
coverage, reference, exact timezone rules version and an explicit exception
verification attestation. Windows crossing midnight must be split by date.
Overlapping windows, duplicate dates, unverified exceptions and timezone-version
mismatches are rejected. Packaged `tzdata==2026.5` is used on Windows/Linux/Docker,
rather than relying on differing host timezone databases. Supported calendar
zones are America/New_York, Europe/London and UTC.

The [OANDA UK published schedule](https://www.oanda.com/uk-en/trading/hours-of-operation/)
provides FX and metals session boundaries. It is a source reference, **not** an
automatic global calendar for all accounts. An operator must verify the actual
account/division schedule and all exceptions over the supplied coverage period.
Fixtures use explicitly synthetic schedules and holiday attestations. They are
not deployment calendars or authenticated market history.

While closed, stream status is `market_closed`, even with no quotes, old quotes,
disconnected transport or a retained history gap. Raw disconnected/gap state is
preserved. At reopening, stale quotes remain stale, disconnect remains disconnected,
and fresh data/history is required. Outside coverage, status is `calendar_unknown`.
Neither state admits new live events or permits downstream intelligence/execution.
Existing cached prices remain historical; closure never makes a price fresh.
Crypto streams retain their existing always-open health behavior.

Continuity skips only scheduled closed candle opens under the exact calendar.
Missing **open-session** candles latch GAP, even if the missing bar was before a
weekend. Bounded same-source backfill validates every expected open, side, epoch,
interval and timestamp before accepting the pending fresh candle. Historical
bars remain outside the live bus; publication backpressure preserves the gap.
Calendar revision changes require a new hub/replay epoch rather than silently
reinterpreting admitted history.

## Failover and conversion groundwork

`FXSourceSelector` is a framework for future **independent, registered** providers.
It requires matching canonical pairs, recent synchronized quote agreement,
healthy streams and open calendar coverage. Divergence latches; epoch changes
invalidate prior evidence; expiry and closed/unknown primary sessions cannot
trigger source migration. Selected quotes keep original source/instrument IDs
and a separate selection epoch. It grants no spec/position/execution migration.

Only OANDA is implemented in this phase. OANDA practice/live are not independent
sources and cannot be paired. Dukascopy transport and real dual-provider failover
remain unavailable; synthetic independent-provider stubs test the framework.
`FXConversionProvider` defines a future conversion boundary. The default
`NoFXConversion` always returns `FX_CONVERSION_REQUIRED`; no USD/USDT peg, reverse
rate, account home conversion or portfolio equity is invented.

## Commands and replay

Offline synthetic replay, with no token/network:

```bash
python -m vision forex-replay tests/fixtures/forex/oanda_replay.json
```

Explicit authorized snapshot, after setting runtime credentials and creating a
verified calendar JSON matching `SessionCalendar` fields:

```bash
python -m vision forex-data --symbol EUR_USD --environment practice \
  --granularity M1 --calendar verified-calendar.json --source-epoch 1
```

On Windows use the same command on one line. Source epoch must be a positive
integer and must increase across caller-managed reconnects. The adapter does not
infer durable epochs or own a background reconnect loop. Closed/unknown session
snapshots perform no network requests. CLI errors are safe categories and global
execution flags are checked before file/network access.

Offline replay stores projected/synthetic metadata, calendar, explicit clock,
epochs and bounded actions: quote, candles, disconnect, reconnect, recover and
status. It returns exact canonical events, decisions, calendar/spec readiness,
and input/result hashes. Complete payloads and calendars reconstruct the trace
without a provider. Replay hashes establish local consistency, not authenticated
market truth or independent strategy evidence.

Tests cover provider contracts, side/volume identity, strict schema round-trips,
nanoseconds/offsets, weekend/daily breaks, both DST transitions, holidays/expiry,
missing bars, source epochs, atomic recovery/backpressure, transport redaction,
execution guards, synthetic failover/divergence and offline replay determinism.
