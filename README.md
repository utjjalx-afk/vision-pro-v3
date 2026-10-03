# Vision Pro V3

An Apache-2.0 foundation for multi-asset market intelligence and governed research.
**Phase 3 adds a provenance-bearing instrument registry and read-only portfolio state.**
No account API, order endpoint, MT5 bridge, strategy, paper fills, dashboard, or API
server is included. Enabling live trading, MT5, paper trading or agents fails before network I/O.

```text
Binance / Bybit public REST/WS -> Canonical normalization -> Data Health Gate -> Bounded Event Bus
```

The longer-term design adds analysis lanes, intents, risk
vetoes, research gates, and guarded execution. See the [architecture baseline](docs/architecture/ARCHITECTURE_SPEC_v1.md)
for provenance and its missing full-spec limitation. The old Vision Pro remains separate.

Phase 2 public Binance/Bybit ingestion and controlled failover remain available.
[Phase 3 details](docs/PHASE3_PORTFOLIO.md) cover explicit quantity units and spec
revisions, immutable positions/marks, atomic snapshots, Decimal PnL/exposure,
readiness states, currency safety and portable deterministic replay. Position
inputs are supplied explicitly; no account connector or synthetic fills are added.

## Install and check

Python 3.11/3.12, Windows PowerShell:

```powershell
.\scripts\setup-windows.ps1
.\scripts\run-windows.ps1
.\scripts\check-windows.ps1
```

Linux:

```bash
./scripts/setup-linux.sh
./scripts/run-linux.sh
./scripts/check-linux.sh
```

Setup creates `.venv` and installs the package plus development tools. Direct
equivalent in an activated virtual environment:

```bash
python -m pip install -e '.[dev]'
python -m vision
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

`python -m vision` stays offline and exits with a diagnostic. Process environment
configures execution guards; `.env.example` is not automatically loaded by Python.
No credentials are needed or read. WebSocket uses `websockets`; REST uses the Python
standard library with TLS verification enabled.

## Inspect public instrument specs

```bash
python -m vision instrument-specs --provider both --symbol BTCUSDT
python -m vision instrument-specs --provider both --symbol ETHUSDT
```

These bounded calls print current venue specs, canonical mapping, explicit units,
metadata provenance hashes and revisions. There is no fallback contract multiplier.
XAU/EURUSD/XAG mappings require separately supplied complete venue specifications.

## Read public market data

Run in the virtual environment, or pass these arguments to the platform run script:

```bash
python -m vision market-data --symbol BTCUSDT --streams trade,quote --max-events 10 --duration 30
python -m vision market-data --provider bybit --streams trade,quote --duration 30
python -m vision market-data --provider failover --streams trade,quote --duration 30
python -m vision market-data --provider bybit --transport rest --streams bar
python -m vision market-data --transport rest --symbol BTCUSDT --max-events 10
python -m vision market-data --streams bar --interval 1s --max-events 2 --duration 15
python -m vision market-data --transport rest --streams bar --interval 1m --max-events 1
```

Accepted events are JSON lines on stdout; health summary is on stderr. WS mode
stops at the event or duration limit. Final status is disconnected because the
socket is closed. REST returns a single snapshot. Exit 3 means unavailable data,
exhausted reconnects, or no selected output events; exit 2 means invalid configuration.

WS streams: Binance aggregate trades / Bybit public trades, best bid/ask snapshots,
and closed UTC candles. Binance REST: trades and latest closed candles. Bybit REST:
latest closed candles. Default WS selection includes all three kinds.
Fixed-duration intervals are supported; calendar-month candles are excluded.

## Data quality

Exact decimals, UTC timestamps, stable IDs, instrument metadata, typed payloads,
and strict JSON decoding feed the health gate. It drops stale/future data,
duplicates, out-of-order events, unknown streams, and gaps before publication.
Malformed frames are counted without logging raw bodies. Binance quotes carry receipt-time
provenance and degraded health because their exchange timestamps are unavailable.

Binance trade continuity uses aggregate IDs; Bybit trade continuity is explicitly
unverified because provider IDs are not contiguous. Candle continuity uses open
times per interval. Cursors survive reconnects with source-epoch provenance.
Same-venue bounded candle recovery validates complete missing ranges atomically;
historical bars stay in a separate recovery journal. Controlled failover requires
fresh standby streams and recent quote agreement; divergence latches output closed.
Actual venue IDs remain intact. There is no trade history repair, full depth book,
automatic failback or durable journal. See [Phase 2 details](docs/PHASE2_FAILOVER.md),
[historical Phase 1 details](docs/PHASE1_MARKET_DATA.md) and [contracts](docs/contracts.md).

## Containers

```bash
docker compose --profile scaffolding config --quiet
docker compose --profile scaffolding build
docker compose up --abort-on-container-exit
```

API/worker/dashboard remain offline placeholders. Default containers have no network.
Public network access is opt-in through a separate profile:

```bash
docker compose --profile market-data run --rm market-data
```

No secrets, host directories, account data, or ports are mounted. The development
override remains a reserved extension point.

Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). CI covers synthetic fixtures,
Windows/Linux scripts, Python 3.11/3.12, packaging, container diagnostics, and the full fixture suite inside Docker.
Live public Binance/Bybit smoke checks run explicitly outside CI, so network availability
and regional restrictions do not make the fixture suite nondeterministic.
