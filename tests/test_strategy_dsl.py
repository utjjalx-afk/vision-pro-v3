import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from vision.analysis.contracts import wire
from vision.journal.models import Experiment
from vision.journal.replay import replay as journal_replay
from vision.journal.repository import SQLiteRepository, canonical, export
from vision.journal.service import FailureMemory, ResearchJournal
from vision.research.backtest import inputs_from_dict, run
from vision.strategies.dsl import Lifecycle, parse, preview
from vision.strategies.governance import record_strategy
from vision.strategies.runtime import (
    compile_strategy,
    confirm_preview,
    dataset_from_dict,
    execute,
    protocol,
)


@pytest.fixture
def strategy():
    root = Path(__file__).parent / "fixtures/strategies"
    artifact = parse((root / "close_trend_v1.json").read_text(encoding="utf-8"))
    dataset = dataset_from_dict(
        json.loads((root / "canonical_dataset.json").read_text(encoding="utf-8"))
    )
    return artifact, dataset


def altered(artifact, path, value=None, *, remove=False):
    data = artifact.value
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    if remove:
        del current[parts[-1]]
    else:
        current[parts[-1]] = value
    return parse(canonical(data))


def approval(compilation, dataset):
    return confirm_preview(
        compilation,
        preview_hash=compilation.preview_hash,
        reviewer="synthetic-human-review",
        at=dataset.events[-1].received_ts,
    )


def test_parser_preview_and_full_phase8_parity(strategy):
    artifact, dataset = strategy
    p = preview(artifact)
    compilation = compile_strategy(artifact, dataset)
    assert compilation.status == "READY" and compilation.reasons == ()
    assert p.text == compilation.preview_text and p.preview_hash == compilation.preview_hash
    assert "ENTER_LONG when close GT trend * 1.001" in p.text
    assert '"commission_bps": "10"' in p.text
    expected = json.loads(
        (Path(__file__).parent / "fixtures/backtest/ambiguous_input.json").read_text()
    )
    assert json.loads(compilation.compiled_json) == expected
    result = execute(compilation, approval(compilation, dataset))
    assert json.loads(result.backtest_json) == run(inputs_from_dict(expected)).to_dict()
    assert result.status == "INCONCLUSIVE"
    with pytest.raises(FrozenInstanceError):
        artifact.artifact_json = "{}"
    copy = artifact.value
    copy["lifecycle"] = "LIVE_ELIGIBLE"
    assert artifact.value["lifecycle"] == "CANDIDATE"


@pytest.mark.parametrize(
    "path",
    [
        "market.timeframe_seconds",
        "market.warmup",
        "market.required_data",
        "features.0.lookback",
        "features.0.recursive_tolerance",
        "entry.condition.multiplier",
        "exit.condition.operator",
        "risk.quantity.value",
        "risk.invalidation.fraction",
        "risk.invalidation.gap_policy",
        "risk.profit_exit.fraction",
        "risk.hard_limits.per_trade_fraction",
        "execution.costs.commission_bps",
        "execution.timing",
        "execution.capabilities",
        "research.commit_sha",
    ],
)
def test_no_silent_parameter_defaults(strategy, path):
    artifact, _ = strategy
    with pytest.raises(ValueError):
        altered(artifact, path, remove=True)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"vision.strategy.v1","schema_version":"vision.strategy.v1"}',
        '{"something":NaN}',
        '{"something":Infinity}',
        "null",
        "[]",
        "{}",
    ],
)
def test_strict_json_rejects_noncanonical_ambiguous_inputs(raw):
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize(
    "path,value",
    [
        ("schema_version", "vision.strategy.v2"),
        ("strategy_version", "latest"),
        ("market.timeframe_seconds", True),
        ("market.warmup", 2.0),
        ("features.0.lookback", 0),
        ("risk.quantity.value", 1),
        ("execution.costs.slippage_bps", "NaN"),
        ("risk.invalidation.fraction", "0"),
        ("research.commit_sha", "local-path"),
        ("origin.generator_id", "unexpected"),
    ],
)
def test_schema_type_bounds_and_origin_fail_closed(strategy, path, value):
    with pytest.raises(ValueError):
        altered(strategy[0], path, value)


