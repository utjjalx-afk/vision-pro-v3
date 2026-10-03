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
    market = commands.add_parser("market-data", help="Read Binance Spot public data; no execution")
    market.add_argument("--symbol", default="BTCUSDT")
    market.add_argument("--transport", choices=("rest", "ws"), default="ws")
    market.add_argument("--streams", help="Comma-separated trade,quote,bar (REST: trade/bar)")
    market.add_argument("--interval", default="1m")
    market.add_argument("--max-events", type=int, default=10)
    market.add_argument("--duration", type=float, default=30)
    args = parser.parse_args()
    try:
        settings = load_settings(os.environ)
    except Phase0ExecutionDisabled as error:
        parser.exit(2, f"{error}\n")
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
                "phase": "phase-1",
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
