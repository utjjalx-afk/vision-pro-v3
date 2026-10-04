# Phase 9: Strategy DSL and Deterministic Research Runtime

Stacked on Phase 8 at `460ea6978df80066f2ca02c556da1a55bb190cca`.
Structured artifacts replace ad hoc rule snippets on this path. This phase validates
parsing, exact compilation and backtest parity, not strategy edge or deployment.
No autonomous paper/live path, LLM service, parameter search or losing-strategy repair.

## Artifact and schema

`vision.strategies.dsl.StrategyDefinition` is an immutable canonical JSON artifact,
distinct from the existing Phase-8 compiled rule DTO. It stores:

* DSL schema version, strategy key, explicit three-part strategy version and origin.
* Declared lifecycle metadata, canonical market, asset class, timeframe, warmup,
  required data and instrument constraints.
* Named canonical feature definitions and exact entry/exit comparisons.
* Quantity/unit/sizing, mandatory invalidation and profit-exit trigger/fill semantics,
  gap policy and every hard risk limit.
* Exact costs, timing, ambiguity/liquidity model and required execution capabilities.
* Acceptance limits, research cash and caller-declared Git commit SHA.

No indicator, period, warmup, stop, target, quantity, cost or timing parameter is
silently defaulted. The strict parser rejects absent/extra fields, duplicate JSON
keys, floats where exact decimal strings are required, non-finite numbers, bad
references, invalid bounds and unknown schema versions. JSON key ordering and
whitespace canonicalize; decimal strings retain their declared representation.
Config hash and strategy ID bind the complete artifact, including version/metadata.
Dictionary access returns a copy.