@pytest.mark.parametrize(
    "path,value,reason",
    [
        ("features.0.kind", "RSI", "UNSUPPORTED_FEATURE"),
        ("entry.condition.operator", "GTE", "UNSUPPORTED_ENTRY_EXIT"),
        ("exit.condition.multiplier", "0.8", "ASYMMETRIC_OR_UNSUPPORTED_THRESHOLDS"),
        ("market.warmup", 3, "WARMUP_RULE_MISMATCH"),
        ("market.required_data", ["closed_bars", "funding"], "UNSUPPORTED_REQUIRED_DATA"),
        ("market.instrument.quantity_unit", "contracts", "INSTRUMENT_CONSTRAINTS"),
        ("risk.quantity.sizing", "AUTO_OPTIMIZE", "UNSUPPORTED_INVALIDATION_RISK"),
        ("risk.invalidation.anchor", "NEXT_OPEN", "UNSUPPORTED_INVALIDATION_RISK"),
        ("risk.profit_exit.fill", "LIMIT", "UNSUPPORTED_INVALIDATION_RISK"),
        ("execution.mode", "LIVE", "UNSUPPORTED_EXECUTION_SEMANTICS"),
        ("execution.ambiguity", "BEST_PROFIT", "UNSUPPORTED_EXECUTION_SEMANTICS"),
        ("execution.costs.mode", "PAPER_IDEAL", "UNSUPPORTED_EXECUTION_SEMANTICS"),
        (
            "execution.capabilities",
            ["cash_spot_long", "live_orders"],
            "UNSUPPORTED_EXECUTION_SEMANTICS",
        ),
    ],
)
def test_supported_schema_does_not_guess_unsupported_runtime(strategy, path, value, reason):
    artifact, dataset = strategy
    compilation = compile_strategy(altered(artifact, path, value), dataset)
    assert compilation.status == "BLOCKED" and reason in compilation.reasons
    assert compilation.compiled_json is None
    result = execute(compilation)
    assert result.status == "BLOCKED" and result.backtest_json is None


@pytest.mark.parametrize(
    "state",
    [
        s.value
        for s in Lifecycle
        if s
        not in {
            Lifecycle.CANDIDATE,
            Lifecycle.TESTING,
        }
    ],
)
def test_lifecycle_claim_is_not_execution_permission(strategy, state):
    artifact, dataset = strategy
    c = compile_strategy(altered(artifact, "lifecycle", state), dataset)
    assert c.status == "BLOCKED" and "LIFECYCLE_RESEARCH_BLOCKED" in c.reasons


def test_generated_draft_needs_exact_confirmation_and_cannot_self_approve(strategy):
    artifact, dataset = strategy
    generated = altered(
        artifact, "origin", {"kind": "GENERATED_DRAFT", "generator_id": "fixture-model"}
    )
    c = compile_strategy(generated, dataset)
    assert execute(c).status == "BLOCKED"
    assert execute(c).reasons == ("PREVIEW_CONFIRMATION_REQUIRED",)
    with pytest.raises(ValueError):
        confirm_preview(
            c, preview_hash="0" * 64, reviewer="test", at=dataset.events[-1].received_ts
        )
    confirmed = approval(c, dataset)
    assert execute(c, confirmed).status == "INCONCLUSIVE"
    changed = compile_strategy(altered(generated, "research.starting_cash", "20000"), dataset)
    assert execute(changed, confirmed).status == "BLOCKED"
    assert "PREVIEW_CONFIRMATION_MISMATCH" in execute(changed, confirmed).reasons
    with pytest.raises(ValueError):
        altered(generated, "embedded_approval", True)


def test_confirmation_is_bound_to_dataset_and_forged_ready_dto_cannot_execute(strategy):
    artifact, dataset = strategy
    c = compile_strategy(artifact, dataset)
    confirmed = approval(c, dataset)
    last = dataset.events[-1]
    changed_dataset = replace(
        dataset, events=(*dataset.events[:-1], replace(last, event_id="changed"))
    )
    changed = compile_strategy(artifact, changed_dataset)
    assert execute(changed, confirmed).status == "BLOCKED"
    with pytest.raises(ValueError, match="mismatch"):
        execute(
            replace(c, compiled_json=c.compiled_json.replace('"quantity":"1"', '"quantity":"2"')),
            confirmed,
        )


def test_rulebook_preflight_runs_before_backtest(strategy, monkeypatch):
    artifact, dataset = strategy
    events = (*dataset.events[:-1], replace(dataset.events[-1], continuity="unverified"))
    c = compile_strategy(artifact, replace(dataset, events=events))
    assert c.status == "BLOCKED" and "DATA_SOURCE_QUALITY_UNVERIFIED" in c.reasons
    monkeypatch.setattr(
        "vision.strategies.runtime.run",
        lambda _: pytest.fail("Blocked rules must not reach backtest"),
    )
    assert execute(c).backtest_json is None


