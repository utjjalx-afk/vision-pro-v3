# Phase 5 — Independent intelligence lanes

Phase 5 adds deterministic intelligence infrastructure on the accepted Phase-4
commit `b47c71f62cdfe3461578eec95d661f39cc918161`. Its branch is
`phase/intelligence-lanes-foundation`, stacked on `phase/paper-risk-foundation`.
No synthesizer, TradeIntent, strategy, lane reliability weights, LLM control or
order submission is implemented. Live/MT5 and all global enabling flags stay OFF.
The paper broker is independent of this module and receives no lane output.

## Input and output contracts

`LaneContext` is immutable: one venue instrument, an explicit UTC `as_of`, bounded
canonical market events, supplemental `Observation` records, Phase-2 `DataQuality`
and explicit `LanePolicy`. Provider payloads and lane assessments cannot be inputs.
The context rejects duplicate identities, wrong instruments, future timestamps,
receipt-before-source timestamps, mutable containers and excessive numeric ranges.
It preserves input order; it never silently sorts away out-of-order provenance.

`LaneAssessment` contains lane, bias, score, confidence, evidence, features,
data_quality, timestamp, context lineage, deterministic assessment ID, availability
and reasons. `UNAVAILABLE` always has null bias/score/confidence and explicit reasons.
Each feature is a finite Decimal or null with a reason; absent data is never zero.
Every evidence item includes `market_event_id`, provider, source_epoch, source and
receipt timestamps, role and fixture flag. For supplemental canonical observations,
`market_event_id` identifies the observation, not an invented exchange trade.

Scores are signed descriptors in [-1, 1], not trade recommendations. Confidence is
an input-health indicator (1 for valid exchange-timestamp inputs, 0.5 for degraded
or receipt-time inputs); it is not a calibrated predictive probability or reliability
weight. Macro/narrative fixture confidence is always zero. All arithmetic uses a
fixed 256-digit, half-even Decimal context independent of caller precision/traps.

Each lane gets the same immutable input context and cannot see another lane's
assessment. `assess_all` returns four separate results without combining them.
Input lineage covers the complete context, so even irrelevant input changes affect
IDs; unrelated inputs do not change a lane's descriptors.

## Technical

Uses the latest `min_bars` (default 5) closed bars from one provider/epoch and one
interval. Exact opening-time spacing is required; missing openings, open bars,
unverified continuity, reordered bars and source transitions make it unavailable.
A new epoch needs a complete fresh window. Backfill bars are permitted for explicit
research replay; they do not become live events or orders.

- `window_return = (last_close - first_close) / first_close`
- `last_return = (last_close - previous_close) / previous_close`
- `mean_absolute_return = mean(abs(consecutive close returns))`
- `score = window_return / (1 + abs(window_return))`

The volatility descriptor is mean absolute return, not annualized volatility or
standard deviation. No empirical trading edge is claimed.

## Flow

Trades with known aggressor sides produce window-local CVD (buy-aggressor quantity
minus sell-aggressor quantity) and trade imbalance (CVD / total traded quantity).
Unknown aggressors, zero volume and unverified continuity leave those features
unavailable. Binance aggregate-trade sequence gaps are detected. No cumulative
cross-session CVD or exchange-continuity guarantee is fabricated for Bybit trades
whose canonical continuity remains unverified.

Best bid/ask quantities produce book imbalance `(bid_qty - ask_qty) / total_qty`.
Two or more valid quotes produce best-level OFI using the price/queue-size indicator
formula from [Cont, Kukanov and Stoikov](https://arxiv.org/html/1011.6402#S2.SS1).
This is OFI over the supplied ordered observations; sampled snapshots do not claim
to reconstruct unseen order-book events. No full-depth book is implemented.
Trade/quote quantities retain their canonical native units (`canonical_quantity`);
there is no contract multiplier, currency conversion or cross-instrument aggregation.

The directional score is trade imbalance when available, otherwise book imbalance.
This explicit fallback does not average indicators or weight lanes. OFI, funding
and OI are descriptive features and do not affect that score. Features can be
partially available, each with its own missing/invalid reason. All examined inputs,
including invalid windows, remain in the evidence list. Market-stream provider/epoch
transitions block the overall assessment; supplemental observations retain their
own provider/epoch and cannot silently mix units within an observation window.

Funding (`rate_fraction`) and OI (`base_quantity` or `contracts`) are canonical
interfaces only. There is no futures provider or Spot funding feed. A caller must
explicitly establish the relevance of supplemental observations to the research
instrument; the interface does not infer or validate an economic relationship.
Missing observations are unavailable features, not invented zero values. Only the
latest fresh value is described; OI change, funding direction and derivatives
economics are outside this phase.

## Macro and Narrative

These are replay interfaces only. One explicit, labelled synthetic canonical
observation declares a `signed_signal` for each lane. The declaration is returned
with `fixture_only=true` and confidence zero; no economic/news inference occurs.
Multiple declarations are ambiguous and unavailable, rather than averaged with
guessed weights. Missing/stale declarations are unavailable. Live macro/narrative
observations are rejected until a separate provider and semantics design exists.
No news text generation, LLM facts, external network or control path exists.

## Health and replay

Unhealthy/missing/stale quality or Phase-2 divergence makes every lane unavailable
while retaining the exact `data_quality` record. Degraded quality remains explicit
and lowers the input-health indicator. Latest market evidence must be fresh under
`max_age` (default 5 seconds); bar source age allows one bar interval in addition.
Quality TTL is separately 5 seconds; supplemental observations default to 1 hour.
These are configurable research policy values, not market performance claims.

`capture_from_hub` runs on the owning hub loop, captures gate state and the failover
divergence flag, and accepts caller-supplied admitted canonical history. It is a
health capture helper, not a historical-admission authenticator or automatic
subscriber. A bad registered stream dominates a degraded one. Explicit historical
contexts must supply quality recorded at that time, not today's clock/state.

Strict JSON replay stores context policy, ordered events, observations and quality.
It validates exact field shapes and bounded lists, preserves signed decimal strings
and optional market fields, and supports an expected lineage check. Hashes detect
consistency changes; they do not authenticate market facts or fixture declarations.
Replay has no provider, execution, synthesizer or LLM dependency.

```bash
python examples/lanes_research.py
python -m vision lanes-replay tests/fixtures/lanes/research_context.json
python -m pytest tests/test_lanes.py
```

The committed replay fixture is entirely synthetic, with explicitly named synthetic
providers. Global agent/paper enabling remains rejected. The new offline subcommand
reads a bounded input file and emits assessments only. CI runs the complete suite
on Windows/Linux Python 3.11/3.12 and in a network-disabled Docker test container.
