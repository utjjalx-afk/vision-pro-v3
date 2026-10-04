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
    forex = commands.add_parser("forex-data", help="Explicit authorized OANDA data snapshot")
    forex.add_argument("--symbol", required=True, choices=("EUR_USD", "XAU_USD", "XAG_USD"))
    forex.add_argument("--environment", required=True, choices=("practice", "live"))
    forex.add_argument("--granularity", required=True, choices=("M1", "M5", "M15", "H1"))
    forex.add_argument("--calendar", required=True)
    forex.add_argument("--source-epoch", required=True, type=int)
    forex_replay = commands.add_parser(
        "forex-replay", help="Replay synthetic FX/metals data offline"
    )
    forex_replay.add_argument("path")
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
    backtest = commands.add_parser("backtest-run", help="Run frozen offline canonical-bar research")
    backtest.add_argument("path")
    backtest.add_argument("output")
    backtest_replay = commands.add_parser("backtest-replay", help="Verify a stored backtest run")
    backtest_replay.add_argument("path")
    strategy_preview = commands.add_parser(
        "strategy-preview", help="Validate and preview exact DSL"
    )
    strategy_preview.add_argument("artifact")
    strategy_audit = commands.add_parser(
        "strategy-audit", help="Compile/audit DSL without backtesting"
    )
    strategy_audit.add_argument("artifact")
    strategy_audit.add_argument("dataset")
    strategy_run = commands.add_parser("strategy-run", help="Run confirmed DSL research offline")
    strategy_run.add_argument("artifact")
    strategy_run.add_argument("dataset")
    strategy_run.add_argument("output")
    strategy_run.add_argument("--confirm-preview", help="Exact hash shown by strategy-audit")
    strategy_run.add_argument("--reviewer", help="Explicit local review attribution")
    strategy_replay = commands.add_parser("strategy-replay", help="Verify an offline strategy run")
    strategy_replay.add_argument("path")
    args = parser.parse_args()
    try:
        settings = load_settings(os.environ)
    except Phase0ExecutionDisabled as error:
        parser.exit(2, f"{error}\n")
    if args.command in {"forex-data", "forex-replay"}:
        from datetime import UTC, datetime
        from pathlib import Path

        from vision.market_data.adapters.binance import MarketDataError
        from vision.market_data.adapters.oanda import OandaREST
        from vision.market_data.forex import replay, snapshot
        from vision.market_data.sessions import calendar_from_dict
        from vision.strategies.dsl import strict_json

        try:
            path = args.path if args.command == "forex-replay" else args.calendar
            with Path(path).open("rb") as handle:
                raw = handle.read(2000001)
            value = strict_json(raw.decode("utf-8"), limit=2000000)
            if args.command == "forex-replay":
                result = replay(value)
            else:
                rest = OandaREST(
                    token=os.environ.get("OANDA_API_TOKEN", ""),
                    account_id=os.environ.get("OANDA_ACCOUNT_ID", ""),
                    environment=args.environment,
                )
                result = snapshot(
                    rest,
                    args.symbol,
                    args.granularity,
                    calendar_from_dict(value),
                    clock=lambda: datetime.now(UTC),
                    source_epoch=args.source_epoch,
                )
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0
        except (ValueError, OSError, TypeError, KeyError, IndexError, OverflowError):
            parser.exit(2, "Forex data BLOCKED: invalid or unavailable data/configuration\n")
        except MarketDataError:
            parser.exit(3, "Forex data BLOCKED: authorized market-data request failed\n")
    if args.command in {"strategy-preview", "strategy-audit", "strategy-run", "strategy-replay"}:
        from datetime import UTC, datetime
        from pathlib import Path

        from vision.analysis.contracts import wire
        from vision.strategies.dsl import parse, preview, strict_json
        from vision.strategies.runtime import (
            compile_strategy,
            confirm_preview,
            dataset_from_dict,
            execute,
            replay,
        )

        def read(path, limit):
            with Path(path).open("rb") as handle:
                raw = handle.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("Strategy input size exceeded")
            return raw.decode("utf-8")

        try:
            if args.command == "strategy-replay":
                result = replay(strict_json(read(args.path, 50000000), limit=50000000))
                print(json.dumps(result.to_dict(), sort_keys=True))
                return 0
            artifact = parse(read(args.artifact, 100000))
            if args.command == "strategy-preview":
                print(preview(artifact).text)
                print("preview_hash=" + preview(artifact).preview_hash)
                return 0
            dataset = dataset_from_dict(strict_json(read(args.dataset, 5000000), limit=5000000))
            compilation = compile_strategy(artifact, dataset)
            if args.command == "strategy-audit":
                print(json.dumps(compilation.to_dict(), sort_keys=True))
                return 0
            confirmation = None
            if args.confirm_preview is not None:
                confirmation = confirm_preview(
                    compilation,
                    preview_hash=args.confirm_preview,
                    reviewer=args.reviewer,
                    at=datetime.now(UTC),
                )
            result = execute(compilation, confirmation)
            envelope = {
                "artifact": artifact.value,
                "dataset": wire(dataset),
                "compilation": compilation.to_dict(),
                "confirmation": wire(confirmation),
                "lifecycle": result.lifecycle,
                "result": result.to_dict(),
            }
            with Path(args.output).open("x", encoding="utf-8") as handle:
                json.dump(envelope, handle, sort_keys=True, indent=2, allow_nan=False)
                handle.write("\n")
            print(json.dumps(result.to_dict(), sort_keys=True))
            return 0
        except (ValueError, OSError, TypeError, KeyError, IndexError, OverflowError) as error:
            parser.exit(2, f"Strategy BLOCKED: {type(error).__name__}\n")
    if args.command in {"backtest-run", "backtest-replay"}:
        from pathlib import Path

        from vision.analysis.contracts import wire
        from vision.research.backtest import inputs_from_dict, replay, run

        try:
            with Path(args.path).open("rb") as handle:
                raw = handle.read(50000001)
            if len(raw) > 50000000:
                raise ValueError("Backtest input exceeds size limit")
            value = json.loads(raw)
            if args.command == "backtest-run":
                inputs = inputs_from_dict(value)
                result = run(inputs)
                with Path(args.output).open("x", encoding="utf-8") as handle:
                    json.dump(
                        {"input": wire(inputs), "run": result.to_dict()},
                        handle,
                        sort_keys=True,
                        indent=2,
                        allow_nan=False,
                    )
                    handle.write("\n")
            else:
                result = replay(value)
            print(
                json.dumps(
                    {
                        "run_id": result.run_id,
                        "status": result.status.value,
                        "dataset_hash": result.dataset_hash,
                        "config_hash": result.config_hash,
                        "result": json.loads(result.result_json),
                    },
                    sort_keys=True,
                )
            )
            return 0
        except (ValueError, OSError, TypeError, KeyError, IndexError, OverflowError) as error:
            parser.exit(2, f"Backtest rejected: {type(error).__name__}\n")
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
                "phase": "phase-10",
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
