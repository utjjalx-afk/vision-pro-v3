# Phase 4: Paper Broker V3 and Hard Risk Governor

Phase 4 is isolated on `phase/paper-risk-foundation`, stacked on the unmerged
Phase-3 branch. It has no authenticated API, live order routing, MT5, strategy or
agent. The explicit `PaperBroker` API enables a research simulation; global
environment enabling flags still reject true/ambiguous values and ordinary
startup remains offline. `paper-replay` validates an existing local checkpoint
without network access.

## Supported economics and order lifecycle

Only fully funded Spot longs in the account's explicit quote currency are
supported. A position owns base units; cash pays full notional plus commission.
This is a cash inventory model, not an assumed margin/leverage model. Spot shorts,
leveraged/contract products and mixed-currency accounts fail closed until their
economics are verified. USD and USDT remain distinct.

Each order pins a Phase-3 spec revision. Separate immutable `MarketRules` bind
quantity bounds, steps and notional limits to the same metadata hash/revision.
`rules_from_metadata` extracts Binance LOT_SIZE/MARKET_LOT_SIZE and notional
constraints or Bybit base precision/minOrderAmt/maxMarketOrderQty. Multiple
quantity increments use an exact common multiple, and order quantities also
respect the spec lot increment. Unknown constraints block registration/validation.
Binance reference/average-price notional filters with nonzero avgPriceMins are
unsupported and block rules loading; the simulator never substitutes a guessed
average. This is deliberately narrower than complete exchange order validation.
No account-specific exchange limits or hidden reference-price services are modeled.

Sources: [Binance filters](https://developers.binance.com/en/docs/products/spot/filters)
and [Bybit instrument metadata](https://bybit-exchange.github.io/docs/v5/market/instrument).

The explicit state machine is:

```text
NEW -> VALIDATING -> RISK_APPROVED -> FILLED
                 -> REJECTED
```

MARKET orders fill synchronously and completely or reject. LIMIT/STOP entry
orders are rejected because their matching semantics are not implemented. A
mandatory position SL supplies deterministic protective market-exit behavior.
No-SL entries reject; risk is never assumed zero. Manual closes are separate
market orders tied to the original position. Entry vetoes do not prevent a valid
risk-reducing exit. Quantity/notional constraints and fresh executable quotes
still apply to exits; insufficient liquidity leaves the position open.

## Fill modes

PAPER_REALISTIC is the default. Buys use ask and sells use bid. Configurable
additional spread (half on each side) and slippage are adverse, then price rounds
to the next adverse spec tick. Commissions are an explicit quote-currency fraction
of filled notional. The default research assumptions are 0 additional spread,
2 bps slippage and 10 bps commission; they are configurable assumptions, not
claims about an account's actual fee tier.

Realistic fills require sufficient displayed L1 capacity. Capacity is consumed
per quote separately for bid/ask, so repeated orders cannot reuse the same book.
There is no invented depth or partial fill model. A fresh quote replaces capacity;
duplicate/regressive quotes cannot replenish it.

PAPER_IDEAL is explicitly optimistic: midpoint fills, no extra spread/slippage or
fees, and no L1 capacity restriction. Both modes retain spec, SL and hard risk
validation. Policy can be configured through a journaled command before the first
order; it is locked afterward, including after rejected orders.

When bid crosses a long SL, a protective exit uses the available current sell
price, including adverse costs/ticks. A gap below SL therefore fills below SL,
not at an imaginary guaranteed stop price. If the displayed book cannot close the
position, the exit is rejected and retried on a later fresh quote. Its risk becomes
unknown (`STOP_EXIT_PENDING`) and new entries are blocked. Stop risk estimates
are scenario estimates; gaps can produce losses above them.

## Ledger and equity

Entry debits notional plus entry fee. Exit credits proceeds minus exit fee.
Realized PnL includes both commissions. Open positions use executable simulated
sell prices and estimated exit commission for conservative liquidation equity.
Unrealized PnL includes their entry and estimated exit fees. With valid marks,
equity equals initial cash + realized + unrealized PnL. Cash is not mislabeled as
equity. Funding, borrowing, transfers, rebates and external cashflows are absent.

Missing/stale/future quotes make equity unknown and block new entries. Existing
positions remain in state; no close is fabricated from stale data. Fresh venue
quotes can mark/manage them even when quality/divergence vetoes new entries.
`capture_from_hub` must run synchronously on the hub's owning event loop and
accepts only its current admitted quote. Venue identity is retained; Bybit data
never automatically prices a Binance position. Receipt-time quote execution
requires explicit simulator policy.

## Hard risk vetoes

Every entry passes mandatory governor checks, with no override path:

- Entry-to-SL loss plus adverse stop costs and both commissions versus equity.
- Aggregate current-to-SL open risk plus candidate risk versus equity.
- UTC daily equity loss versus the day's baseline, including unrealized losses.
- Drawdown versus equity high-water, including unrealized losses.
- Canonical-symbol concentration across venues and aggregate gross exposure.
- Verified economics/SL, available cash, current quotes and Phase-2 feed quality.

Default fractions are 1% trade risk, 5% portfolio risk, 3% daily equity loss,
10% drawdown, 25% symbol exposure and 80% gross exposure. These are explicit
research policy defaults, not financial recommendations. Fees/spread can make
projected equity breach a guard before a fill. Loss/drawdown guards latch;
rebounding prices do not unlock them. Daily latch resets at UTC day change while
drawdown remains latched. The new day's baseline carries the last verified equity
before its first update, so an overnight gap is not reset away. No external
deposit/withdrawal handling or timezone-dependent trading day is implemented.

## Immutable lineage and restart

RiskDecision, PaperOrder/OrderResult, Fill and PaperPosition are frozen records.
Stable request identity produces the chain risk_decision_id -> order_id -> fill_id
-> position_id. An identical request retry returns its original result without
another fill or journal entry. A reused ID with changed inputs rejects.

The command journal includes initial cash/currency, cost/risk policies, specs and
rules, quotes/quality, orders and pre-order configuration changes. Hash chaining
detects mismatched/reordered/truncated inputs relative to the stored head; it is
not an authentication signature. Strict replay reconstructs cash, fees, positions,
loss latches, book consumption and deterministic IDs. It does not read the current
market or registry.

Explicit checkpoint paths use a flushed/fsynced temporary file and atomic replace.
With a configured path, a command computes on a private candidate, writes its
checkpoint, then commits memory. Failed writes leave memory unchanged. Restart
replays that checkpoint and request identity prevents duplicate fills. This is
single-process serialized state, not a multiprocess database or power-loss-proof
distributed ledger. Journals are bounded to 10,000 commands / 5 MB, orders to
2,048, and registered instruments to 256. Replay never calls pickle/eval.

```bash
python examples/paper_research.py
python -m vision paper-replay path/to/explicit-paper-checkpoint.json
python -m pytest tests/test_paper.py
```

The example uses synthetic fixtures and manually specified research orders; it
does not connect to exchanges or place account orders. CI runs the full suite on
Windows/Linux Python 3.11/3.12 and Docker, including costs, gap stops, risk gates,
restart/idempotency, corruption, persistence failures and Phase-2 integration.
