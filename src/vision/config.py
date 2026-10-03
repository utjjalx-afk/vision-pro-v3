"""Fail-closed Phase-0 configuration; no credentials are read or echoed."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


class Phase0ExecutionDisabled(ValueError):
    """An execution-enabling or ambiguous configuration was supplied."""


@dataclass(frozen=True, slots=True)
class Settings:
    live_trading_enabled: Literal[False] = False
    mt5_execution_enabled: Literal[False] = False

    def __post_init__(self) -> None:
        if self.live_trading_enabled is not False or self.mt5_execution_enabled is not False:
            raise Phase0ExecutionDisabled("Execution is unimplemented in Phase-0")


def load_settings(environment: Mapping[str, str]) -> Settings:
    """Only explicit false spellings are accepted, including for an absent flag."""
    for key in ("VISION_LIVE_TRADING_ENABLED", "VISION_MT5_EXECUTION_ENABLED"):
        value = environment.get(key, "false").strip().lower()
        if value not in {"false", "0", "no", "off"}:
            raise Phase0ExecutionDisabled(f"{key} must be false in Phase-0")
    return Settings()
