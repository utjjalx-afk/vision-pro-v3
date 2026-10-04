# Phase 6 — Reliability Synthesizer + TradeIntent

This phase is stacked on Phase 5 at
`9708ac853d29a965f113f299a7d2615e2bf723cf` in
`phase/reliability-intent-foundation`. It implements deterministic research
plumbing, not demonstrated trading edge. No LLM, strategy, order submission,
broker connection, MT5 or live execution is added. Global enabling remains OFF.

## Prospective Outcome Grader boundary

`ProspectiveLedger.register` records an immutable `ForecastCommitment` using the
ledger clock before its future horizon. It pins the LaneAssessment, canonical
trade baseline, grading protocol, registration timestamp and optional regime.
No hindsight/backfilled baseline, unavailable/neutral/zero-confidence assessment,
fixture lane, receipt-time benchmark or unhealthy/divergent input can qualify.
Market evidence must match the baseline provider/epoch; supplemental evidence
retains its own provenance. Synthetic evidence must be explicitly designated as
fixture outcomes and is never eligible reliability evidence.

`grade` requires an existing registration and a fresh live exchange-timestamp
trade at/just after the frozen horizon from the pinned instrument/provider/epoch.
It rejects early, late, stale, unhealthy, divergent or cross-epoch evidence. There
is no reward override: directional reward is recomputed from canonical price
change and the assessment's original sign. Correct above the declared minimum
move earns 1, incorrect earns 0, and movement within the neutral band earns 0.5.
Defaults: horizon 60 seconds, minimum absolute move 0.001 (0.1%), fresh-evidence
TTL 5 seconds and terminal tolerance 5 seconds.

This is directional **benchmark grading**, not realized trade PnL, fill simulation
or a cost-adjusted strategy backtest. Fees, spreads, funding and executable entry
economics are not inferred. It cannot establish profitable trading edge.

Retries are idempotent; a grade cannot be overwritten. Per-lane/instrument windows
cannot overlap. Duplicate forecasts, baseline events and terminal events cannot
inflate sample counts. Regime is declared before the outcome. Immutable snapshots
exclude grades not known by the requested timestamp. Fixture outcomes remain
available for replay but cannot activate reliability.

The ledger is a bounded, serialized single-process research boundary (10,000
registrations), not a durable authenticated Outcome service. It does not write
registration receipts to external storage, authenticate providers or prevent a
caller fabricating historical records. Exported replay records verify chronology,
shapes and consistency, not authenticity. A production prospective evidence store
and strategy-validation process remain future work. There are no eligible real
outcome datasets or trained reliability weights shipped in this repository.

## Reliability revisions

Reliability is derived only from validated `GradedOutcome` records, never lane
confidence alone, hand-entered win totals or model opinion. Revisions are scoped
to lane, instrument and the exact pinned grading protocol. A regime-specific
request selects only that prospective regime and has its own sample gate; it does
not fall back to global reliability.

For n eligible outcomes with reward sum r and prior strength k:

```text
posterior_success = (r + k/2) / (n + k)
eligible_reliability = max(0, 2*posterior_success - 1)
```

The prior shrinks toward neutral 0.5. Before the sample minimum, state is
`UNPROVEN` and eligible reliability is null. At sufficient samples, posterior at
or below neutral is `NO_SUPPORT`; a positive shrunk support value is `ELIGIBLE`.
ELIGIBLE means permitted to contribute under policy, not statistically established
or profitable. This is not a calibrated probability, confidence interval,
independence guarantee or multiple-testing correction.

Default global minimum is 30, regime minimum 50 and prior strength 20. Thresholds
are configurable with an enforced floor of 10 samples and prior strength 10.
Each revision carries exact outcome IDs, protocol/policy hash, cutoff and scope.
Grades after the current **market timestamp** cannot enter SynthesisInput, even
when synthesis is performed later. Replaying an older snapshot cannot learn from
future outcomes.

## Independent synthesis

`SynthesisInput` is immutable and contains the InstrumentRecord, synchronized lane
assessments, prospective outcomes, health, policies, optional regime and explicit
UTC clock. Lanes remain independent; the synthesizer alone sees their outputs.
There is no manually supplied weight table. Unavailable, fixture, unproven,
unsupported, missing-evidence and zero-confidence lanes have no contribution,
rather than a zero/neutral vote. An available neutral descriptor is explicit and
can affect aggregate directional support.

For each eligible lane:

```text
weight = confidence * data_quality_factor * eligible_reliability
signed_contribution = lane.score * weight
score = sum(signed_contribution) / sum(weight)
support = sum(weight) / eligible_lane_count
```

`data_quality_factor` is 1 only for fresh healthy non-divergent inputs. Poor global
or participating-lane data produces WAIT and no contribution from that lane.
Degraded/receipt contexts are not eligible for directional synthesis. The lane's
signed score retains descriptor strength; no simple majority vote is used.

Defaults require two eligible lanes, support >= 0.1 and abs(score) >= 0.25.
The minority directional-mass share must not exceed 0.2. A single lane must not
exceed 0.6 of either total weight or absolute directional mass. These guards stop
a strong lane plus an effectively neutral lane masquerading as consensus. Poor
data, low support, disagreement, dominance, stale assessments/specs or mixed
market provider/epochs produce `WAIT`. Directions are only LONG / SHORT / WAIT.
Assessment TTL is 5 seconds and spec TTL is 24 hours, configurable explicitly.

`SynthesisDecision` retains all supplied lane IDs, including excluded lanes, with
evidence hashes, reliability revisions, exclusions, market timestamp, current
quality, spec revision, source lineage, input lineage, expiry and algorithm version.
WAIT may retain diagnostic score/support but never produces an intent. An entirely
unproven input has null score/support. Arithmetic uses the fixed Phase-5 Decimal
context, so caller precision, rounding and traps cannot alter decisions.

## Unsized TradeIntent candidate

`candidate(decision)` emits an immutable candidate for LONG/SHORT only; WAIT
returns null. It includes symbol/instrument, direction, thesis/evidence IDs,
reliability revision IDs, support indicator, timestamp, market timestamp, expiry,
invalidation conditions and provenance. It has **no quantity, final lot size,
order ID, fill or final stop price**. Its confidence is support, not calibrated
predictive probability.

Candidates invalidate on expiry, unhealthy/divergent data, source-epoch change,
instrument revision change, strategy ineligibility or risk veto. These declarative
conditions are context for a future consumer, not a running position manager.
Strategy/eligibility validation, concrete SL and final sizing remain separate
future gate/risk/broker responsibilities. No router connects candidates to the
paper broker; PaperBroker rejects TradeIntent as an order. SHORT can be a research
candidate while the Phase-4 cash-spot broker continues to reject short execution.

## Portable replay and verification

Strict JSON replay reconstructs metadata, assessments, grading protocols, frozen
commitments, terminal evidence and quality. Rewards and reliability are recomputed;
unknown fields, fake reward/weight overrides, future evidence and malformed data
are rejected. Context lineage supports exact decision/intent replay; hashes are
consistency checks, not signatures. CLI input is bounded to 5 MB.

```bash
python examples/synthesis_research.py
python -m vision synthesis-replay tests/fixtures/synthesis/unproven_input.json
python -m pytest tests/test_synthesis.py
```

The committed example/fixture has only synthetic lanes and no graded outcomes;
it intentionally returns WAIT and no intent. Tests exercise eligible decisions
using controlled prospective records, along with shrinkage, small samples, regime
gates, gaps, no lookahead, dominance, quality, idempotency and exact replay. CI
runs the full suite on Windows/Linux Python 3.11/3.12 and network-disabled Docker.