def test_wrong_timeframe_and_instrument_block_without_rebinding(strategy):
    artifact, dataset = strategy
    assert (
        "DECLARED_TIMEFRAME_MISMATCH"
        in compile_strategy(altered(artifact, "market.timeframe_seconds", 30), dataset).reasons
    )
    assert (
        "INSTRUMENT_CONSTRAINTS"
        in compile_strategy(
            altered(artifact, "market.canonical_symbol", "crypto:ETH/USDT"), dataset
        ).reasons
    )


def test_identity_canonical_order_and_decimal_context_independence(strategy):
    artifact, dataset = strategy
    raw = json.dumps(artifact.value, indent=4)
    assert parse(raw) == artifact
    with localcontext() as c:
        c.prec = 6
        low = compile_strategy(artifact, dataset)
    with localcontext() as c:
        c.prec = 40
        high = compile_strategy(artifact, dataset)
    assert low == high
    changed = altered(artifact, "strategy_version", "1.0.1")
    assert changed.strategy_id != artifact.strategy_id
    assert preview(changed).preview_hash != preview(artifact).preview_hash


@pytest.fixture
def journal(strategy, tmp_path):
    artifact, dataset = strategy
    at = [dataset.events[-1].received_ts]
    repository = SQLiteRepository(tmp_path / "strategies.sqlite")
    service = ResearchJournal(repository, clock=lambda: at[0])
    value = artifact.value
    from vision.strategies.dsl import cost_model

    metadata = Experiment(
        "dsl",
        "family",
        canonical(protocol(artifact, dataset)),
        value["research"]["commit_sha"],
        (dataset.record.revision,),
        (("binance.spot", 0),),
        None,
        cost_model(value["execution"]["costs"]),
        at[0],
    )
    service.open_experiment(metadata)
    service.register_strategy("dsl", artifact, dataset)
    yield service, repository, at, metadata
    repository.close()


def test_durable_preview_restart_runtime_and_separate_reliability(strategy, journal, tmp_path):
    artifact, dataset = strategy
    service, repository, at, _ = journal
    assert journal_replay(export(repository))["strategy_results"] == []
    reopened = SQLiteRepository(tmp_path / "strategies.sqlite")
    recovered = ResearchJournal(reopened, clock=lambda: at[0])
    c = compile_strategy(artifact, dataset)
    entry = record_strategy(recovered, "dsl", artifact, dataset, approval(c, dataset))
    assert entry == record_strategy(recovered, "dsl", artifact, dataset, approval(c, dataset))
    assert FailureMemory(recovered).records()[0].payload["failure"]["status"] == "PARKED"
    assert recovered.prospective_snapshot(at[0]) == ()
    assert journal_replay(export(reopened))["strategy_results"] == [entry.payload["result"]]
    reopened.close()


def test_lifecycle_append_only_gate_and_explicit_research_transition(strategy, journal):
    artifact, dataset = strategy
    service, repository, at, _ = journal
    testing = service.strategy_lifecycle(
        "dsl",
        artifact.strategy_id,
        "test-request",
        "TESTING",
        "explicit research test",
        ("fixture",),
    )
    assert (
        service.strategy_lifecycle(
            "dsl",
            artifact.strategy_id,
            "test-request",
            "TESTING",
            "explicit research test",
            ("fixture",),
        )
        == testing
    )
    for state in ("VERIFIED", "PAPER", "LIVE_ELIGIBLE"):
        with pytest.raises(ValueError, match="unavailable"):
            service.strategy_lifecycle(
                "dsl",
                artifact.strategy_id,
                "promotion-" + state,
                state,
                "unsupported promotion",
                ("fixture",),
            )
    c = compile_strategy(artifact, dataset)
    first = record_strategy(service, "dsl", artifact, dataset, approval(c, dataset))
    assert first.payload["result"]["lifecycle"] == "TESTING"
    at[0] += timedelta(seconds=1)
    blocked = service.strategy_lifecycle(
        "dsl",
        artifact.strategy_id,
        "block-request",
        "BLOCKED",
        "insufficient research evidence",
        ("fixture",),
    )
    second = record_strategy(service, "dsl", artifact, dataset, approval(c, dataset))
    assert second.payload["result"]["status"] == "BLOCKED"
    assert second.payload["result"]["backtest_json"] is None
    assert second.payload["lifecycle_entry_id"] == blocked.entry_id
    assert first.payload["result"]["status"] == "INCONCLUSIVE"
    assert len(journal_replay(export(repository))["strategy_results"]) == 2


