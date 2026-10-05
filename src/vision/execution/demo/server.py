"""Optional Windows demo server; feature enabled separately, startup always DISARMED."""

import argparse
import os
from pathlib import Path

from vision.broker.codec import dto
from vision.broker.mt5_bridge import MT5Reader
from vision.broker.server import make_server
from vision.config import load_settings
from vision.execution.demo.gateway import DemoGateway
from vision.execution.demo.journal import DemoJournal
from vision.execution.demo.models import DemoPolicy
from vision.execution.demo.native import DemoMT5Transport
from vision.journal.repository import SQLiteRepository
from vision.risk.governor import HardRiskGovernor, RiskLimits
from vision.strategies.dsl import strict_json


def main():
    parser = argparse.ArgumentParser(description="Guarded operator-controlled MT5 DEMO server")
    parser.add_argument("--config", required=True)
    parser.add_argument("--terminal-path", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--enable-demo", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    repository = None
    try:
        load_settings(os.environ)  # All global/live/agent flags still false.
        token = os.environ.get("MT5_BRIDGE_TOKEN", "")
        with Path(args.config).open("rb") as handle:
            config = strict_json(handle.read(100001).decode())
        if type(config) is not dict or set(config) != {
            "mapping",
            "demo_policy",
            "risk_limits",
            "experiment_id",
        }:
            raise ValueError("Exact private demo configuration required")
        policy = dto(DemoPolicy, config["demo_policy"])
        limits = dto(RiskLimits, config["risk_limits"])
        reader = MT5Reader.connect(
            config["mapping"], token.encode(), terminal_path=args.terminal_path
        )
        repository = SQLiteRepository(args.journal)
        journal = DemoJournal(repository, experiment_id=config["experiment_id"])
        transport = DemoMT5Transport(
            reader, account_identity=policy.account_identity, magic=policy.magic
        )
        gateway = DemoGateway(
            transport, journal, policy, HardRiskGovernor(limits), enabled=args.enable_demo
        )
        server = make_server(reader, token, port=args.port, demo_gateway=gateway)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, ImportError, RuntimeError):
        parser.exit(2, "Demo gateway BLOCKED: invalid configuration or unavailable demo terminal\n")
    finally:
        if repository is not None:
            repository.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
