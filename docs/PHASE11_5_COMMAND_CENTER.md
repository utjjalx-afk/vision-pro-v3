# Phase 11.5 — Vision Pro V3 Command Center

Single authenticated, read-only cockpit for analysis, provenance, agent lanes, risk,
broker snapshots and verified research history. Branch `phase/command-center` starts
from Phase 11. Phase 12 remains separate. Phase 11 acceptance remains pending.

## Run on Windows / Linux

From the repository root, install `python -m pip install -c requirements-dashboard.lock -e ".[dev,dashboard]"`.
In `apps/dashboard`, use Node 24: `npm ci`, `npm test`, `npm run build`.
Then from the repository root:

```sh
python -m vision.apps.dashboard.server --token-file data/private/viewer.token --feed binance
```

Open `http://127.0.0.1:8787`. Read the private viewer token file locally and enter
it in the password field. It is never stored in local storage or passed in a URL.
Only display preferences persist. The server binds loopback and issues a one-hour
HttpOnly/SameSite=Strict session. Tokens are private files; apply local OS permissions
for the operator's account. `--feed off` is the offline default. `--symbol` configures
one Binance Spot instrument. The six supported timeframes are subscribed at startup.
Bybit/OANDA cards remain unavailable unless a future owning runtime is integrated.

For a running Phase 11 bridge, add `--bridge-token-file <private-file>` and
`--bridge-port 8765`. This calls only its existing `snapshot` reader. It cannot arm,
execute, close or send an order. Broker identity, currencies, exact symbol mapping,
raw timestamps and future/stale quote failures remain visible. Broker bars are not
available through that bridge and are not replaced with crypto/Yahoo candles.

Use `--journal-export <verified-export.json>` to attach an existing research export.
Hash-chain and semantic replay must pass before any failures, outcomes, backtests,
strategies or paper checkpoints are displayed. No named historical failures are
seeded without records. Macro/narrative remain unavailable without verified context.

## Architecture and truth

Existing MarketDataHub → admitted canonical events → bounded dashboard projection →
FastAPI read DTOs / 250ms WebSocket batches → React/TypeScript → Lightweight Charts.
Initial snapshot is REST. Socket sequence gaps require a full resnapshot; every 5s
the server sends a full refresh. Browser transport silence overrides feed health.
Closed canonical bars update charts; REST backfill is labeled historical and cannot
make a feed healthy. Initial history is 512 bars; dynamic retention is bounded. Older 512-bar windows
load on demand through an authenticated historical endpoint and remain labeled HISTORICAL.
Recursive EMA/Wilder/MACD state survives display-window eviction. Partial UTC-session
VWAP stays unavailable until a verified midnight opening is present.
Nested object and keyed-array patches avoid retransmitting unchanged chart/flow history.

Footprint uses exact Decimal price buckets at 100 verified provider ticks per bucket, base
quantity and the provider's buyer-maker flag. Bid is aggressor sell, ask aggressor
buy. Unknown aggressor side is counted separately. Per-candle totals, delta, delta%,
POC (lower-price tie), horizontal imbalance and CVD are deterministic. CVD restarts
on provider/instrument/epoch changes and is labeled since-epoch, not global volume.
64 footprint candles and 512 levels/candle are retained; 200 latest trades exposed.
Level-one OFI is based on changes to canonical top-of-book quotes. No synthetic
funding, OI, liquidations, global order flow, FX depth or footprint is displayed.

Binance depth uses a separately allowlisted public endpoint and bounded socket queue.
Snapshots are non-authoritative until a delta bridges lastUpdateId; U/u gaps erase
the book and require resnapshot. Stale depth suppresses metrics. Top-5/10 signed
imbalance, spread, midpoint and size-weighted microprice are provider-local descriptors.

Indicators are fresh math: seeded EMA 20/50/100/200, SMA20, UTC-day typical-price VWAP,
population BB20±2σ, Wilder RSI14/ATR14/ADX14, MACD12/26/9 and Stoch14/3/3. Missing warm-up
stays null; price-count volume cannot produce VWAP. Independent analytical ramp/flat
golden vectors cover finite output and chained warm-up recovery. No Vardhan math copied.

Agent topology invokes existing assess_all and reliability-gated synthesize using the
hub context. No attached prospective outcomes means unproven contributions and WAIT.
The risk panel invokes HardRiskGovernor with unavailable risk context and displays
its actual veto, never inventing account baselines/exposures. Execution is DISARMED;
there are no live controls or mutation API routes. Viewer is the only active role;
operator/admin control permissions remain unimplemented and unavailable.

## Acceptance checklist

Implementation and local tests do not constitute complete operational acceptance.
Required proof: live canonical stream/history continuity; real lane receipts;
deterministic flow/depth replay; precision/freshness failures; broker currency and
portfolio equality; verified journal lineage; lifecycle/reconnect tests; mobile and
keyboard access; Windows/Linux/Docker CI; no credentials; no execution controls.
Unavailable integrations are explicitly visible and must not be described as connected.
Phase 11's +3h future timestamp block remains unchanged. Live eligibility remains NO.

References: [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/),
[Lightweight Charts API](https://tradingview.github.io/lightweight-charts/docs/api),
[Binance local book procedure](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams#how-to-manage-a-local-order-book-correctly).


## Docker

Build with `docker compose -f docker-compose.dashboard.yml build`.
Create `data/dashboard-container` and grant container UID 10001 write permission
on that directory on Linux; then `docker compose -f docker-compose.dashboard.yml up`.
Read the generated private token from the mounted local directory. Root filesystem
is read-only, privileges are dropped and the published port is loopback only.
Public analysis streaming is opt-in through the server command; it never enables
broker execution. The existing deterministic Docker suite installs the locked API extras.

## Observed demo run

The read-only native USD demo snapshot reports future source timestamps on all four
MT5 symbols, so broker health stays BLOCKED. Some admitted public events fail the
stricter lane-context receipt/source timestamp invariant; the exact validation
reason is exposed, lane scores stay unavailable and synthesis does not generate
a trade intent. No timestamp is shifted or replaced to pass these gates.
Operational acceptance remains pending genuine lane receipts and verified journal
lineage. This branch does not alter Phase 11 acceptance or Phase 12 execution.
