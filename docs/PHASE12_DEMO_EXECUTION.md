# Phase 12 — Guarded MT5 DEMO execution

Status: software implementation; broker acceptance pending. This stacked branch
does not mark Phase 11 accepted, arm a connected terminal, or establish live eligibility.
The operator authorized coding before Phase 11 sign-off; the original frozen
plan's coding hold is superseded only for implementation. The first actual demo
smoke test remains a separate explicit gate.

## Entry boundary

Typed TradeIntent → explicit strategy eligibility → native sizing audit →
HardRiskGovernor → final broker recheck → DEMO-only gateway → broker facts →
append-only Outcome Journal. No natural-language/LLM order interface or strategy
scheduler exists. Sizing approval remains calculation-only; it never arms execution.

Global live, MT5, agent and autonomous paper flags stay false. The separate
`vision-mt5-demo` entrypoint requires explicit private configuration; feature
enabled is separate from temporary `ARMED_DEMO`. Startup and restart are DISARMED.
Operator Phase 11 acceptance evidence must be declared for the exact HMAC account
identity before arming is possible. Evidence is a reviewed local attestation,
not a forged automatic acceptance endpoint.

The existing Phase 11 quote freshness implementation is unchanged. Future
native timestamps, including the unresolved three-hour discrepancy, continue
to block sizing and execution. No inferred timezone offset is applied.

## Narrow supported broker semantics

- Connected DEMO account only, exact account allowlist and explicit broker mapping.
- Isolated account, no existing positions or pending orders for a new entry.
- Hedging account (`margin_mode=2`); netting and exchange modes block.
- MARKET entry, Market Execution (`trade_exemode=2`), FOK support required.
  IOC/RETURN-only brokers block; partial facts still halt for reconciliation.
- Explicit valid tick-grid SL, optional valid TP, broker-native floored volume.
- Dedicated magic and a bounded hash of client_order_id in broker comment.
- No pyramiding, grid, martingale, pending entries, stop widening or force-min volume.
- Explicit full-position MARKET close only; no strategy partial closes.

FOK support and all other native fields must come from the actual terminal.
These restrictions may block the currently connected broker. They are not
demonstrated broker compatibility until the separately authorized smoke run.

## Final checks and risk

The supplied complete sizing audit must replay, match intent/request/policy,
and be fresh. Capture native state again; changes to account, quotes, source
timestamps, specs, equity/free margin, positions or pending orders reject and
require a newly calculated command. Re-run native sizing and compare economics.
The receipt's SL/volume are never silently altered. Apply explicit spread cap,
TP validation and account trade/AlgoTrading permissions. Check arming expiry
again after native calculation. Native transport takes another snapshot before
submission and checks connected demo identity immediately before order_send.

HardRiskGovernor enforces per-trade, aggregate, daily equity loss and drawdown
caps. RiskContext pins current account/currency, same-day baseline and high-water
mark. For this isolated CFD gateway its configured symbol/gross exposure inputs
are explicitly **native margin plus SL risk**, not leveraged notional exposure.
These margin-at-risk caps are separate research assumptions; they do not claim
spot/CFD notional equivalence. No contract multiplier or FX rate is guessed.
Actual fill risk/margin are recalculated natively; excess SL risk halts new entries.
Gap/slippage losses are not bounded by an SL calculation.

## Persistence, uncertainty and restart

CREATED → PRECHECKED → SUBMITTING → BROKER_ACK → FILLED → POSITION_OPEN →
CLOSING → CLOSED. REJECTED, UNKNOWN, RECONCILIATION_REQUIRED and
CRITICAL_PROTECTION_FAULT are explicit side states.

The existing SQLite Outcome Journal commits SUBMITTING with synchronous FULL
durability before the native call. Account-wide active reservations and intent
deduplication are checked transactionally. Reusing client_order_id with identical
command returns its journal state; changed command collides. A new client id
cannot reuse a consumed intent. Nothing resends even after a definitive rejection.

Crash after SUBMITTING, timeout, unknown code or malformed result stays uncertain.
Restart never rearms and unresolved reservations block arming. Monitoring reads
native orders/deals/positions to resolve; an absence of history never proves a
submission was not delivered. CREATED/PRECHECKED without committed SUBMITTING
can be finalized as unsent by explicit monitoring. No exactly-once transport
claim is made. Journal persistence failure after a native call leaves the durable
reservation unresolved; recover by monitoring, not sending again.

