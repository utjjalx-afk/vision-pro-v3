"""Private VPS export integrity and demo-only risk options; no native orders."""

import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_demo_execution import arm, setup

from vision.execution.demo.journal import replay
from vision.execution.demo.risk import assess
from vision.operations.demo import daily_export, journal_snapshot, observe, persist


def config():
    return {
        "viewer_token_file": "not-read-by-fake",
        "dashboard_port": 8787,
        "account_identity": "synthetic-demo-account",
        "currency": "USD",
        "journal": None,
    }


def report(**changes):
    value = {
        "account": {
            "identity": "synthetic-demo-account",
            "currency": "USD",
            "demo": True,
            "connected": True,
        },
        "live_enabled": False,
        "broker_state": "BLOCKED",
    }
    value.update(changes)
    return value


def test_ist_day_and_export_digest(tmp_path):
    value = observe(
        config(), now=datetime(2026, 10, 7, 19, tzinfo=UTC), fetch=lambda *args: report()
    )
    assert value["day_ist"] == "2026-10-08"
    persist(tmp_path, value)
    archive = daily_export(tmp_path, value["day_ist"], tmp_path / "evidence.zip")
    with zipfile.ZipFile(archive) as bundle:
        assert {"manifest.json", "summary.md", "observations.csv"} <= set(bundle.namelist())
        assert b"Daily PnL unavailable" in bundle.read("summary.md")
    with pytest.raises(FileExistsError):
        daily_export(tmp_path, value["day_ist"], archive)


@pytest.mark.parametrize(
    "field,value",
    [("demo", False), ("identity", "changed"), ("currency", "EUR"), ("connected", False)],
)
def test_wrong_native_identity_never_persisted(field, value):
    current = report()
    current["account"][field] = value
    observation = observe(config(), fetch=lambda *a: current)
    assert observation["status"] == "COLLECTION_FAILED"
    assert "report" not in observation and observation["collector_orders"] == 0


def test_failed_capture_retained_without_zero_pnl(tmp_path):
    def failed(*args):
        raise OSError("private credential must not be logged")

    observation = observe(config(), fetch=failed)
    path = persist(tmp_path, observation)
    assert "private credential" not in path.read_text()
    daily_export(tmp_path, observation["day_ist"], tmp_path / "failure.zip")


def test_tamper_or_wrong_day_cannot_export(tmp_path):
    observation = observe(config(), fetch=lambda *a: report())
    path = persist(tmp_path, observation)
    value = json.loads(path.read_text())
    value["evidence"]["status"] = "HEALTHY"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="INTEGRITY"):
        daily_export(tmp_path, observation["day_ist"], tmp_path / "tampered.zip")
    assert not (tmp_path / "tampered.zip").exists()


def test_mixed_accounts_cannot_be_combined(tmp_path):
    observation = observe(config(), fetch=lambda *a: report())
    persist(tmp_path, observation)
    other = {
        **observation,
        "account_identity": "different-demo",
        "report": {
            **observation["report"],
            "account": {**observation["report"]["account"], "identity": "different-demo"},
        },
    }
    persist(tmp_path, other)
    with pytest.raises(ValueError, match="MIXED_ACCOUNT"):
        daily_export(tmp_path, observation["day_ist"], tmp_path / "mixed.zip")


def test_readonly_journal_export_replays_without_writing(tmp_path):
    g, _, repo, command, _ = setup(tmp_path)
    arm(g)
    g.execute(**command)
    before = repo.entries()
    value = journal_snapshot(repo.path, g.policy.account_identity)
    assert value["state"] == "VERIFIED" and value["entry_count"] == len(before)
    assert repo.entries() == before
    with pytest.raises(ValueError, match="ACCOUNT_MISMATCH"):
        journal_snapshot(repo.path, "different-account")
    cfg, current = config(), report()
    cfg["account_identity"] = g.policy.account_identity
    current["account"]["identity"] = g.policy.account_identity
    observation = observe(cfg, fetch=lambda *a: current)
    persist(tmp_path, observation)
    archive = daily_export(
        tmp_path, observation["day_ist"], tmp_path / "journal.zip", journal=repo.path
    )
    with zipfile.ZipFile(archive) as bundle:
        assert "trades-at-export.csv" in bundle.namelist()
        value = json.loads(bundle.read("journal-at-export.json"))
        assert (
            value["head"] == journal_snapshot(repo.path, g.policy.account_identity)["journal_head"]
        )


