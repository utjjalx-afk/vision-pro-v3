# Phase 7: Outcome Journal and Failure Memory

Stacked on Phase 6 at `d4cc404ecfcc87ce17dee89849fcdf34cd7bba6b`.
This is durable local research infrastructure, not strategy validation or trading edge.
No live execution, MT5, autonomous agents, strategy routing or automatic orders are added.

## Three separate evidence paths

1. The Phase-6 prospective directional grader updates eligible lane reliability only.
2. The paper outcome grader describes actual executed paper records only.
3. Failure Memory records research governance decisions only.

Paper profit never updates lane reliability. Correct directional forecasts never become
executed strategy profit. A journal's signal method accepts reliability outcomes only
when their exact registration and grade already exist durably and were known by the
signal timestamp. The original pure Phase-6 SDK remains available for caller-supplied
research inputs; use ResearchJournal for this durable evidence boundary.

## Durable repository and protocol

`Repository` abstracts immutable entries and transactional append. SQLite uses
BEGIN IMMEDIATE, WAL and synchronous FULL. Domain validation and append run under
the same write transaction; registrations commit before returning a receipt.
Exact retries are idempotent. Identity collisions, clock regression, chain corruption
and invalid domain history fail closed. SQL triggers prohibit UPDATE and DELETE.
PostgreSQL is a future implementation, not included here.

ResearchJournal registers experiment metadata: canonical config and its hash, explicit
Git commit SHA, spec revisions, provider/source epochs, regime and exact paper costs.
Metadata cannot be overwritten. Signal inputs, decisions and candidate intents are
stored with complete canonical evidence. Explicitly backfilled signals are labelled
and cannot produce current intents. Prospective registration and grading reuse the
frozen Phase-6 protocol; a grade without its prior durable registration is rejected.
Fixture/unproven/sample gates remain unchanged.

The local clock and supplied provider records are trusted inputs. Hash chains and
SQL triggers detect consistency violations; they are not authenticated signatures.
A filesystem owner can replace or re-sign the database/export. Retain a separately
trusted head to detect truncation against that known head. SQLite durability depends
on the filesystem/device honoring writes. Authenticated provider receipts, remote
timestamping, encryption and replicated disaster recovery remain future work.

## Paper outcome semantics

The broker exposes a defensive checkpoint and strict offline reconstruction. Journal
paper receipts capture actual checkpoint records; later captures must extend the
earlier prefix. The same order cannot be claimed by multiple experiments. Registered
costs/spec/source provenance must match. The journal never submits an order.

An explicitly linked intent supplies:

`assessment_ids -> synthesis_id -> intent_id -> risk_decision_id -> order_id ->
fill_id -> position_id -> exit_id -> outcome_id`.

Manual simulator inputs retain absent upstream links as null/empty instead of
inventing assessments or intents. An optional linked candidate must be a matching,
unexpired LONG candidate; quantity remains an explicit broker/risk input. This
association does not implement strategy eligibility or automated routing.

* STOP_HIT requires an actual broker STOP/GAP_THROUGH_STOP fill.
* MANUAL_EXIT describes an actual explicit close without another declared purpose.
* TARGET_HIT requires a prior target declaration and actual close fill at/above it.
* EXPIRED requires a prior expiry declaration and actual close at/after expiry.
* SYSTEM_EXIT requires prior reason/evidence and an actual close.
* NO_RESULT describes a rejected or still-open paper request. Realized PnL, fees,
  slippage and R remain null; an open entry's known initial risk can be retained.

Declarations classify an actual close; they do not execute targets, expiry or system
exits. Supported economics remain fully funded cash Spot longs from Phase 4.
Realized PnL uses actual entry/exit fills and both commissions. Fees use those
commissions. Slippage cost measures fill-versus-executable bid/ask quote shortfall
for both legs, including additional spread and tick rounding. It is not an isolated
estimate of the configured slippage parameter and can be negative in PAPER_IDEAL.
R divides actual net PnL by the entry RiskDecision's positive trade_risk, with fixed
Decimal contexts. No inferred multiplier, fabricated cash or assumed conversion.

Finalized outcomes never change. Corrections append reason/evidence and supersede
the current tip; forks and conflicting retries are rejected. The complete chain
remains exported, while replay reports the latest outcome per root once.

## Failure Memory

REJECTED / PARKED / BLOCKED records require a reason, evidence IDs and explicit
validation stage. Exact experiment fingerprints identify duplicate configuration,
commit, spec/source, regime and cost inputs. Declared family reuse emits a
pseudo-replication warning. These warnings are research prompts, not statistical
independence proofs; records do not alter trading or reliability.

## Offline export and replay

```bash
python examples/journal_research.py --database data/research.sqlite --export data/research.json --commit-sha d4cc404ecfcc87ce17dee89849fcdf34cd7bba6b
python -m vision research-export data/research.sqlite data/research-copy.json
python -m vision research-replay data/research.json
python -m vision research-replay tests/fixtures/journal/paper_research.json
```

The example uses fixed synthetic data and must receive an explicit code revision.
Use a new database/output for each demonstration. Local data stays ignored.
Export opens an existing database read-only and refuses to overwrite an output.
Replay verifies schema, hashes and the complete domain history, including broker
checkpoints, synthesis, prospective timing and corrected outcome values, without
providers. Optional `--expected-head` pins a separately retained head.
Replay is verification, not an import path into eligible prospective evidence.
Limits: 10,000 journal entries, 5 MB per payload/checkpoint and 50 MB per export.

Tests cover complete lineage, actual costs/stops, separate evidence paths,
durable registration across restart, concurrent writers, rollback/process crash,
superseding corrections and re-signed semantic tampering. CI runs the full suite
on Windows/Linux Python 3.11/3.12 and inside an offline Docker test container.