def test_version_identity_cannot_be_redefined_across_experiments(strategy, journal):
    artifact, dataset = strategy
    service, _, _, metadata = journal
    changed = altered(artifact, "research.starting_cash", "20000")
    service.open_experiment(
        replace(
            metadata, experiment_id="changed", config_json=canonical(protocol(changed, dataset))
        )
    )
    with pytest.raises(ValueError, match="redefined"):
        service.register_strategy("changed", changed, dataset)
    bump = altered(changed, "strategy_version", "1.0.1")
    service.open_experiment(
        replace(metadata, experiment_id="bump", config_json=canonical(protocol(bump, dataset)))
    )
    assert service.register_strategy("bump", bump, dataset).key == bump.strategy_id


def test_resigned_result_tamper_fails_semantic_replay(strategy, journal):
    from vision.core.instruments import digest
    from vision.journal.repository import GENESIS, entries_from_export

    artifact, dataset = strategy
    service, repository, _, _ = journal
    c = compile_strategy(artifact, dataset)
    record_strategy(service, "dsl", artifact, dataset, approval(c, dataset))
    forged, previous = export(repository), GENESIS
    for row in forged["entries"]:
        if row["kind"] == "strategy_result":
            value = json.loads(row["payload_json"])
            value["result"]["status"] = "PASS"
            row["payload_json"] = canonical(value)
        row["previous_hash"] = previous
        row["entry_hash"] = previous = digest({k: v for k, v in row.items() if k != "entry_hash"})
    forged["head"] = previous
    assert entries_from_export(forged)
    with pytest.raises(ValueError, match="semantic"):
        journal_replay(forged)


def test_temporal_confirmation_and_lifecycle_retry_collision(strategy, journal):
    artifact, dataset = strategy
    service, repository, at, _ = journal
    c = compile_strategy(artifact, dataset)
    receipt = approval(c, dataset)
    before = repository.entries()
    for delta in (-1, 1):
        with pytest.raises(ValueError, match="Confirmation"):
            service.strategy_result(
                "dsl",
                artifact,
                dataset,
                replace(receipt, confirmed_at=at[0] + timedelta(seconds=delta)),
            )
        assert repository.entries() == before
    service.strategy_lifecycle("dsl", artifact.strategy_id, "park", "PARKED", "manual park", ("e",))
    with pytest.raises(ValueError, match="collision"):
        service.strategy_lifecycle(
            "dsl", artifact.strategy_id, "park", "PARKED", "changed evidence", ("other",)
        )
    service.strategy_lifecycle(
        "dsl", artifact.strategy_id, "resume", "CANDIDATE", "explicit research resume", ("e",)
    )
    assert (
        service.strategy_result("dsl", artifact, dataset, receipt).payload["result"]["status"]
        == "INCONCLUSIVE"
    )


def test_preflight_rejects_late_and_regressing_canonical_receipts(strategy):
    artifact, dataset = strategy
    old = replace(dataset.events[0], received_ts=dataset.events[-1].received_ts)
    c = compile_strategy(artifact, replace(dataset, events=(old, *dataset.events[1:])))
    assert c.status == "BLOCKED"
    assert "DATA_RECEIPT_ORDER" in c.reasons and "DATA_SIGNAL_UNAVAILABLE_NEXT_OPEN" in c.reasons


def test_portable_confirmed_fixture_and_journal(strategy):
    from vision.strategies.runtime import replay

    root = Path(__file__).parent / "fixtures/strategies"
    value = json.loads((root / "confirmed_run.json").read_text(encoding="utf-8"))
    assert replay(value).status == "INCONCLUSIVE"
    changed = json.loads(json.dumps(value))
    changed["result"]["status"] = "PASS"
    with pytest.raises(ValueError, match="mismatch"):
        replay(changed)
    report = journal_replay(
        json.loads((root / "research_journal.json").read_text(encoding="utf-8"))
    )
    assert report["strategy_results"][0]["status"] == "INCONCLUSIVE"
    assert report["directional_outcome_ids"] == report["prospective_registration_ids"] == []


@pytest.mark.parametrize(
    "command",
    [
        "strategy-preview",
        "strategy-audit",
        "strategy-run",
        "strategy-replay",
    ],
)
def test_cli_execution_guard_before_strategy_io(tmp_path, command):
    from test_journal import cli

    path, output = tmp_path / "missing", tmp_path / "output"
    args = (
        (path, path, output)
        if command == "strategy-run"
        else ((path, path) if command == "strategy-audit" else (path,))
    )
    result = cli(command, *args, enabled=True)
    assert result.returncode == 2 and "must be false" in result.stderr
    assert not output.exists()


