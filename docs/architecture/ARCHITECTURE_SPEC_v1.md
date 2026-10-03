# VISION PRO V3 — ARCHITECTURE SPEC v1

## Baseline provenance

The supplied conversation preview labels this baseline **FROZEN FOR PHASE-1**,
with repository target `vision-pro-v3`, public visibility, Windows/Linux as
first-class platforms, Crypto/Forex/Metals as primary markets, and live execution
off by default. The old Vision Pro remains a stable reference without destructive migration.

**Source completeness: partial.** The preview truncates section 2. Retrieval of
the referenced conversation returned the architecture audit and cross-platform
requirements, but not the full frozen-spec message. This document preserves the
available frozen core below and records the other recoverable baseline decisions.
It is not a verbatim substitute for the missing full specification. Folder names
and schema fields are provisional Phase-0 mappings until that text is supplied.
Do not label this document a newly approved or complete frozen specification.

## 1. Frozen core principle (available excerpt)

```text
MARKET DATA
    ↓
DATA HEALTH
    ↓
CANONICAL EVENT BUS
    ↓
DETERMINISTIC STATE
    ↓
INDEPENDENT ANALYSIS LANES
    ↓
SYNTHESIZER
    ↓
TRADE INTENT
    ↓
RISK GOVERNOR
    ↓
PAPER / EXECUTION
    ↓
ORDER STATE MACHINE
    ↓
OUTCOME + JOURNAL + FAILURE MEMORY
```

Strategy directly broker order create nahi karegi.

## 2. Recoverable system architecture

```text
                         MARKET DATA HUB
          Crypto: Binance / Bybit | Forex/Metals: Dukascopy / OANDA
                 Broker: MT5 | TradingView: source marked with *
                                 |
                         DATA HEALTH GATE
                                 |
                         CANONICAL EVENT BUS
                                 |
                         STATE REDUCER / CACHE
                                 |
             Technical / Flow / Macro / Narrative lanes
                                 |
                       RELIABILITY SYNTHESIZER
                                 |
                          LONG / SHORT / WAIT
                                 |
                            TRADE INTENT
                                 |
                       RISK GOVERNOR / HARD VETO
                                 |
                          EXECUTION CONTROL
                                 |
                       PAPER / MT5 / EXCHANGE
                                 |
                        ORDER STATE MACHINE
                                 |
                            EVENT STORE
                                 |
                  OUTCOME GRADER / TRADE JOURNAL
                                 |
                           FAILURE MEMORY
```

Technical, Flow, and Macro appear in the frozen preview. Narrative and the
expanded downstream boundaries appear in the preceding architecture audit.
TradingView's asterisk qualification is missing from the truncated source;
its transport, licensing, and eligibility remain unspecified and unimplemented.

Connector behavior is isolated behind connect/disconnect, trade/book/bar
subscriptions, instrument discovery, health, and reconciliation contracts.
Canonical events normalize source differences before deterministic reduction.
No strategy or analysis lane owns broker credentials or broker I/O.

Same strategy semantics are intended across research, backtest, paper, and
eventual live operation. Intents pass a risk veto before any execution plan.
Order lifecycle must eventually account for creation, acknowledgement,
submission, partial fills, fills, closure, rejection, and cancellation, with
reconciliation and append-only evidence. Phase-0 implements none of this lifecycle.

## 3. Recoverable research governance

```text
Strategy Idea -> Failure Rulebook -> Lookahead Audit
    -> Execution Assumption Audit -> Backtest -> Statistical Overfit Audit
    -> Cost Stress -> Walk Forward -> OOS -> Paper -> LIVE_ELIGIBLE
```

Nexus failure doctrine is intended to govern research promotion. Its complete
rulebook is not present in the retrieved source and must not be invented here.
Research eligibility never bypasses the runtime risk governor or execution controls.

## 4. Cross-platform requirement (retrieved conversation)

> Vision Pro V3 core must run natively and through containers on Windows and
> Linux. No trading/research module may depend directly on Windows-specific APIs.
> MT5 integration must remain an isolated Windows bridge accessible over a
> documented network interface.

Future core services include API, worker, dashboard, market data hub, paper
broker, and risk engine. Easy mode is intended to use SQLite with optional Redis;
production is intended to use PostgreSQL and Redis. Use pathlib, UTC timestamps,
environment configuration, and portable subprocesses. Future x86_64 and ARM64
support depends on dependency compatibility; Phase-0 does not claim ARM validation.
The MT5 bridge is a separate Windows-only boundary, not a core dependency.

## 5. Phase-0 implementation boundary (historical baseline)

Phase-0 adds original documentation, licensing, Python packaging, schema stubs,
placeholder packages/apps, portable scripts, container skeletons, and CI/tests.
No feed I/O, analysis algorithms, signal generation, backtests, paper fills,
database persistence, event bus, reducer, MT5 bridge, broker execution, or web UI
is implemented. The CLI prints an offline diagnostic and exits.

CanonicalMarketEvent and InstrumentSpec v1 stubs use immutable Python dataclasses,
aware UTC timestamps, finite exact Decimal values, decimal strings on the wire,
explicit source/instrument identities, and a versioned public JSON Schema.
Only trade, quote, and bar payload stubs are supplied. Other market-event types,
sequencing scopes, instrument catalogs, and migration policies need Phase-1 design.
No timing-order assertion is made between source and receive clocks: health
and clock-skew treatment belong to the future Data Health Gate.

Live trading and MT5 execution are hard-disabled and unimplemented. Startup
rejects every enabling or ambiguous value for either execution flag. Broker
credentials are neither read nor logged by the foundation.

## 6. Reuse and migration

Build a fresh repository with independent Git history. Architectural ideas from
the prior audit are reference material; no code is copied from those projects.
Permissive licenses still require attribution for any future incorporation.
GPL/LGPL and projects without clear licenses require explicit review before reuse.
No private data or old repository content belongs in this public foundation.

## 7. Outstanding source requirement

Insert the complete frozen Architecture Spec v1 once supplied, preserving its
wording and reconciling the provisional package map and schema fields against it.
Until then, exact full-spec alignment cannot be verified. This limitation must
remain visible in any foundation pull request.

## 8. Current Phase 1 implementation

The user explicitly authorized Canonical Events, Market Data Hub, Binance public
REST/WebSocket, and Data Health Gate on the foundation branch. See
[Phase 1 implementation](../PHASE1_MARKET_DATA.md) for current behavior, contracts,
health semantics, limits, and validation. Section 5 records the original Phase-0
state. Execution remains unimplemented/off; the missing complete frozen-spec
text remains an acknowledged provenance limitation.

## 9. User-authorized Phase-2 implementation

Phase 2 extends the data-only layer with Bybit public Spot market data, controlled
Binance-to-Bybit source selection, divergence checks, connection epochs and atomic
bounded candle recovery. Details and explicit continuity limitations are in
[PHASE2_FAILOVER.md](../PHASE2_FAILOVER.md). The historical frozen baseline above
is preserved; the full-spec provenance limitation remains. Main stays at Phase 0
while implementation is reviewed on the draft foundation PR. All execution, MT5,
paper and agent capabilities remain disabled and unimplemented.