[strategy-definition-v1.schema.json](../schemas/strategy-definition-v1.schema.json)
provides the editor/interchange structural contract using
[JSON Schema 2020-12 validation](https://json-schema.org/draft/2020-12/json-schema-validation).
The deterministic Python validator additionally enforces exact Decimal bounds,
integer types, references and cross-field consistency. Structural schema validity
alone does not mean a rule is supported or executable.

## Exact rule preview and supported compilation

Preview includes a readable comparison summary plus the complete sorted artifact,
all parameters/constraints, declared origin/lifecycle and research-only scope.
Its hash binds exact text, strategy identity and compiler version.

V1 faithfully maps the existing close-trend-v1 semantics: one named trend feature,
SMA/EMA of closed canonical prices, strict close GT trend * entry multiplier and
close LT trend * exit multiplier. Multipliers must encode symmetric thresholds.
Market/unit/economics must match verified Binance/Bybit cash Spot long inputs.
Stops/targets anchor to signal close, trigger market fills, and use explicit gap
and ambiguity policies. Every Phase-8 rule/cost/risk/acceptance parameter is supplied.

Other operators/features, multiple features, asymmetric rules, contracts/margin,
implicit sizing, LIMIT targets, unknown required data or execution capabilities
return BLOCKED with named reasons. They are not approximated, translated into code
or guessed. Supporting them requires a new reviewed compiler/runtime version.
The artifact format reserves exact representations, not implementation promises.

StrategyDataset contains only canonical events, finer bars, labels, instrument
record/rules and evaluation boundary. Artifact parameters own compilation;
dataset files contain no competing strategy or execution defaults.
Compilation hashes bind the complete dataset/economics, artifact, preview, compiler,
audits and compiled input.

## Failure Rulebook before execution

Compiler preflight checks market/timeframe/spec/rule provenance, required features,
supported economics/capabilities, Phase-8 dataset quality/continuity/alignment,
future specs, receipt ordering and next-open availability. Warmup plus a next open
must exist. Finer-bar reconstruction, prefix lookahead and recursive EMA
contamination checks run before a backtest can start.
Unsupported or invalid preflight gives BLOCKED with no compiled input/backtest.
Supported compilation is READY for confirmed research only.
Runtime recomputes compilation instead of trusting a forged READY DTO.

Backtest execution reuses Phase 8 unchanged. Tests compare exact compiled inputs,
paper economics and full backtest results for SMA/EMA. Same-bar ambiguity remains
INCONCLUSIVE; all Phase-8 model assumptions, bounds and unsupported economics remain.

## Explicit confirmation and generated drafts

Both structured artifacts and GENERATED_DRAFT artifacts require an explicit
PreviewConfirmation before this DSL runtime executes. It binds strategy ID, exact
preview hash, dataset compilation ID, reviewer attribution and UTC timestamp.
No confirmation or a stale/mismatched receipt produces BLOCKED with no backtest.
Embedded approvals are forbidden extra fields.

The CLI only creates a receipt when the caller explicitly supplies the displayed
preview hash and reviewer. Journal execution requires confirmation after durable
preview registration and before runtime. No natural-language/LLM translator is
implemented. A future translator may propose GENERATED_DRAFT JSON only; its output
must pass this same parser, preview, audit and external confirmation boundary.
The draft cannot carry its own permission or execute code.

Receipts are local review attestations, not authenticated proof of a human identity.
Origin, reviewer, clock and code SHA are supplied claims. Future UI/provider
integrations need their own authenticated review authority. None of these local
records can authorize paper deployment or live trading.

## Lifecycle and durable research lineage

All lifecycle names are recognized: CANDIDATE / TESTING / VERIFIED / PAPER /
LIVE_ELIGIBLE / PARKED / REJECTED / BLOCKED. On this path, only CANDIDATE/TESTING
may research-run. Initial higher-state claims remain metadata and are BLOCKED.

Append-only explicit lifecycle requests carry prior state/entry, reason, evidence
and deployment_permission=false. Supported transitions are candidate to testing
or parking/rejection/blocking, testing to parking/rejection/blocking, parked to
candidate/rejected/blocked and blocked to candidate/rejected.
VERIFIED/PAPER/LIVE_ELIGIBLE promotion and rejected/higher-state restoration are
unavailable pending future governance authority.

Published key/version pairs cannot be redefined; changed artifacts need a version
bump. Within one journal, lifecycle state applies to the same immutable strategy ID across
experiments, so a fresh experiment cannot reset a BLOCKED strategy.
Backtest outcomes do not auto-change lifecycle.

ResearchJournal stores artifact + exact preview + complete dataset, lifecycle
records and compilation/confirmation/result lineage. Registered protocol, commit,
costs, spec/source provenance and historical timestamps must match.
Result receipts bind the current lifecycle entry. Resuming identical research keeps
a new journal context while retaining the same underlying deterministic result;
repeated computation does not become independent evidence.
record_strategy links BLOCKED/FAIL/INCONCLUSIVE to Failure Memory as
BLOCKED/REJECTED/PARKED respectively. Exact retries are idempotent; final records
are never overwritten. Experiment/family duplicate warnings remain available.
Replay rebuilds every step and rejects even re-signed altered results/lifecycle
claims. Strategy outcomes never update prospective lane reliability.

## Offline commands

```bash
python -m vision strategy-preview tests/fixtures/strategies/close_trend_v1.json
python -m vision strategy-audit tests/fixtures/strategies/close_trend_v1.json tests/fixtures/strategies/canonical_dataset.json
python -m vision strategy-run tests/fixtures/strategies/close_trend_v1.json tests/fixtures/strategies/canonical_dataset.json data/blocked-review.json
python -m vision strategy-replay tests/fixtures/strategies/confirmed_run.json
python -m vision research-replay tests/fixtures/strategies/research_journal.json
```

The third command lacks confirmation and deliberately records BLOCKED. To run
reviewed research, use a new output path and pass `--confirm-preview <displayed-hash>
--reviewer <local-review-reference>`. Outputs refuse overwrite. Model computation
returns exit 0 with a status callers must inspect; invalid inputs exit 2 labelled
Strategy BLOCKED. Execution flags are checked before I/O. The synthetic confirmed
fixture remains INCONCLUSIVE and its receipt is explicitly a synthetic review.
Limits: 100 KB artifacts, 5 MB dataset JSON, 500 bars and Phase-7/8 storage/model bounds.
