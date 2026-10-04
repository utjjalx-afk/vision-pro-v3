import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from vision.analysis.contracts import Availability, Bias, Evidence, Lane, LaneAssessment, wire
from vision.analysis.synthesizer.contracts import Direction, SynthesisInput, SynthesisPolicy
from vision.analysis.synthesizer.engine import synthesize
from vision.analysis.synthesizer.reliability import ReliabilityPolicy, ReliabilityState, revision
from vision.analysis.synthesizer.replay import inputs_from_dict, replay
from vision.core.contracts import TradePayload
from vision.core.instruments import (
    CanonicalSymbol,
    InstrumentRecord,
    QuantityUnit,
    SpecProvenance,
    digest,
)
from vision.core.state.portfolio import DataQuality
from vision.intents.models import TradeIntent, candidate
from vision.outcomes.prospective import (
    ForecastCommitment,
    GradedOutcome,
    GradingPolicy,
    ProspectiveLedger,
    validate_history,
)


@pytest.fixture
def factory(now, market_event, binance_instrument):
    def quality(at, state="healthy", divergent=False):
        return DataQuality(
            at, ((market_event.instrument_id, state),), divergent, "prospective-test"
        )

    def event(at, identity, price="100", source="binance.spot", epoch=0):
        return replace(
            market_event,
            event_id=identity,
            source_ts=at,
            received_ts=at,
            source=source,
            source_epoch=epoch,
            sequence=int((at - now).total_seconds()),
            continuity="contiguous",
            payload=TradePayload(Decimal(price), Decimal(1)),
        )

    def assessment(lane, at, score="0.8", conf="1", fixture=False, source="binance.spot"):
        ev = event(at, f"e-{lane.value}-{at.isoformat()}", source=source)
        score = Decimal(score)
        return LaneAssessment(
            digest({"lane": lane.value, "at": at.isoformat(), "score": str(score)}),
            lane,
            ev.instrument_id,
            at,
            Availability.AVAILABLE,
            Bias.BULLISH if score > 0 else Bias.BEARISH if score < 0 else Bias.NEUTRAL,
            score,
            Decimal(conf),
            (Evidence(ev.event_id, 0, ev.source, at, at, "trade", fixture),),
            (),
            quality(at),
            (),
            digest({"at": at.isoformat()}),
            fixture,
        )

    def history(lane, n=30, wins=None, regime=None, fixture=False, policy=None, score="0.8"):
        policy = policy or GradingPolicy()
        values = []
        for index in range(n):
            at = now + timedelta(seconds=index * 120)
            a = assessment(lane, at, score=score)
            baseline = event(at, f"base-{lane.value}-{index}")
            c = ForecastCommitment(a, baseline, at, policy, regime, fixture)
            end = c.target_at
            win = index < (n if wins is None else wins)
            up = win == (Decimal(score) > 0)
            terminal = event(end, f"end-{lane.value}-{index}", "102" if up else "98")
            values.append(GradedOutcome(c, terminal, end, quality(end)))
        return tuple(values)

    def inputs(outcomes=(), scores=("0.8", "0.8"), confidence=("1", "1"), **changes):
        at = now + timedelta(hours=2)
        record = InstrumentRecord(
            binance_instrument,
            CanonicalSymbol(binance_instrument.asset_class, "BTC", "USDT"),
            QuantityUnit.BASE,
            SpecProvenance("binance.spot", "fixture", at, digest({"fixture": 1}), "fixture"),
            "linear_quote",
        )
        assessments = tuple(
            assessment(lane, at, scores[i], confidence[i])
            for i, lane in enumerate((Lane.TECHNICAL, Lane.FLOW))
        )
        return replace(SynthesisInput(record, at, assessments, outcomes, quality(at)), **changes)

    return now, event, assessment, quality, history, inputs


def test_default_empty_reliability_waits(factory):
    _, _, _, _, _, inputs = factory
    result = synthesize(inputs())
    assert result.direction is Direction.WAIT and candidate(result) is None
    assert result.score is result.confidence is None
    assert all(
        c.exclusion == "UNPROVEN" and c.weight is c.signed_contribution is None
        for c in result.contributions
    )


@pytest.mark.parametrize("samples", [0, 1, 5, 29])
def test_lucky_small_samples_unproven(factory, samples):
    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL, samples) + history(Lane.FLOW, samples))
    result = synthesize(data)
    assert result.direction is Direction.WAIT
    assert all(c.reliability.state is ReliabilityState.UNPROVEN for c in result.contributions)


