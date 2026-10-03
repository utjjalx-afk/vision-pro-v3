# Contributing

Contributions are licensed under Apache-2.0. Keep changes small and explain the
behavior, evidence, and validation in the pull request.

1. Use Python 3.11 or 3.12 and a fresh virtual environment.
2. Install with `python -m pip install -e '.[dev]'`.
3. Run Ruff lint/format checks and `python -m pytest`, or the platform check script.
4. Keep all fixtures synthetic and all timestamps timezone-aware UTC.
5. Use decimal strings on the wire and `Decimal` in monetary/quantity stubs.

Current scope is canonical events, Market Data Hub, Binance public REST/WS, and
data health. Use synthetic fixtures in CI and bounded public smoke checks
separately. Do not add strategies, paper fills, authenticated account endpoints,
live broker calls, or MT5 dependencies. Analysis will produce intents; risk owns
hard vetoes; broker I/O belongs outside the deterministic core.

Do not copy old Git history, private URLs, local machine paths, secrets, account
data, or source from reference projects. Any future third-party incorporation
requires a license review and an update to THIRD_PARTY_NOTICES.md.

Core Python must run on Windows and Linux. Keep a future MT5 bridge isolated.
Architecture amendments require explicit versioned documentation; do not silently
replace a frozen baseline. The full frozen-spec text remains an input dependency.
