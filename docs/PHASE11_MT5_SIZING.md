# Phase 11 — MT5 broker truth and calculation-only sizing

**Implementation/research status: acceptance pending mandatory four-asset demo
reconciliation. CI does not establish broker correctness.**

The Windows MT5 reader is isolated behind an authenticated loopback API. The
cross-platform core/client and deterministic sizer do not import the optional
native package. There is no order submission function or endpoint. Global live,
MT5 execution, autonomous paper and agent flags remain false. A
`BROKER_SIZE_APPROVED` receipt approves a calculation, never execution.

## Broker truth

`MT5Reader` attaches to a user-selected terminal already logged into a connected
**demo** account; it never logs in or switches accounts programmatically. Exact
canonical mappings must be supplied for XAU/USD, XAG/USD, EUR/USD and BTC/USD.
Broker suffixes are retained unchanged. Symbol discovery returns candidates, not
automatic selections. Base/profit currencies must agree with the mapping; BTC
securities or BTC/USDT cannot silently become BTC/USD. Connection may select the
explicit mapped symbols into local Market Watch; it changes no broker orders.

The reader projects native symbol information: contract/tick sizes, tick values,
volume min/max/step/limit, point, stop/freeze levels, trade mode and currencies.
Quotes use native bid/ask and time_msc, not an external provider price. Account
equity, balance, free margin and all current positions/SLs are captured together.
Pending orders block entries until their risk semantics are supported. Missing
responses are not empty positions or zero risk.

Raw login/server and position tickets are replaced with keyed HMAC identifiers.
The runtime bearer secret also pins this identity namespace; secret rotation
changes identities intentionally. Account changes, quote/spec/position changes
during sizing and uncertain capture all fail closed. Local receipts and caller
eligibility assertions are not authenticated market truth or deployment grants.

## Native sizing semantics

Explicit eligible SizeRequest (or identical canonical TradeIntent conversion)
and explicit SizingPolicy are required. Unsized candidates cannot be submitted
as orders. No default SL, budget, eligibility, reserve or cost buffer is supplied.

1. Validate connected pinned demo identity, expiry, fresh synchronized account,
   specs/quotes, broker mapping/revision, trade mode and per-trade equity cap.
2. LONG entry uses ask and bid-side stop trigger; SHORT entry uses bid and
   ask-side stop trigger. Reject invalid side, off-tick SL and stops inside the
   stricter stops/freeze distance. Never widen or round the stop automatically.
3. Native [order_calc_profit](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py)
   at exactly one lot determines the account-currency SL loss. Tick value or a
   global contract multiplier is never used as a fallback.
4. Subtract explicit loss buffer; cap to volume max/directional limit; floor to
   volume_step. Below-minimum volume blocks. Non-zero-origin/ambiguous volume
   grids are unsupported. No minimum-volume forcing.
5. Recalculate the **actual final volume** with native profit calculation. An
   unexpected nonlinear/over-budget result blocks rather than resizing in a loop.
6. Native [order_calc_margin](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcmargin_py)
   must fit current free margin plus explicit reserve. Native margin is an
   estimate and does not include pending/open orders by itself; pending orders
   block and existing free margin remains authoritative.
7. Calculate existing-position additional equity loss from the current
   closeable quote to its actual SL using native profit. No-SL/unknown/crossed-SL
   positions block under the required BLOCK policy. Apply aggregate open-risk
   equity cap. This SL-risk estimate does not bound gaps, swaps or execution
   costs; explicit buffers are research assumptions, not guarantees.
8. Re-read broker state; any account/spec/quote/position change blocks approval.

Native MT5 SDK inputs/outputs are binary floats. The adapter preserves their
shortest decimal representations and the deterministic core uses fixed-context
Decimal arithmetic. It does not pretend the SDK returns exact decimal money.
Native error bodies, terminal paths and credentials are not logged.

## Authenticated API

Install the optional Windows binding: `python -m pip install -e ".[mt5]"`.
Set a randomly generated runtime `MT5_BRIDGE_TOKEN` of at least 32 characters.
Keep it out of commands, files under version control and logs. Supply a local
mapping JSON whose keys are exact canonical symbols and whose values are exact
discovered broker names.

```text
python -m vision.broker.server --mapping private-mapping.json --terminal-path <terminal-path>
```

Authenticated routes:

- GET `/v1/health`: bridge/terminal version, pinned account identity/currency,
  demo/connection and absent order-dispatch capability.
- GET `/v1/symbols`: broker discovery suggestions.
- GET `/v1/snapshot`: private broker truth projection.
- POST `/v1/size`: exact request/policy DTOs → immutable calculation receipt.
- POST `/v1/size-audit`: complete snapshot, after-snapshot, request, policy,
  clock, native calculation transcript and result for offline replay.

Every endpoint requires a bearer token. Constant-time comparison, duplicate
authorization/framing rejection, bounded JSON, safe errors, no redirects and no
access logs are enforced. Server binds **127.0.0.1 only**, serializes SDK calls,
and refuses arbitrary native methods/order endpoints. This phase does not provide
a remotely exposed network listener or TLS deployment; that requires a separate
authenticated encrypted transport design. Local filesystem/admin owners remain
outside the trust boundary.

Offline replay:

```bash
python -m vision broker-sizing-replay tests/fixtures/broker/synthetic_sizing.json
```

Replay validates exact native-call arguments/currency and both snapshots,
recomputes volume/risk/margin/guards and requires exact result equality. Complete
audits contain private account economics; store them locally, never in the public
repo. Public fixtures are wholly synthetic. Hashes check consistency, not truth.

## Mandatory demo acceptance gate

`scripts/reconcile-mt5-demo.py` captures both sides for all four explicitly mapped
assets using supplied stops/budgets/policy and a connected demo terminal. Its
private config contains exactly mapping, policy and plans. Each plan has explicit
LONG/SHORT stop decimal strings and risk_budget. Policy uses decimal strings and
integer microsecond max_age/max_skew, BLOCK unknown-risk and demo_only=true.

```text
python scripts/reconcile-mt5-demo.py private-protocol.json private-report.json --terminal-path <terminal-path>
```

Output is exclusively created, never overwritten, and always marked
`AWAITING_OPERATOR_RECONCILIATION`, acceptance_passed=false. An operator must
independently compare terminal broker specs, suffixes, SL distances, volume
steps, native profit/margin and account-currency effects, record evidence and
sign off. CI/synthetic fixtures cannot satisfy that requirement. No automatic
promotion or acceptance endpoint exists.

Current local demo check: XAUUSD, XAGUSD and EURUSD are available; six negative
one-lot diagnostic SL losses and six valid native margins were captured privately.
The connected account has **no BTC/USD crypto instrument**. BTC-named USD/USD
securities were rejected as substitutes. Four-asset reconciliation and operator
review remain pending; Phase-11 acceptance is not claimed. OANDA smoke testing is
independent of this gate.