@pytest.mark.parametrize("score,direction", [("0.8", Direction.LONG), ("-0.8", Direction.SHORT)])
def test_eligible_synthesis_and_unsized_candidate(factory, score, direction):
    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW), scores=(score, score))
    result = synthesize(data)
    assert result.direction is direction
    assert result.score == Decimal(score) and result.confidence == Decimal("0.6")
    intent = candidate(result)
    assert intent.direction is direction and intent.candidate_only
    assert intent.decision_id == result.decision_id
    assert intent.symbol == "crypto:BTC/USDT"
    assert intent == candidate(result)
    assert "risk_veto" in intent.invalidation_conditions
    assert not {"quantity", "lot_size", "order_id", "stop_loss", "size"} & {
        f.name for f in fields(TradeIntent)
    }


def test_neutral_shrinkage_and_threshold_exact_vector(factory):
    now, _, _, _, history, _ = factory
    outcomes = history(Lane.TECHNICAL, 10)
    policy = ReliabilityPolicy(minimum_samples=10)
    r = revision(
        outcomes,
        Lane.TECHNICAL,
        outcomes[0].terminal.instrument_id,
        now + timedelta(hours=2),
        policy=policy,
    )
    assert r.state is ReliabilityState.ELIGIBLE and r.samples == 10
    with localcontext() as c:
        c.prec = 256
        assert r.posterior_success == Decimal(20) / Decimal(30)
        assert r.eligible_reliability == 2 * r.posterior_success - 1
    assert r.eligible_reliability < Decimal("0.34")


@pytest.mark.parametrize("wins", [0, 10, 15])
def test_chance_or_bad_grades_no_support(factory, wins):
    now, _, _, _, history, _ = factory
    r = revision(
        history(Lane.TECHNICAL, wins=wins),
        Lane.TECHNICAL,
        "BINANCE:SPOT:BTCUSDT",
        now + timedelta(hours=2),
    )
    assert r.state is ReliabilityState.NO_SUPPORT and r.eligible_reliability is None


def test_fixture_outcomes_can_never_activate_reliability(factory):
    _, _, _, _, history, inputs = factory
    result = synthesize(
        inputs(history(Lane.TECHNICAL, fixture=True) + history(Lane.FLOW, fixture=True))
    )
    assert result.direction is Direction.WAIT
    assert all(c.reliability.samples == 0 for c in result.contributions)


def test_regime_separate_sample_gate_no_global_fallback(factory):
    _, _, _, _, history, inputs = factory
    outcomes = history(Lane.TECHNICAL, regime="trend") + history(Lane.FLOW, regime="trend")
    assert synthesize(inputs(outcomes)).direction is Direction.LONG
    assert synthesize(inputs(outcomes, regime="trend")).direction is Direction.WAIT
    assert synthesize(inputs(outcomes, regime="range")).direction is Direction.WAIT
    result = synthesize(
        inputs(
            outcomes,
            regime="trend",
            reliability_policy=ReliabilityPolicy(regime_minimum_samples=30),
        )
    )
    assert result.direction is Direction.LONG


def test_grading_protocol_is_pinned(factory):
    _, _, _, _, history, inputs = factory
    alternate = GradingPolicy(horizon=timedelta(seconds=30))
    result = synthesize(
        inputs(history(Lane.TECHNICAL, policy=alternate) + history(Lane.FLOW, policy=alternate))
    )
    assert result.direction is Direction.WAIT
    assert all(c.reliability.samples == 0 for c in result.contributions)


def test_unavailable_is_no_vote_not_neutral(factory):
    now, _, assessment, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    unavailable = replace(
        assessment(Lane.MACRO, data.as_of),
        status=Availability.UNAVAILABLE,
        bias=None,
        score=None,
        confidence=None,
        reasons=("MISSING_DATA",),
    )
    first = synthesize(data)
    second = synthesize(replace(data, assessments=(*data.assessments, unavailable)))
    assert (
        second.direction is first.direction
        and second.score == first.score
        and second.confidence == first.confidence
    )
    assert (
        next(c for c in second.contributions if c.lane_id == unavailable.assessment_id).exclusion
        == "UNAVAILABLE"
    )
    assert len(second.contributions) == 3


