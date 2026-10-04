"""Durable research services with transactional domain checks and separate evidence paths."""

from datetime import UTC, datetime

from vision.analysis.contracts import wire
from vision.analysis.synthesizer.engine import synthesize
from vision.analysis.synthesizer.replay import assessment, grading_policy, outcome
from vision.core.codec import decimal_string, event_from_dict, utc_string
from vision.core.contracts import _identifier
from vision.core.instruments import digest
from vision.core.state.portfolio import Side
from vision.core.state.replay import shape
from vision.execution.paper.broker import PaperBroker
from vision.execution.paper.models import PaperCosts, PaperMode
from vision.failure_memory.models import FailureRecord
from vision.intents.models import candidate
from vision.journal.models import ExitDeclaration, Experiment, OutcomeState
from vision.journal.repository import Pending, canonical
from vision.outcomes.paper import grade
from vision.outcomes.prospective import ProspectiveLedger


def pending(kind, experiment, key, value):
    return Pending(kind, experiment, key, canonical(value))


def find(entries, entry_id, kind=None):
    result = next((e for e in entries if e.entry_id == entry_id), None)
    if result is None or kind is not None and result.kind != kind:
        raise ValueError("Required journal reference is absent/wrong kind")
    return result


def experiment_from_dict(value):
    data = shape(value, Experiment)
    costs = shape(data["costs"], PaperCosts)
    costs["mode"] = PaperMode(costs["mode"])
    for key in ("spread_bps", "slippage_bps", "commission_bps"):
        costs[key] = decimal_string(costs[key])
    if not isinstance(data["spec_revisions"], list) or not isinstance(
        data["source_provenance"], list
    ):
        raise ValueError("Explicit metadata arrays required")
    data["spec_revisions"] = tuple(data["spec_revisions"])
    data["source_provenance"] = tuple(tuple(pair) for pair in data["source_provenance"])
    data["costs"] = PaperCosts(**costs)
    data["created_at"] = utc_string(data["created_at"])
    return Experiment(**data)


def experiment(entries, identity):
    entry = next(
        (e for e in entries if e.kind == "experiment" and e.experiment_id == identity), None
    )
    if entry is None:
        raise ValueError("Registered experiment required")
    return experiment_from_dict(entry.payload["experiment"])


def ledger_from_entries(entries):
    time = [None]
    ledger = ProspectiveLedger(clock=lambda: time[0])
    for entry in entries:
        time[0] = entry.recorded_at
        if entry.kind == "prospective_registration":
            data = entry.payload["commitment"]
            c = ledger.register(
                assessment(data["assessment"]),
                event_from_dict(data["baseline"]),
                policy=grading_policy(data["policy"]),
                regime=data["regime"],
                fixture_only=data["fixture_only"],
            )
            if wire(c) != data or c.commitment_id != entry.key:
                raise ValueError("Durable registration receipt mismatch")
        elif entry.kind == "directional_grade":
            g = outcome(entry.payload["outcome"])
            result = ledger.grade(g.commitment.commitment_id, g.terminal, g.quality)
            if result != g or g.outcome_id != entry.key:
                raise ValueError("Durable grade lacks an exact prospective registration")
    return ledger


def declaration_from_dict(value):
    data = shape(value, ExitDeclaration)
    data["state"] = OutcomeState(data["state"])
    if not isinstance(data["evidence_ids"], list):
        raise ValueError("Exit evidence array required")
    data["evidence_ids"] = tuple(data["evidence_ids"])
    if data["target_price"] is not None:
        data["target_price"] = decimal_string(data["target_price"])
    if data["expires_at"] is not None:
        data["expires_at"] = utc_string(data["expires_at"])
    return ExitDeclaration(**data)


