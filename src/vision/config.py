"""Fail-closed data-only configuration; no credentials are read or echoed."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


class Phase0ExecutionDisabled(ValueError):
    """An execution-enabling or ambiguous configuration was supplied.

    The exception name is retained for compatibility with the Phase-0 foundation.
    """


@dataclass(frozen=True, slots=True)
class Settings:
    live_trading_enabled: Literal[False] = False
    mt5_execution_enabled: Literal[False] = False
    paper_trading_enabled: Literal[False] = False
    agents_enabled: Literal[False] = False

    def __post_init__(self) -> None:
        if any(
            value is not False
            for value in (
                self.live_trading_enabled,
                self.mt5_execution_enabled,
                self.paper_trading_enabled,
                self.agents_enabled,
            )
        ):
            raise Phase0ExecutionDisabled(
                "Global enabling is unavailable; paper uses an explicit offline simulator"
            )


def load_settings(environment: Mapping[str, str]) -> Settings:
    """Only explicit false spellings are accepted, including for an absent flag."""
    for key in (
        "VISION_LIVE_TRADING_ENABLED",
        "VISION_MT5_EXECUTION_ENABLED",
        "VISION_PAPER_TRADING_ENABLED",
        "VISION_AGENTS_ENABLED",
    ):
        value = environment.get(key, "false").strip().lower()
        if value not in {"false", "0", "no", "off"}:
            raise Phase0ExecutionDisabled(f"{key} must be false in Phase-4")
    return Settings()