@pytest.mark.parametrize(
    "scores,confidence,reason",
    [
        (("0.8", "-0.8"), ("1", "1"), "DISAGREEMENT"),
        (("0.05", "0.05"), ("1", "1"), "LOW_DIRECTIONAL_SUPPORT"),
        (("0.8", "0.8"), ("0.01", "0.01"), "LOW_SUPPORT"),
        (("0.8", "0.8"), ("1", "0.2"), "LANE_DOMINANCE"),
        (("0.8", "0.01"), ("1", "1"), "LANE_DOMINANCE"),
        (("0.8", "0"), ("1", "1"), "LANE_DOMINANCE"),
    ],
)
def test_fail_closed_support_guards(factory, scores, confidence, reason):
    _, _, _, _, history, inputs = factory
    result = synthesize(
        inputs(history(Lane.TECHNICAL) + history(Lane.FLOW), scores=scores, confidence=confidence)
    )
    assert result.direction is Direction.WAIT and reason in result.reasons
    assert candidate(result) is None


@pytest.mark.parametrize("state", ["degraded", "stale", "gap", "disconnected", "warming_up"])
def test_poor_quality_never_emits_intent(factory, state):
    _, _, _, quality, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    result = synthesize(replace(data, quality=quality(data.as_of, state)))
    assert result.direction is Direction.WAIT and "POOR_DATA_QUALITY" in result.reasons


def test_divergence_and_stale_lane_spec(factory):
    _, _, _, quality, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    assert (
        synthesize(replace(data, quality=quality(data.as_of, divergent=True))).direction
        is Direction.WAIT
    )
    assert (
        "STALE_ASSESSMENTS"
        in synthesize(replace(data, as_of=data.as_of + timedelta(seconds=6))).reasons
    )
    stale_record = replace(
        data.record,
        provenance=replace(data.record.provenance, observed_at=data.as_of - timedelta(days=2)),
    )
    assert "STALE_INSTRUMENT_SPEC" in synthesize(replace(data, record=stale_record)).reasons
    poor_lane = replace(data.assessments[0], data_quality=quality(data.as_of, "gap"))
    assert (
        "POOR_DATA_QUALITY"
        in synthesize(replace(data, assessments=(poor_lane, data.assessments[1]))).reasons
    )


def test_zero_confidence_and_fixture_lane_excluded(factory):
    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW), confidence=("0", "1"))
    assert synthesize(data).direction is Direction.WAIT
    assert any(c.exclusion == "ZERO_CONFIDENCE" for c in synthesize(data).contributions)
    a = replace(data.assessments[0], confidence=Decimal(1), fixture_only=True)
    assert any(
        c.exclusion == "FIXTURE_ONLY"
        for c in synthesize(replace(data, assessments=(a, data.assessments[1]))).contributions
    )


def test_prospective_ledger_register_then_grade_idempotent(factory):
    now, event, assessment, quality, _, _ = factory
    clock = [now]
    ledger = ProspectiveLedger(clock=lambda: clock[0])
    a = assessment(Lane.TECHNICAL, now)
    baseline = event(now, "baseline")
    c = ledger.register(a, baseline)
    assert ledger.register(a, baseline) == c
    assert ledger.snapshot(now) == ()
    clock[0] = c.target_at
    terminal = event(clock[0], "terminal", "102")
    g = ledger.grade(c.commitment_id, terminal, quality(clock[0]))
    assert g.reward == 1 and g.benchmark_return == Decimal("0.02")
    assert ledger.grade(c.commitment_id, terminal, quality(clock[0])) == g
    assert ledger.snapshot(clock[0]) == (g,)
    assert ledger.snapshot(now) == ()
    with pytest.raises(ValueError):
        ledger.grade(
            c.commitment_id,
            replace(terminal, payload=TradePayload(Decimal(99), Decimal(1))),
            quality(clock[0]),
        )


@pytest.mark.parametrize(
    "price,reward", [("102", "1"), ("98", "0"), ("100", "0.5"), ("100.1", "0.5")]
)
def test_directional_grade_known_vectors(factory, price, reward):
    now, event, assessment, quality, _, _ = factory
    c = ForecastCommitment(
        assessment(Lane.TECHNICAL, now), event(now, "base"), now, GradingPolicy()
    )
    g = GradedOutcome(c, event(c.target_at, "end", price), c.target_at, quality(c.target_at))
    assert g.reward == Decimal(reward)


