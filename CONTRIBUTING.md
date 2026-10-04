# Contributing

Contributions are licensed under Apache-2.0. Keep changes small and explain the
behavior, evidence, and validation in the pull request.

1. Use Python 3.11 or 3.12 and a fresh virtual environment.
2. Install with `python -m pip install -e '.[dev]'`.
3. Run Ruff lint/format checks and `python -m pytest`, or the platform check script.
4. Keep all fixtures synthetic and all timestamps timezone-aware UTC.
5. Use decimal strings on the wire and `Decimal` in monetary/quantity stubs.

Current scope is canonical events, Market Data Hub, Binance/Bybit public REST/WS,
controlled failover, data health, bounded candle recovery, instrument registries and
read-only synchronized portfolio valuation. Use explicit units and spec revisions;
never introduce a fallback multiplier or implicit currency conversion. Use synthetic fixtures in CI and bounded public smoke checks
separately. Phase 4 paper fills belong only in execution/paper with mandatory governor checks.
Do not add strategies, authenticated account endpoints,
live broker calls, or MT5 dependencies. Phase-5 lanes consume only immutable canonical
contexts and produce independent assessments. Missing data is UNAVAILABLE; fixture
signals must be labelled. Phase-6 synthesizer/intents are separate from lanes and
must preserve prospective outcome gates, sample thresholds and unsized candidates.
Do not add LLM control, strategy execution or order submission. Strategy validation
remains separate; risk owns
hard vetoes; broker I/O belongs outside the deterministic core.

Do not copy old Git history, private URLs, local machine paths, secrets, account
data, or source from reference projects. Any future third-party incorporation
requires a license review and an update to THIRD_PARTY_NOTICES.md.

Core Python must run on Windows and Linux. Keep a future MT5 bridge isolated.
Phase-7 paper outcomes must come from replayed broker records and never update
directional reliability. Use transactional append, prospective registration
receipts and superseding corrections; never UPDATE or DELETE finalized history.
Export/replay must work offline and reject semantically invalid re-signed records.
Phase-8 rules consume closed prefixes only. Reuse PaperBroker economics/risk;
preserve next-open availability and INCONCLUSIVE ambiguity. Freeze matrices/folds
before evaluation. Never select winners, retune on holdout, invent liquidity or
promote research to VERIFIED/LIVE.
Phase-9 DSL fields must stay explicit and versioned. Preserve exact preview hashes,
confirmation binding, mandatory preflight and Phase-8 parity. Unknown/ambiguous
semantics are BLOCKED; never infer stops, targets, sizing or execution capabilities.
Artifact changes require new versions. No lifecycle label grants deployment authority.
Architecture amendments require explicit versioned documentation; do not silently
replace a frozen baseline. The full frozen-spec text remains an input dependency.
