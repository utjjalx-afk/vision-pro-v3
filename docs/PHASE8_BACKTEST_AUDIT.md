# Phase 8: Backtest Engine and Failure Audit

Stacked on Phase 7 at `49b23a23f91bada0a1ece799af996dc45c2532a4`.
The engine exposes broken assumptions. PASS means declared research checks passed
for the supplied data/model; it never means VERIFIED, LIVE or trading edge.
No LLM tuning, winner selection, iterative optimization, autonomous paper loop,
live/MT5 connector or automatic promotion.

## Frozen rules and shared execution

Immutable BacktestInput pins canonical events, instrument revision and verified
market rules, realistic PaperCosts, HardRiskGovernor limits, acceptance thresholds,
cash and explicit Git SHA. BacktestRun pins dataset/config hashes, strategy version,
immutable canonical input/result JSON and PASS / FAIL / INCONCLUSIVE. Run IDs include
the algorithm version and complete result.

The initial declarative close-trend-v1 rule is long-only: close above SMA/EMA plus
threshold enters; below its lower threshold exits; otherwise HOLD. Quantity,
stop/target fractions and warmup are explicit. Pure decision/indicator functions
are reusable by a future explicit paper path. This synthetic rule is not a strategy
recommendation. Arbitrary plugins, shorts/margin/derivatives, multi-asset backtests
and production-scale streaming are outside this foundation.

Orders/fills, commissions, tick/step/min-notional rules, cash and hard risk checks
reuse PaperBroker. Quotes are explicitly synthetic: bar price on both sides, fixed
L1 capacity equal to declared quantity. PaperCosts applies additional spread,
slippage and commissions. This assumes liquidity; it does not infer book depth from
volume or model partial fills, queue position, funding or impact.

## Timing and ambiguity

Only closed prefixes enter rules. Signal availability is max(interval end, received
timestamp). Entry/exit happens only at the immediately next open. Late signals miss
that open and are audited, never filled retroactively. Final-bar signals stay
unexecuted. Stops/targets use signal close, not next open. A gap can invalidate an
entry; the governor rejects it. Existing gap stops use actual synthetic opening
quotes and normal paper costs.

Targets trigger market closes at the target quote or favorable opening gap, not
guaranteed-price LIMIT orders. Cost-adjusted fills may fall below the trigger.
Trade state follows actual broker grading; exit_trigger separately records
STOP / TARGET_MARKET / RULE_NEXT_OPEN.

OHLC bars touching both SL/TP have unknown ordering. The conservative stop-first
estimate is recorded and remains INCONCLUSIVE even if losing. No profitable ordering
is silently chosen. Intrabar timestamps are synthetic model points, not recovered
exchange timestamps.

Optional lower_events are canonical finer bars. Coverage, boundaries, source/epoch
and aggregated OHLCV must reproduce the parent. No interpolation. Finer bars can
resolve parent ordering, but a finer bar touching both levels is still ambiguous.
This is a bar reconstruction hook, not a tick/depth engine.

## Failure rulebook and gates

Preflight rejects missing/reordered/duplicate bars, invalid UTC alignment/close
timestamps, unverified continuity, source/epoch/sequence mismatch and future specs.
Calculations use fixed Decimal contexts.
Prefix-invariance lookahead audit compares full-data evaluation at each index with
prefix evaluation. The engine accepts only the built-in prefix rule; the helper can
detect a caller evaluator reading future bars. It is not an arbitrary-code sandbox
or proof about external data. EMA contamination compares full-prefix and declared
warmup suffix results against a frozen tolerance; excess forces INCONCLUSIVE.

Ambiguity, late signals, rejected entries, open positions, invalid reconstruction,
recursive contamination or too few trades prevent PASS. Invalid data gives FAIL
with null PnL/no checkpoint. Complete unambiguous runs use net PnL and drawdown gates.
Drawdown is close-to-close marked equity, not intrabar maximum. Broker risk latches
see the simulated quote path; unknown OHLC ordering cannot recover continuous equity.
Open PnL is not realized profit; positions are not force-liquidated at dataset end.

## Stress, walk forward and diagnostics

SuiteDefinition freezes bounded cost/parameter/source matrices and fold sizes.
Costs cannot undercut baseline assumptions. Every listed variant runs; none is
selected or retuned.
Walk forward uses rolling train windows only for feature warmup. Disjoint tests
start fresh accounts; only test closes produce signals. Final holdout is excluded
from preceding tests. Parameters stay frozen; first entry can follow first test
close. Insufficient data/folds or incomplete outcomes remain INCONCLUSIVE.

Source variants require distinct supplied providers on aligned time ranges, matching
canonical symbol, frozen rule/cost/risk/acceptance policy. Venue economics stay
explicit. Missing alternatives are INCONCLUSIVE. Regime attribution uses event-ID
labels pinned in the dataset hash; labels are caller declarations, not authenticated
or inferred from future profit.

CSCV PBO uses complementary equal temporal blocks, IS mean-net-return ranking and
OOS rank percentiles. Suites derive aligned returns from fixed candidate equity
changes divided by initial cash. An explicit complete trial-universe declaration
is required. Missing alignment, ambiguous histories or degenerate IS ties produce
INCONCLUSIVE. PBO above frozen maximum_pbo fails the suite. Standalone diagnostic
PASS means calculation applicability only.
The generic CSCV approach is described in
[Bailey et al., The Probability of Backtest Overfitting](https://carmamaths.org/jon/backtest2.pdf).
This implementation uses mean returns rather than annualized Sharpe. It does not
establish statistical independence, authenticated data, undisclosed trial completeness
or trading edge.

## Durable results and Failure Memory

ResearchJournal.backtest/backtest_suite require matching experiment protocol,
commit, costs, spec/source revisions and historical timestamps. Append-only results
have idempotent retries. Replay recomputes every run/suite, rejecting even re-signed
altered PnL/PASS claims. record_run/record_suite link FAIL to REJECTED and INCONCLUSIVE
to PARKED at VALIDATION. Existing fingerprint/family warnings expose duplicates and
pseudo-replication, not independent trials. Backtests never register forecasts or
update lane reliability.

## Offline use and bounds

```bash
python examples/backtest_research.py --output-dir data/backtest-example --commit-sha 49b23a23f91bada0a1ece799af996dc45c2532a4
python -m vision backtest-run tests/fixtures/backtest/ambiguous_input.json data/run.json
python -m vision backtest-replay tests/fixtures/backtest/ambiguous_run.json
python -m vision research-replay tests/fixtures/backtest/research_journal.json
```

Outputs refuse overwrite. The synthetic example is deliberately INCONCLUSIVE.
CLI input/model errors exit 2; successful computation exits 0 with a research status
that callers must inspect. Execution flags are checked before file I/O.
Limits: 500 parent/finer bars each, 8 cost/parameter/source variants, bounded warmup
and Phase-7 journal payload/export limits. Oversized suites are rejected; larger
studies need future streaming/storage work.