@pytest.mark.parametrize(
    "bad",
    [
        "retroactive",
        "early",
        "late",
        "epoch",
        "provider",
        "quality",
        "backfill",
        "unknown",
        "future_grade",
    ],
)
def test_ineligible_grade_records_reject(factory, bad):
    now, event, assessment, quality, _, _ = factory
    c = ForecastCommitment(
        assessment(Lane.TECHNICAL, now), event(now, "base"), now, GradingPolicy()
    )
    terminal = event(c.target_at, "end", "102")
    q = quality(c.target_at)
    with pytest.raises(ValueError):
        if bad == "retroactive":
            replace(c, registered_at=c.target_at)
        elif bad == "early":
            GradedOutcome(c, event(now + timedelta(seconds=30), "end"), c.target_at, q)
        elif bad == "late":
            GradedOutcome(
                c,
                event(c.target_at + timedelta(seconds=10), "end"),
                c.target_at + timedelta(seconds=10),
                quality(c.target_at + timedelta(seconds=10)),
            )
        elif bad == "epoch":
            GradedOutcome(c, replace(terminal, source_epoch=1), c.target_at, q)
        elif bad == "provider":
            GradedOutcome(c, replace(terminal, source="bybit.spot"), c.target_at, q)
        elif bad == "quality":
            GradedOutcome(c, terminal, c.target_at, quality(c.target_at, divergent=True))
        elif bad == "backfill":
            GradedOutcome(c, replace(terminal, delivery_kind="backfill"), c.target_at, q)
        elif bad == "unknown":
            ProspectiveLedger(clock=lambda: c.target_at).grade("unregistered", terminal, q)
        else:
            GradedOutcome(c, terminal, now, q)


def test_ledger_rejects_overlapping_and_regressing_registration(factory):
    now, event, assessment, _, _, _ = factory
    clock = [now]
    ledger = ProspectiveLedger(clock=lambda: clock[0])
    ledger.register(assessment(Lane.TECHNICAL, now), event(now, "first"))
    clock[0] = now + timedelta(seconds=1)
    with pytest.raises(ValueError):
        ledger.register(assessment(Lane.TECHNICAL, clock[0]), event(clock[0], "second"))
    clock[0] = now - timedelta(seconds=1)
    with pytest.raises(ValueError):
        ledger.register(assessment(Lane.FLOW, now), event(now, "flow"))


def test_samples_cannot_be_duplicated_or_reordered(factory):
    _, _, _, _, history, _ = factory
    samples = history(Lane.TECHNICAL, 2)
    with pytest.raises(ValueError):
        validate_history((samples[0], samples[0]))
    with pytest.raises(ValueError):
        validate_history(tuple(reversed(samples)))


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_lane",
        "unsynchronized",
        "future_assessment",
        "future_outcome",
        "mutable",
        "different_instrument",
    ],
)
def test_bad_synthesis_input_rejected(factory, change):
    _, _, _, _, history, inputs = factory
    data = inputs()
    with pytest.raises(ValueError):
        if change == "duplicate_lane":
            replace(data, assessments=(data.assessments[0], data.assessments[0]))
        elif change == "unsynchronized":
            replace(
                data,
                assessments=(
                    replace(data.assessments[0], context_lineage="other"),
                    data.assessments[1],
                ),
            )
        elif change == "future_assessment":
            replace(data, as_of=data.as_of - timedelta(seconds=1))
        elif change == "future_outcome":
            replace(data, outcomes=history(Lane.TECHNICAL, 100))
        elif change == "mutable":
            replace(data, assessments=list(data.assessments))
        else:
            replace(data, assessments=(replace(data.assessments[0], instrument_id="OTHER"),))


def test_exact_replay_and_immutable_lineage(factory):
    from decimal import Inexact, Rounded

    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    expected = synthesize(data)
    payload = json.loads(json.dumps(wire(data)))
    with localcontext() as c:
        c.prec = 2
        c.traps[Inexact] = c.traps[Rounded] = True
        actual = replay(payload, expected_lineage=data.lineage_id)
    assert actual == expected and candidate(actual) == candidate(expected)
    assert inputs_from_dict(payload) == data
    assert all(c.evidence_hash and c.reliability.revision_id for c in expected.contributions)
    with pytest.raises(FrozenInstanceError):
        expected.direction = Direction.WAIT
    with pytest.raises(FrozenInstanceError):
        candidate(expected).confidence = Decimal(1)
    with pytest.raises(ValueError):
        replay(payload, expected_lineage="wrong")


