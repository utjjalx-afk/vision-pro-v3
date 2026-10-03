"""Offline diagnostic entry point shared by native and container scaffolds."""

import argparse
import json
import os

from vision import __version__
from vision.config import Phase0ExecutionDisabled, load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Vision Pro V3 offline Phase-0 diagnostic")
    parser.add_argument(
        "--component", choices=("core", "api", "worker", "dashboard"), default="core"
    )
    args = parser.parse_args()
    try:
        settings = load_settings(os.environ)
    except Phase0ExecutionDisabled as error:
        parser.exit(2, f"{error}\n")
    print(
        json.dumps(
            {
                "version": __version__,
                "phase": "phase-0",
                "component": args.component,
                "mode": "offline-diagnostic",
                "live_trading_enabled": settings.live_trading_enabled,
                "mt5_execution_enabled": settings.mt5_execution_enabled,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
