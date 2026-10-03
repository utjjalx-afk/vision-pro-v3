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
    args = parser.parse_args()
    try:
        settings = load_settings(os.environ)
    except Phase0ExecutionDisabled as error:
        parser.exit(2, f"{error}\n")
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
                "phase": "phase-4",
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
