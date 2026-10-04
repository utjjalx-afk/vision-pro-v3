"""Private local evidence only; CI cannot complete the mandatory broker acceptance gate."""

import argparse
import json
import os
from pathlib import Path

from vision.broker.codec import decimal, dto
from vision.broker.models import SizingPolicy
from vision.broker.mt5_bridge import MT5Reader
from vision.broker.reconcile import reconcile
from vision.config import load_settings
from vision.strategies.dsl import strict_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("output")
    parser.add_argument("--terminal-path", required=True)
    args = parser.parse_args()
    try:
        load_settings(os.environ)
        with Path(args.config).open("rb") as handle:
            value = strict_json(handle.read(100001).decode())
        if set(value) != {"mapping", "policy", "plans"}:
            raise ValueError("Exact demo protocol required")
        plans = {
            key: {name: decimal(amount) for name, amount in row.items()}
            for key, row in value["plans"].items()
        }
        reader = MT5Reader.connect(
            value["mapping"],
            os.environ.get("MT5_BRIDGE_TOKEN", "").encode(),
            terminal_path=args.terminal_path,
        )
        report = reconcile(reader, dto(SizingPolicy, value["policy"]), plans)
        with Path(args.output).open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
        print("Demo evidence captured; operator review and four-asset acceptance remain required.")
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, ImportError):
        parser.exit(
            2, "Demo reconciliation BLOCKED: unavailable broker truth or invalid protocol\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())