def test_daily_loss_option_demo_only_and_replayable(tmp_path):
    g, transport, repo, command, _ = setup(tmp_path)
    command["risk_context"] = replace(command["risk_context"], daily_baseline=Decimal(12000))
    g.policy = replace(g.policy, daily_loss_limit_enabled=False)
    arm(g)
    head = g.execute(**command)
    assert head["state"] == "POSITION_OPEN" and len(transport.calls) == 1
    assert head["details"]["daily_loss_limit_enabled"] is False
    assert replay(repo.entries()) == g.journal.heads()


def test_daily_option_never_suppresses_other_vetoes():
    class Governor:
        def assess(self, **values):
            return ("DAILY_EQUITY_LOSS_LIMIT", "PER_TRADE_RISK_CAP", "MAX_DRAWDOWN")

    assert assess(Governor(), daily_loss_limit_enabled=False) == (
        "PER_TRADE_RISK_CAP",
        "MAX_DRAWDOWN",
    )
    assert len(assess(Governor())) == 3
    with pytest.raises(ValueError):
        assess(Governor(), daily_loss_limit_enabled=0)


def test_required_tp_never_sends_without_target(tmp_path):
    g, transport, _, command, _ = setup(tmp_path)
    g.policy = replace(g.policy, require_tp=True)
    arm(g)
    with pytest.raises(ValueError, match="MANDATORY_DEMO_TP"):
        g.execute(**command)
    assert not transport.calls


@pytest.mark.parametrize(
    "side,entry,initial,stop,bid,ask,want",
    [
        ("LONG", "100", "90", "90", "125", "126", "115"),
        ("SHORT", "100", "110", "110", "74", "75", "85"),
    ],
)
def test_profit_lock_trailing_preview_both_directions(side, entry, initial, stop, bid, ask, want):
    from vision.execution.demo.protection import ProtectionRules, preview

    d = Decimal
    value = preview(
        side=side,
        entry=d(entry),
        initial_stop=d(initial),
        accepted_stop=d(stop),
        bid=d(bid),
        ask=d(ask),
        tick_size=d("0.1"),
        minimum_distance=d(1),
        rules=ProtectionRules(d(3), d(1), d("0.25"), d(1)),
    )
    assert value["state"] == "PREVIEW_ONLY" and d(value["proposed_stop"]) == d(want)
    assert value["execution_authorized"] is False


def test_trailing_preview_never_widens_or_forces_broker_distance():
    from vision.execution.demo.protection import ProtectionRules, preview

    d = Decimal
    args = dict(
        side="LONG",
        entry=d(100),
        initial_stop=d(90),
        accepted_stop=d(119),
        bid=d(125),
        ask=d(126),
        tick_size=d("0.1"),
        minimum_distance=d(1),
        rules=ProtectionRules(d(3), d(1), d("0.25"), d(1)),
    )
    assert preview(**args)["state"] == "HOLD_EXISTING_SL"
    args.update(accepted_stop=d(90), minimum_distance=d(20))
    assert preview(**args)["state"] == "BLOCKED_BROKER_DISTANCE"


def test_daily_loss_choice_is_part_of_replay_identity(tmp_path):
    from vision.execution.demo.journal import validate_payload

    g, _, repo, command, _ = setup(tmp_path)
    arm(g)
    g.execute(**command)
    payload = repo.entries()[1].payload
    payload["details"]["daily_loss_limit_enabled"] = False
    with pytest.raises(ValueError, match="replay mismatch"):
        validate_payload(payload)


def test_scheduled_daily_export_at_20_ist(tmp_path):
    from vision.operations.demo import scheduled_exports

    at = datetime(2026, 10, 7, 14, 29, tzinfo=UTC)
    observation = observe(config(), now=at, fetch=lambda *a: report())
    persist(tmp_path, observation)
    scheduled_exports(tmp_path, observation, None, 20, now=at)
    assert not (tmp_path / "exports").exists()
    scheduled_exports(tmp_path, observation, None, 20, now=at.replace(minute=30))
    assert (tmp_path / "exports/2026-10-07-20h-snapshot.zip").is_file()


def test_export_failure_remains_evidence_and_does_not_stop_monitoring(tmp_path):
    from vision.operations.demo import scheduled_exports

    at = datetime(2026, 10, 7, 16, tzinfo=UTC)
    observation = observe(config(), now=at, fetch=lambda *a: report())
    path = persist(tmp_path, observation)
    path.write_text("tampered")
    scheduled_exports(tmp_path, observation, None, 20, now=at)
    values = [
        json.loads(p.read_text())["evidence"] for p in path.parent.glob("*.json") if p != path
    ]
    assert values[0]["reason"] == "DAILY_EXPORT_FAILED"
    assert not (tmp_path / "exports/2026-10-07-20h-snapshot.zip").exists()
