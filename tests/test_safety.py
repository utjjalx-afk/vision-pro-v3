import json
import os
import subprocess
import sys

import pytest

from vision.config import Phase0ExecutionDisabled, Settings, load_settings


def test_safe_defaults():
    settings = load_settings({})
    assert settings.live_trading_enabled is False
    assert settings.mt5_execution_enabled is False


@pytest.mark.parametrize(
    "key",
    [
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ],
)
@pytest.mark.parametrize("value", ["true", "1", "yes", "on", "", "unknown"])
def test_enabling_and_ambiguous_flags_rejected(key, value):
    with pytest.raises(Phase0ExecutionDisabled):
        load_settings({key: value})


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " FALSE "])
def test_explicit_false_accepted(value):
    assert load_settings({"VISION_LIVE_TRADING_ENABLED": value}) == Settings()


def test_direct_configuration_cannot_enable_execution():
    with pytest.raises(Phase0ExecutionDisabled):
        Settings(live_trading_enabled=True)
    with pytest.raises(Phase0ExecutionDisabled):
        Settings(mt5_execution_enabled=True)


def safe_environment():
    return {
        **os.environ,
        "VISION_LIVE_TRADING_ENABLED": "false",
        "VISION_MT5_EXECUTION_ENABLED": "false",
    }


@pytest.mark.parametrize("component", ["core", "api", "worker", "dashboard"])
def test_installed_cli_runs_outside_checkout_without_reading_secrets(tmp_path, component):
    result = subprocess.run(
        [sys.executable, "-m", "vision", "--component", component],
        cwd=tmp_path,
        env={**safe_environment(), "BINANCE_API_SECRET": "synthetic-do-not-echo"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    wire = json.loads(result.stdout)
    assert wire["component"] == component
    assert wire["mode"] == "offline-diagnostic"
    assert wire["live_trading_enabled"] is False
    assert wire["mt5_execution_enabled"] is False
    assert "synthetic-do-not-echo" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "key",
    [
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ],
)
def test_cli_rejects_enabled_execution(tmp_path, key):
    result = subprocess.run(
        [sys.executable, "-m", "vision"],
        cwd=tmp_path,
        env={**safe_environment(), key: "true"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "must be false in Phase-10" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "key",
    [
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ],
)
def test_market_data_cli_rejects_execution_before_network(tmp_path, key):
    result = subprocess.run(
        [sys.executable, "-m", "vision", "market-data"],
        cwd=tmp_path,
        env={**safe_environment(), key: "true"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "must be false in Phase-10" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["--symbol", "btcusdt"],
        ["--duration", "nan"],
        ["--max-events", "0"],
        ["--streams", "account"],
        ["--interval", "1M"],
    ],
)
def test_invalid_cli_data_options_fail_before_network(tmp_path, arguments):
    result = subprocess.run(
        [sys.executable, "-m", "vision", "market-data", *arguments],
        cwd=tmp_path,
        env=safe_environment(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""


@pytest.mark.parametrize(
    "key",
    [
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ],
)
def test_specs_cli_rejects_enabling_before_network(tmp_path, key):
    result = subprocess.run(
        [sys.executable, "-m", "vision", "instrument-specs"],
        cwd=tmp_path,
        env={**safe_environment(), key: "true"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and result.stdout == ""


def test_specs_cli_invalid_symbol_fails_without_network(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "vision", "instrument-specs", "--symbol", "bad"],
        cwd=tmp_path,
        env=safe_environment(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and result.stdout == ""