@pytest.mark.parametrize("value", [None, 3, {}, {"weights": [1, 1]}])
def test_invalid_replay_shape(value):
    with pytest.raises(ValueError):
        replay(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_samples": 1},
        {"minimum_samples": True},
        {"prior_strength": Decimal(0)},
        {"regime_minimum_samples": 9},
    ],
)
def test_policy_prevents_few_lucky_wins(kwargs):
    with pytest.raises(ValueError):
        ReliabilityPolicy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_lanes": 1},
        {"minimum_support": Decimal(0)},
        {"maximum_lane_share": Decimal(1)},
        {"minimum_direction": Decimal(0)},
    ],
)
def test_synthesis_policy_cannot_disable_core_guards(kwargs):
    with pytest.raises(ValueError):
        SynthesisPolicy(**kwargs)


def test_committed_unproven_fixture_never_fabricates_edge():
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).parent / "fixtures/synthesis/unproven_input.json").read_text(
            encoding="utf-8"
        )
    )
    result = replay(payload)
    assert result == replay(payload)
    assert result.direction is Direction.WAIT and candidate(result) is None
    assert all(c.reliability.state is ReliabilityState.UNPROVEN for c in result.contributions)
    assert len(result.contributions) == 4


def test_no_llm_execution_or_provider_dependencies_in_decision_path():
    import ast
    from pathlib import Path

    import vision.analysis.synthesizer
    import vision.intents
    import vision.outcomes

    forbidden = (
        "vision.execution",
        "vision.orders",
        "vision.risk",
        "vision.market_data.adapters",
        "openai",
        "requests",
        "urllib",
        "websockets",
    )
    for module in (vision.analysis.synthesizer, vision.intents, vision.outcomes):
        for path in Path(module.__file__).parent.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                assert all(not name.startswith(forbidden) for name in names), path.name


def test_intent_cannot_be_submitted_as_paper_order(factory):
    from vision.execution.paper.broker import PaperBroker

    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    broker = PaperBroker("USDT", Decimal(1000), data.as_of)
    with pytest.raises(ValueError):
        broker.submit(candidate(synthesize(data)))


@pytest.mark.parametrize(
    "mutation", ["extra", "float", "epoch", "future", "retroactive", "reward", "mutable", "null"]
)
def test_replay_tampering_rejected(factory, mutation):
    _, _, _, _, history, inputs = factory
    payload = wire(inputs(history(Lane.TECHNICAL, 1)))
    if mutation == "extra":
        payload["weights"] = [1, 1]
    elif mutation == "float":
        payload["assessments"][0]["score"] = 0.8
    elif mutation == "epoch":
        payload["outcomes"][0]["terminal"]["source_epoch"] = 4
    elif mutation == "future":
        payload["as_of"] = "2025-01-01T00:00:00Z"
    elif mutation == "retroactive":
        payload["outcomes"][0]["commitment"]["registered_at"] = payload["outcomes"][0]["graded_at"]
    elif mutation == "reward":
        payload["outcomes"][0]["reward"] = "1"
    elif mutation == "mutable":
        payload["outcomes"] = {}
    else:
        payload["quality"] = None
    with pytest.raises(ValueError):
        replay(payload)


def test_reliability_snapshot_never_leaks_future_grades(factory):
    now, _, _, _, history, _ = factory
    samples = history(Lane.TECHNICAL)
    r = revision(samples, Lane.TECHNICAL, "BINANCE:SPOT:BTCUSDT", now)
    assert r.samples == 0 and r.state is ReliabilityState.UNPROVEN
    r = revision(samples, Lane.TECHNICAL, "BINANCE:SPOT:ETHUSDT", now + timedelta(hours=2))
    assert r.samples == 0