def test_cli_exact_preview_confirmation_and_offline_replay(strategy, tmp_path):
    from test_journal import cli

    artifact, dataset = strategy
    a, d = tmp_path / "artifact.json", tmp_path / "dataset.json"
    a.write_text(artifact.artifact_json, encoding="utf-8")
    d.write_text(canonical(wire(dataset)), encoding="utf-8")
    shown = cli("strategy-preview", a)
    assert shown.returncode == 0 and preview(artifact).text in shown.stdout
    checked = cli("strategy-audit", a, d)
    assert checked.returncode == 0
    c = json.loads(checked.stdout)
    blocked = cli("strategy-run", a, d, tmp_path / "blocked.json")
    assert json.loads(blocked.stdout)["status"] == "BLOCKED"
    output = tmp_path / "confirmed.json"
    result = cli(
        "strategy-run",
        a,
        d,
        output,
        "--confirm-preview",
        c["preview_hash"],
        "--reviewer",
        "explicit-local-review",
    )
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "INCONCLUSIVE"
    replayed = cli("strategy-replay", output)
    assert replayed.returncode == 0 and replayed.stdout == result.stdout
    before = output.read_bytes()
    assert cli("strategy-run", a, d, output).returncode == 2
    assert output.read_bytes() == before
    unknown = artifact.value
    unknown["unexpected"] = "execute me"
    a.write_text(canonical(unknown), encoding="utf-8")
    rejected = cli("strategy-preview", a)
    assert rejected.returncode == 2 and "Strategy BLOCKED" in rejected.stderr
    assert "Traceback" not in rejected.stderr


def test_schema_contract_declares_all_rule_parameters_required():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/strategy-definition-v1.schema.json").read_text())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    for name in ("entry", "exit", "risk", "execution", "market", "research"):
        node = schema["properties"][name]
        assert node["additionalProperties"] is False
        assert set(node["required"]) == set(node["properties"])
    assert "default" not in (
        root / "schemas/strategy-definition-v1.schema.json"
    ).read_text().replace("No defaults or deployment permission.", "")


def test_ema_compilation_parity_and_recursive_audit(strategy):
    artifact, dataset = strategy
    ema = altered(artifact, "features.0.kind", "EMA")
    contaminated = compile_strategy(ema, dataset)
    assert "RECURSIVE_INDICATOR_CONTAMINATION" in contaminated.reasons
    flat = replace(
        dataset,
        events=tuple(
            replace(
                e,
                payload=replace(
                    e.payload,
                    open=Decimal(100),
                    high=Decimal(101),
                    low=Decimal(99),
                    close=Decimal(100),
                ),
            )
            for e in dataset.events
        ),
    )
    compiled = compile_strategy(ema, flat)
    assert compiled.status == "READY"
    result = execute(compiled, approval(compiled, flat))
    assert (
        json.loads(result.backtest_json)
        == run(inputs_from_dict(json.loads(compiled.compiled_json))).to_dict()
    )


def test_new_experiment_cannot_reset_global_strategy_lifecycle(strategy, journal):
    artifact, dataset = strategy
    service, _, _, metadata = journal
    service.strategy_lifecycle(
        "dsl",
        artifact.strategy_id,
        "global-block",
        "BLOCKED",
        "blocked across experiments",
        ("fixture",),
    )
    registration = service.open_experiment(replace(metadata, experiment_id="same-artifact"))
    assert "DUPLICATE_EXPERIMENT" in registration.payload["warnings"]
    c = compile_strategy(artifact, dataset)
    result = record_strategy(service, "same-artifact", artifact, dataset, approval(c, dataset))
    assert result.payload["result"]["status"] == "BLOCKED"
    assert result.payload["result"]["backtest_json"] is None


def test_research_resume_keeps_distinct_journal_context_without_inventing_new_backtest(
    strategy, journal
):
    artifact, dataset = strategy
    service, repository, _, _ = journal
    c = compile_strategy(artifact, dataset)
    receipt = approval(c, dataset)
    first = record_strategy(service, "dsl", artifact, dataset, receipt)
    service.strategy_lifecycle("dsl", artifact.strategy_id, "park", "PARKED", "park", ("e",))
    service.strategy_lifecycle("dsl", artifact.strategy_id, "resume", "CANDIDATE", "resume", ("e",))
    second = record_strategy(service, "dsl", artifact, dataset, receipt)
    assert first.key != second.key
    assert first.payload["result"] == second.payload["result"]
    assert record_strategy(service, "dsl", artifact, dataset, receipt) == second
    assert len(journal_replay(export(repository))["strategy_results"]) == 2
