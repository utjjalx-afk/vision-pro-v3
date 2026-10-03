# Vision Pro V3

An Apache-2.0 foundation for market intelligence, research, paper trading,
risk governance, and eventually guarded execution across Crypto, Forex, and Metals.

**Phase-0 only.** The current program prints an offline diagnostic and exits.
It does not connect to feeds, serve an API/dashboard, simulate trades, or place orders.
Live trading and MT5 execution are unimplemented; enabling either flag fails at startup.

## Architecture

```text
Market Data Hub -> Data Health Gate -> Canonical Event Bus -> Deterministic State
    -> Technical / Flow / Macro / Narrative lanes -> Reliability Synthesizer
    -> Trade Intent -> Risk Governor -> Paper / Execution Control
    -> Order State Machine -> Event Store -> Outcome / Journal / Failure Memory
```

Strategies will produce intents. They will never call broker order APIs directly.
Research promotion will require failure-rulebook, lookahead, execution-assumption,
backtest, statistical-overfit, cost-stress, walk-forward, OOS, and paper gates.

See [Architecture Spec v1](docs/architecture/ARCHITECTURE_SPEC_v1.md) for baseline
provenance, intended boundaries, and the missing full-spec limitation.
Phase-0 establishes folder boundaries and schema stubs, not these engines.

## Native setup (Python 3.11 or 3.12)

Windows PowerShell:

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

Both setups create `.venv` and install the package with development tools.
Direct equivalent in an activated virtual environment:

```bash
python -m pip install -e '.[dev]'
python -m vision
python -m ruff check .
python -m ruff format --check .
python -m pytest
```

Configuration uses process environment variables. `.env.example` is documentation
and is not loaded automatically. There are no runtime third-party dependencies.

## Containers (skeleton)

```bash
docker compose config --quiet
docker compose build
docker compose up --abort-on-container-exit
```

The default API placeholder prints a diagnostic and exits successfully. Future
worker/dashboard boundaries can be inspected with `docker compose --profile scaffolding up`.
No listeners, credentials, databases, broker bridges, or persistent services are configured.
The development override is a reserved extension point:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet
```

## Repository map

| Path | Phase-0 role |
| --- | --- |
| `src/vision/core` | Canonical event and InstrumentSpec stubs |
| `src/vision/market_data` | Feed adapters and health boundaries |
| `src/vision/analysis` | Independent lane and synthesis boundaries |
| `src/vision/risk`, `execution`, `orders` | Reserved governance/lifecycle boundaries |
| `src/vision/research`, `storage`, `journal`, `failure_memory` | Reserved research and outcome boundaries |
| `src/vision/apps`, `apps` | Offline app placeholders and future app contracts |
| `schemas`, `tests` | Public wire-schema stubs and contract/safety tests |
| `scripts`, `docker`, `.github/workflows` | Portable setup, containers, and CI |

The Windows-only MT5 bridge is a future isolated boundary. No MT5 package is installed.
The existing Vision Pro repository is separate; this repository has an independent history.

## Development and security

Read [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). CI checks Python 3.11/3.12 on
Windows/Linux, packaging, native scripts, and Compose skeleton validity.
