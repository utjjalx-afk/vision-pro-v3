"""Offline diagnostic by default; public market data requires an explicit subcommand."""

import argparse
import json
import os

from vision import __version__
from vision.config import Phase0ExecutionDisabled, load_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Vision Pro V3 public market-data foundation")
    parser.add_argument(
        "--component", choices=("core", "api", "worker", "dashboard"), default="core"
    )
    commands = parser.add_subparsers(dest="command")
    market = commands.add_parser("market-data", help="Read public Spot data; no execution")
    market.add_argument("--provider", choices=("binance", "bybit", "failover"), default="binance")
    market.add_argument("--symbol", default="BTCUSDT")
    market.add_argument("--transport", choices=("rest", "ws"), default="ws")
    market.add_argument("--streams", help="Comma-separated trade,quote,bar (REST: trade/bar)")
    market.add_argument("--interval", default="1m")
    market.add_argument("--max-events", type=int, default=10)
    market.add_argument("--duration", type=float, default=30)
    specs = commands.add_parser("instrument-specs", help="Read public Spot specs with provenance")
    specs.add_argument("--symbol", default="BTCUSDT")
    specs.add_argument("--provider", choices=("binance", "bybit", "both"), default="both")
    paper = commands.add_parser("paper-replay", help="Validate/replay an offline paper checkpoint")
    paper.add_argument("path")
    lanes = commands.add_parser("lanes-replay", help="Replay canonical offline intelligence inputs")
    lanes.add_argument("path")
    synthesis = commands.add_parser(
        "synthesis-replay", help="Replay prospective reliability and unsized intent candidates"
    )
    synthesis.add_argument("path")
    research_export = commands.add_parser(
        "research-export", help="Export a durable journal offline"
    )
    research_export.add_argument("database")
    research_export.add_argument("output")
    research_replay = commands.add_parser(
        "research-replay", help="Verify a complete research export"
    )
    research_replay.add_argument("path")
    research_replay.add_argument("--expected-head")
    args = parser.parse_args()
    try:
        settings = load_settings(os.environ)
    except Phase0ExecutionDisabled as error:
        parser.exit(2, f"{error}\n")
    if args.command in {"research-export", "research-replay"}:
        import sqlite3
        from pathlib import Path

        from vision.journal.replay import replay
        from vision.journal.repository import SQLiteRepository, export

        try:
            if args.command == "research-export":
                repository = SQLiteRepository(args.database, readonly=True)
                try:
                    value = export(repository)
                    report = replay(value)
                finally:
                    repository.close()
                with Path(args.output).open("x", encoding="utf-8") as handle:
                    json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
                    handle.write("\n")
            else:
                with Path(args.path).open("rb") as handle:
                    raw = handle.read(50000001)
                if len(raw) > 50000000:
                    raise ValueError("Research export exceeds size limit")
                report = replay(json.loads(raw), expected_head=args.expected_head)
            print(json.dumps(report, sort_keys=True, allow_nan=False))
            return 0
        except (ValueError, OSError, sqlite3.Error) as error:
            parser.exit(2, f"Research journal rejected: {type(error).__name__}\n")
    if args.command == "synthesis-replay":
        from pathlib import Path

        from vision.analysis.synthesizer.replay import replay
        from vision.intents.models import candidate

        try:
            with Path(args.path).open("rb") as handle:
                raw = handle.read(5000001)
            if len(raw) > 5000000:
                raise ValueError("Synthesis replay input exceeds size limit")
            decision = replay(json.loads(raw))
            intent = candidate(decision)
            print(
                json.dumps(
                    {
                        "decision": decision.to_dict(),
                        "intent": intent.to_dict() if intent else None,
                    },
                    sort_keys=True,
                )
            )
            return 0
        except (ValueError, OSError) as error:
            parser.exit(2, f"Synthesis replay rejected: {type(error).__name__}\n")
    if args.command == "lanes-replay":
        from pathlib import Path

        from vision.analysis.replay import replay

        try:
            with Path(args.path).open("rb") as handle:
                raw = handle.read(5000001)
            if len(raw) > 5000000:
                raise ValueError("Lane replay input exceeds size limit")
            print(json.dumps([item.to_dict() for item in replay(json.loads(raw))], sort_keys=True))
            return 0
        except (ValueError, OSError) as error:
            parser.exit(2, f"Lane replay rejected: {type(error).__name__}\n")
    if args.command == "paper-replay":
        from vision.execution.paper.broker import PaperBroker

        try:
            broker = PaperBroker.load(args.path)
            print(json.dumps(broker.snapshot(broker.last_at).to_dict(), sort_keys=True))
            return 0
        except (ValueError, OSError) as error:
            parser.exit(2, f"Paper checkpoint rejected: {type(error).__name__}\n")
    if args.command == "instrument-specs":
        from vision.core.instruments import InstrumentRegistry
        from vision.market_data.adapters.binance import BinanceREST, MarketDataError, symbol_name
        from vision.market_data.adapters.bybit import BybitREST
        from vision.market_data.instruments import refresh_public_spec

        try:
            symbol_name(args.symbol)
            registry = InstrumentRegistry()
            providers = {"binance": BinanceREST, "bybit": BybitREST}
            for provider in providers if args.provider == "both" else (args.provider,):
                record = refresh_public_spec(registry, providers[provider](), args.symbol)
                print(json.dumps(record.to_dict(), sort_keys=True), flush=True)
            return 0
        except ValueError as error:
            parser.exit(2, f"{error}\n")
        except MarketDataError as error:
            parser.exit(3, f"{error}\n")
    if args.command == "market-data":
        from vision.market_data.adapters.binance import MarketDataError
        from vision.market_data.cli import run_market_data

        try:
            return run_market_data(args)
        except ValueError as error:
            parser.exit(2, f"{error}\n")
        except MarketDataError as error:
            parser.exit(3, f"{error}\n")
        except KeyboardInterrupt:
            return 130
    print(
        json.dumps(
            {
                "version": __version__,
                "phase": "phase-7",
                "component": args.component,
                "mode": "offline-diagnostic",
                "live_trading_enabled": settings.live_trading_enabled,
                "mt5_execution_enabled": settings.mt5_execution_enabled,
                "paper_trading_enabled": settings.paper_trading_enabled,
                "agents_enabled": settings.agents_enabled,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