## Broker reconciliation and outcome audit

ACK is not proof of a fill. Require broker order/deal/position identity, symbol,
side, exact filled volume, weighted fill price, exact SL/TP and position volume.
Broker-triggered SL/TP exits are linked by the proven position identity even if
their comment differs. Unknown ownership, partial volume or state divergence
halts new entries. Missing accepted SL raises CRITICAL_PROTECTION_FAULT and
HALTED immediately; no automatic repair or emergency close is implemented.
Read-only monitoring continues in every state, including after kill switch.
The optional server polls active journal reservations once per second without
submitting or repairing orders. Disarming never clears an incident state;
clock rollback also invalidates ephemeral arming.

Verified closing deals and absent position establish CLOSED. Net outcome uses
broker profit + commission + swap + fee in account currency. The journal stores
signal evidence, synthesis, intent, source, risk decision, original sizing receipt,
fresh audit, client id, redacted MT5 request/order/deal/position ids and exit/outcome
facts. Chain, legal transitions, original/fresh native sizing audits and the
persisted governor context/limits replay offline, including risk-decision identity.
Demo outcomes never enter reliability calibration.

Parity audit compares expected entry with fill, spread and directional slippage,
volume, SL, native expected/actual fill risk/margin, fees and broker PnL. Existing
PaperBroker supports spot only: CFD paper PnL/margin/fees remain explicitly
UNAVAILABLE. The audit does not invent paper economics or silently calibrate them.

## Optional authenticated server

Private config keys are exactly `mapping`, `demo_policy`, `risk_limits`,
`experiment_id`; typed decimal values use strings and durations use integer
microseconds. DemoPolicy contains account_identity, phase11_evidence_id,
phase11_accepted, max_spread, deviation_points, receipt_ttl, arm_ttl and magic.
RiskLimits uses its existing six explicit fractions. Do not publish this config.

```text
vision-mt5-demo --config private-demo.json --terminal-path <terminal> --journal private-demo.sqlite
```

`--enable-demo` separately enables the feature; it does not arm. All routes use
runtime `MT5_BRIDGE_TOKEN` bearer auth, constant-time comparison, bounded strict
JSON, safe errors and loopback 127.0.0.1. The existing calculation-only server
does not gain demo routes unless an explicit gateway is supplied.

GET `/v1/demo/status`; POST `/v1/demo/arm`, `/disarm`, `/halt`, `/execute`,
`/monitor`, `/close`. Paths after the first also have `/v1/demo` prefix.
Arming requires exact account_identity and operator_confirmation=ARMED_DEMO;
closing requires client_order_id and operator_confirmation=CLOSE_DEMO_POSITION.
Execution envelope has exact client_order_id, typed intent, request, sizing_policy,
complete sizing_audit, sized_at, risk_context, optional tp (explicit null allowed).
No arbitrary raw MT5 request, native method or live override is exposed.

The bearer-token holder, acceptance attestor, risk baseline provider and local
filesystem owner are trusted operator boundaries. One gateway/journal must own
the isolated account. Native broker state may change outside local locks;
post-fill reconciliation and fail-closed incidents remain necessary.

## Validation and separate smoke gate

Synthetic tests cover account gates, permission/unsupported modes, expiry,
freshness, price/spec/equity/margin moves, spread/TP, native return codes,
deduplication, restart/uncertainty, partial fill, missing/mismatched SL, ownership,
full close/PnL, journal replay and authenticated routes. No test imports the real
MetaTrader5 package or places a connected-terminal order.

Windows/Linux Python 3.11/3.12 and Docker run the full deterministic suite in CI.
PR stays draft. Software CI does not establish broker acceptance. After Phase 11
acceptance and a separately approved bounded demo session, smoke one instrument
at a time: EURUSD, XAUUSD, BTCUSD, XAGUSD. Verify entry/fill/SL, position, full
close, deals/PnL and journal replay. No real-account/live smoke is authorized.

Native request semantics follow [MetaQuotes order_send](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py)
and [trade return codes](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes).