def test_registration_identity_collision_and_synthetic_designation(factory):
    now, event, assessment, _, _, _ = factory
    ledger = ProspectiveLedger(clock=lambda: now)
    a = assessment(Lane.TECHNICAL, now)
    b = event(now, "first")
    ledger.register(a, b)
    with pytest.raises(ValueError):
        ledger.register(a, replace(b, event_id="changed"))
    s = assessment(Lane.FLOW, now, source="synthetic.market")
    with pytest.raises(ValueError):
        ForecastCommitment(s, event(now, "s", source="synthetic.market"), now, GradingPolicy())
    c = ForecastCommitment(
        s, event(now, "s", source="synthetic.market"), now, GradingPolicy(), fixture_only=True
    )
    assert c.fixture_only


def test_receipt_time_grades_and_fixture_lanes_cannot_be_prospective_evidence(factory):
    from vision.core.contracts import TimestampBasis

    now, event, assessment, _, _, _ = factory
    a = assessment(Lane.TECHNICAL, now)
    with pytest.raises(ValueError):
        ForecastCommitment(
            a,
            replace(event(now, "receipt"), timestamp_basis=TimestampBasis.RECEIPT),
            now,
            GradingPolicy(),
        )
    with pytest.raises(ValueError):
        ForecastCommitment(
            replace(a, fixture_only=True),
            event(now, "fixture"),
            now,
            GradingPolicy(),
            fixture_only=True,
        )


def test_quality_factor_explicit_and_unhealthy_has_no_contribution(factory):
    _, _, _, quality, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    assert all(c.data_quality_factor == 1 for c in synthesize(data).contributions)
    result = synthesize(replace(data, quality=quality(data.as_of, "gap")))
    assert all(
        c.data_quality_factor is c.weight is c.signed_contribution is None
        for c in result.contributions
    )


def test_offline_synthesis_cli(factory, tmp_path):
    import os
    import subprocess
    import sys

    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    path = tmp_path / "synthesis.json"
    path.write_text(json.dumps(wire(data)), encoding="utf-8")
    env = {
        **os.environ,
        **dict.fromkeys(
            (
                "VISION_LIVE_TRADING_ENABLED",
                "VISION_MT5_EXECUTION_ENABLED",
                "VISION_PAPER_TRADING_ENABLED",
                "VISION_AGENTS_ENABLED",
            ),
            "false",
        ),
    }
    result = subprocess.run(
        [sys.executable, "-m", "vision", "synthesis-replay", str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    expected = synthesize(data)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "decision": expected.to_dict(),
        "intent": candidate(expected).to_dict(),
    }
    path.write_text("null", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "vision", "synthesis-replay", str(path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "Synthesis replay rejected" in result.stderr
    assert "Traceback" not in result.stderr


def test_market_source_epoch_change_waits_with_proven_reliability(factory):
    _, _, _, _, history, inputs = factory
    data = inputs(history(Lane.TECHNICAL) + history(Lane.FLOW))
    flow = data.assessments[1]
    changed = replace(flow, evidence=tuple(replace(e, source_epoch=1) for e in flow.evidence))
    result = synthesize(replace(data, assessments=(data.assessments[0], changed)))
    assert result.direction is Direction.WAIT and "SOURCE_TRANSITION" in result.reasons
    assert candidate(result) is None


def test_supplemental_provider_is_preserved_without_rebinding_price_benchmark(factory):
    now, event, assessment, quality, _, _ = factory
    a = assessment(Lane.FLOW, now)
    supplementary = Evidence(
        "public-observation", 10, "public.supplemental", now, now, "funding", False
    )
    a = replace(a, evidence=(*a.evidence, supplementary))
    c = ForecastCommitment(a, event(now, "base"), now, GradingPolicy())
    g = GradedOutcome(c, event(c.target_at, "end", "102"), c.target_at, quality(c.target_at))
    assert g.reward == 1
    assert g.commitment.assessment.evidence[-1].provider == "public.supplemental"
    assert g.terminal.source == g.commitment.baseline.source == "binance.spot"


def test_same_terminal_or_baseline_identity_cannot_inflate_samples(factory):
    _, _, _, _, history, _ = factory
    first, second = history(Lane.TECHNICAL, 2)
    duplicate_terminal = replace(
        second, terminal=replace(second.terminal, event_id=first.terminal.event_id)
    )
    with pytest.raises(ValueError):
        validate_history((first, duplicate_terminal))
    duplicate_baseline = replace(
        second,
        commitment=replace(
            second.commitment,
            baseline=replace(
                second.commitment.baseline, event_id=first.commitment.baseline.event_id
            ),
        ),
    )
    with pytest.raises(ValueError):
        validate_history((first, duplicate_baseline))
