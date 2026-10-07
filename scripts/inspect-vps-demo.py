"""Read native demo identity/currency/clock before configuring a new VPS account pin."""

import argparse
import json
from pathlib import Path

from vision.apps.dashboard.mt5_market import SYMBOLS, DemoMarketReader
from vision.broker.mt5_bridge import MT5Reader

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--terminal", required=True)
parser.add_argument("--bridge-token-file", type=Path, required=True)
args = parser.parse_args()
reader = MT5Reader.connect(
    {canonical: symbol for symbol, canonical in SYMBOLS.items()},
    args.bridge_token_file.read_bytes().strip(),
    terminal_path=args.terminal,
)
DemoMarketReader(reader)  # Verify exact four-symbol base/profit identity.
snapshot = reader.snapshot()
print(
    json.dumps(
        {
            "account_identity": snapshot.account.identity,
            "currency": snapshot.account.currency,
            "demo": snapshot.account.demo,
            "connected": snapshot.account.connected,
            "quotes": [
                {
                    "symbol": q.symbol,
                    "native_source_age_seconds": (reader.clock() - q.source_at).total_seconds(),
                }
                for q in snapshot.quotes
            ],
            "phase11_acceptance": "NOT_GRANTED",
            "live_enabled": False,
            "orders_placed": 0,
        },
        indent=2,
    )
)