class ResearchJournal:
    def __init__(self, repository, *, clock=lambda: datetime.now(UTC), _replaying=False):
        self.repository, self.clock = repository, clock
        self._replaying = _replaying
        self._verified_entries = ()

    def _transact(self, at, build):
        def checked(entries):
            if not self._replaying and entries != self._verified_entries:
                from vision.journal.replay import replay_entries

                replay_entries(entries)
                self._verified_entries = entries
            return build(entries)

        entry = self.repository.transact(at, checked)
        if entry.sequence == len(self._verified_entries) + 1:
            self._verified_entries += (entry,)
        return entry

    def open_experiment(self, item):
        if not isinstance(item, Experiment):
            raise ValueError("Experiment required")
        at = self.clock()

        def build(entries):
            if item.created_at != at:
                # Exact retry remains idempotent; new registrations cannot backdate metadata.
                prior = next(
                    (
                        e
                        for e in entries
                        if e.kind == "experiment" and e.experiment_id == item.experiment_id
                    ),
                    None,
                )
                if prior is None or prior.payload["experiment"] != wire(item):
                    raise ValueError(
                        "New experiment metadata must be registered at the journal clock"
                    )
            prior = next(
                (
                    e
                    for e in entries
                    if e.kind == "experiment" and e.experiment_id == item.experiment_id
                ),
                None,
            )
            if prior is not None:
                if prior.payload["experiment"] != wire(item):
                    raise ValueError("Experiment metadata cannot be overwritten")
                return pending("experiment", item.experiment_id, item.experiment_id, prior.payload)
            existing = [
                experiment_from_dict(e.payload["experiment"])
                for e in entries
                if e.kind == "experiment"
            ]
            warnings = []
            if any(e.fingerprint == item.fingerprint for e in existing):
                warnings.append("DUPLICATE_EXPERIMENT")
            if any(e.family_id == item.family_id for e in existing):
                warnings.append("FAMILY_PSEUDO_REPLICATION")
            return pending(
                "experiment",
                item.experiment_id,
                item.experiment_id,
                {
                    "experiment": wire(item),
                    "config_hash": item.config_hash,
                    "fingerprint": item.fingerprint,
                    "warnings": warnings,
                },
            )

        return self._transact(at, build)

    def register_forecast(
        self, experiment_id, item, baseline, *, policy=None, regime=None, fixture_only=False
    ):
        at = self.clock()

        def build(entries):
            metadata = experiment(entries, experiment_id)
            if (
                regime != metadata.regime
                or (baseline.source, baseline.source_epoch) not in metadata.source_provenance
            ):
                raise ValueError("Forecast must match registered experiment provenance/regime")
            for entry in entries:
                if (
                    entry.kind == "prospective_registration"
                    and entry.payload["commitment"]["assessment"]["assessment_id"]
                    == item.assessment_id
                    and entry.experiment_id != experiment_id
                ):
                    raise ValueError(
                        "Forecast cannot be prospectively registered twice across experiments"
                    )
            ledger = ledger_from_entries(entries)
            ledger.clock = lambda: at
            c = ledger.register(
                item, baseline, policy=policy, regime=regime, fixture_only=fixture_only
            )
            return pending(
                "prospective_registration", experiment_id, c.commitment_id, {"commitment": wire(c)}
            )

        return self._transact(at, build)

    def grade_directional(self, experiment_id, commitment_id, terminal, health):
        at = self.clock()

        def build(entries):
            experiment(entries, experiment_id)
            if not any(
                e.kind == "prospective_registration"
                and e.key == commitment_id
                and e.experiment_id == experiment_id
                for e in entries
            ):
                raise ValueError("Durable prospective registration must precede directional grade")
            ledger = ledger_from_entries(entries)
            ledger.clock = lambda: at
            g = ledger.grade(commitment_id, terminal, health)
            return pending("directional_grade", experiment_id, g.outcome_id, {"outcome": wire(g)})

        return self._transact(at, build)

    def prospective_snapshot(self, as_of):
        entries = self.repository.entries()
        from vision.journal.replay import replay_entries

        replay_entries(entries)
        return ledger_from_entries(entries).snapshot(as_of)

    def signal(self, experiment_id, inputs, *, backfilled=False):
        at = self.clock()

        def build(entries):
            metadata = experiment(entries, experiment_id)
            if (
                type(backfilled) is not bool
                or inputs.as_of > at
                or (not backfilled and at - inputs.as_of > inputs.policy.max_age)
            ):
                raise ValueError("Explicit current/backfilled signal timestamp required")
            if (
                inputs.record.revision not in metadata.spec_revisions
                or inputs.regime != metadata.regime
            ):
                raise ValueError("Signal spec/regime differs from registered experiment")
            for lane in inputs.assessments:
                if any(
                    (e.provider, e.source_epoch) not in metadata.source_provenance
                    for e in lane.evidence
                ):
                    raise ValueError("Signal evidence source was not registered")
            approved = {
                g.outcome_id: g
                for g in ledger_from_entries(entries).snapshot(inputs.market_timestamp)
            }
            if any(approved.get(g.outcome_id) != g for g in inputs.outcomes):
                raise ValueError(
                    "Signal reliability requires durable prospective evidence; "
                    "paper/backfilled grades cannot substitute"
                )
            decision = synthesize(inputs)
            return pending(
                "signal",
                experiment_id,
                decision.decision_id,
                {"input": wire(inputs), "decision": decision.to_dict(), "backfilled": backfilled},
            )

        return self._transact(at, build)

    def intent(self, experiment_id, signal_entry_id):
        at = self.clock()

        def build(entries):
            from vision.analysis.synthesizer.replay import inputs_from_dict

            signal = find(entries, signal_entry_id, "signal")
            if signal.experiment_id != experiment_id or signal.payload["backfilled"]:
                raise ValueError("Current same-experiment signal required for intent")
            item = candidate(synthesize(inputs_from_dict(signal.payload["input"])))
            if item is None or at >= item.expires_at:
                raise ValueError("WAIT/expired signal cannot yield a current candidate")
            return pending(
                "intent",
                experiment_id,
                item.intent_id,
                {"signal_entry_id": signal_entry_id, "intent": item.to_dict()},
            )

        return self._transact(at, build)

    def paper(self, experiment_id, checkpoint, request_id, *, intent_entry_id=None):
        # Freeze caller payload; replay never invokes a provider or writes a broker checkpoint.
        broker = PaperBroker.from_checkpoint(checkpoint)
        frozen = broker.checkpoint()
        at = self.clock()

        def build(entries):
            metadata = experiment(entries, experiment_id)
            result = broker.orders.get(request_id)
            if result is None or result.order.close_position_id is not None or broker.last_at > at:
                raise ValueError("Existing non-future paper entry required")
            if (
                metadata.created_at > result.order.created_at
                or metadata.costs != broker.costs
                or result.order.spec_revision not in metadata.spec_revisions
            ):
                raise ValueError("Paper economics/specs must match prior experiment metadata")
            for e in broker.journal:
                if e["command"]["op"] == "quote":
                    market = event_from_dict(e["command"]["event"])
                    if (market.source, market.source_epoch) not in metadata.source_provenance:
                        raise ValueError("Paper market source/epoch was not registered")
            session = digest(broker.config())
            for entry in entries:
                if entry.kind == "paper" and entry.experiment_id == experiment_id:
                    other = entry.payload
                    if other["session"] == session:
                        old = other["checkpoint"]["journal"]
                        if (
                            frozen["journal"][: len(old)] != old
                            or other["entry_order_id"] == result.order_id
                            and other["intent_entry_id"] != intent_entry_id
                        ):
                            raise ValueError(
                                "Captured paper history/intent association cannot be rewritten"
                            )
                if entry.kind == "paper" and entry.experiment_id != experiment_id:
                    other = entry.payload
                    if other["session"] == session and other["entry_order_id"] == result.order_id:
                        raise ValueError(
                            "A single paper entry cannot masquerade as multiple experiments"
                        )
            if intent_entry_id is not None:
                receipt = find(entries, intent_entry_id, "intent")
                intent = receipt.payload["intent"]
                if (
                    receipt.experiment_id != experiment_id
                    or receipt.recorded_at > result.order.created_at
                    or result.order.side is not Side.LONG
                    or intent["direction"] != "LONG"
                    or intent["instrument_id"] != result.order.instrument_id
                    or intent["spec_revision"] != result.order.spec_revision
                    or not utc_string(intent["timestamp"])
                    <= result.order.created_at
                    < utc_string(intent["expires_at"])
                ):
                    raise ValueError(
                        "Declared intent association does not match actual paper entry"
                    )
            key = digest(
                {
                    "session": session,
                    "request": request_id,
                    "head": broker.journal_head,
                    "intent": intent_entry_id,
                }
            )
            return pending(
                "paper",
                experiment_id,
                key,
                {
                    "checkpoint": frozen,
                    "request_id": request_id,
                    "intent_entry_id": intent_entry_id,
                    "session": session,
                    "entry_order_id": result.order_id,
                },
            )

        return self._transact(at, build)

    def declare_exit(self, experiment_id, paper_receipt_id, item):
        if not isinstance(item, ExitDeclaration):
            raise ValueError("ExitDeclaration required")
        at = self.clock()

        def build(entries):
            receipt = find(entries, paper_receipt_id, "paper")
            if receipt.experiment_id != experiment_id:
                raise ValueError("Same-experiment paper receipt required")
            broker = PaperBroker.from_checkpoint(receipt.payload["checkpoint"])
            position = broker.positions.get(item.position_id)
            if position is None or position.close_fill_id is not None or broker.last_at > at:
                raise ValueError("Exit must be declared for a captured open position")
            key = digest({"position": item.position_id, "request": item.exit_request_id})
            return pending(
                "exit_declaration",
                experiment_id,
                key,
                {"paper_receipt_id": paper_receipt_id, "declaration": wire(item)},
            )

        return self._transact(at, build)

    def finalize(
        self,
        experiment_id,
        paper_receipt_id,
        *,
        declaration_entry_id=None,
        supersedes=None,
        correction_reason=None,
        correction_evidence_ids=(),
    ):
        at = self.clock()

        def build(entries):
            receipt = find(entries, paper_receipt_id, "paper")
            if receipt.experiment_id != experiment_id:
                raise ValueError("Same-experiment paper outcome required")
            data = receipt.payload
            signal_id = intent_id = None
            assessment_ids = ()
            if data["intent_entry_id"] is not None:
                intent = find(entries, data["intent_entry_id"], "intent").payload["intent"]
                intent_id, signal_id = intent["intent_id"], intent["decision_id"]
                signal = find(
                    entries,
                    find(entries, data["intent_entry_id"], "intent").payload["signal_entry_id"],
                    "signal",
                )
                assessment_ids = tuple(
                    a["assessment_id"] for a in signal.payload["input"]["assessments"]
                )
            declaration = None
            if declaration_entry_id is not None:
                e = find(entries, declaration_entry_id, "exit_declaration")
                if e.experiment_id != experiment_id:
                    raise ValueError("Same-experiment exit declaration required")
                declaration = (declaration_from_dict(e.payload["declaration"]), e.recorded_at)
            if supersedes is not None:
                if (
                    not isinstance(correction_reason, str)
                    or not correction_reason.strip()
                    or type(correction_evidence_ids) is not tuple
                    or not 1 <= len(correction_evidence_ids) <= 256
                ):
                    raise ValueError("Superseding correction requires reason and evidence")
                _identifier(correction_reason)
                for evidence_id in correction_evidence_ids:
                    _identifier(evidence_id)
            elif correction_reason is not None or correction_evidence_ids:
                raise ValueError("Correction must explicitly supersede a finalized outcome")
            result = grade(
                data["checkpoint"],
                data["request_id"],
                at=at,
                receipt_id=paper_receipt_id,
                assessments=assessment_ids,
                synthesis_id=signal_id,
                intent_id=intent_id,
                declaration=declaration,
                supersedes=supersedes,
                correction_reason=correction_reason,
                correction_evidence_ids=correction_evidence_ids,
            )
            prior = [
                e
                for e in entries
                if e.kind == "trade_outcome" and e.payload["outcome"]["root_id"] == result.root_id
            ]
            duplicate = next((e for e in prior if e.key == result.outcome_id), None)
            if duplicate is not None:
                return pending("trade_outcome", experiment_id, result.outcome_id, duplicate.payload)
            if prior:
                previous_receipt = find(
                    entries, prior[-1].payload["outcome"]["paper_receipt_id"], "paper"
                )
                previous_history = previous_receipt.payload["checkpoint"]["journal"]
                if data["checkpoint"]["journal"][: len(previous_history)] != previous_history:
                    raise ValueError("Correction cannot regress captured paper history")
            if (
                prior
                and (supersedes != prior[-1].key or prior[-1].experiment_id != experiment_id)
                or not prior
                and supersedes is not None
            ):
                raise ValueError(
                    "Final outcome is immutable; correction must supersede the current tip"
                )
            return pending(
                "trade_outcome",
                experiment_id,
                result.outcome_id,
                {"outcome": wire(result), "declaration_entry_id": declaration_entry_id},
            )

        return self._transact(at, build)

    def failure(self, experiment_id, item):
        if not isinstance(item, FailureRecord):
            raise ValueError("FailureRecord required")
        at = self.clock()

        def build(entries):
            experiment(entries, experiment_id)
            return pending("failure", experiment_id, item.failure_id, {"failure": wire(item)})

        return self._transact(at, build)


class FailureMemory:
    def __init__(self, journal):
        self.journal = journal

    def record(self, experiment_id, item):
        return self.journal.failure(experiment_id, item)

    def records(self, *, family_id=None):
        entries = self.journal.repository.entries()
        from vision.journal.replay import replay_entries

        replay_entries(entries)
        return tuple(
            e
            for e in entries
            if e.kind == "failure"
            and (family_id is None or experiment(entries, e.experiment_id).family_id == family_id)
        )
