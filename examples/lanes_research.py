"""Synthetic canonical replay only; no providers, LLMs or order submission."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from vision.analysis.contracts import LaneContext, Observation, wire
from vision.analysis.runner import assess_all
from vision.core.contracts import (
    BarPayload,
    CanonicalMarketEvent,
    EventType,
    QuotePayload,
    TradePayload,
)
from vision.core.state.portfolio import DataQuality


def synthetic_context():
    at = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    instrument = "BINANCE:SPOT:BTCUSDT"
    events = []
    for index in range(5):
        opening = at - timedelta(minutes=5 - index)
        price = Decimal(100 + index)
        events.append(
            CanonicalMarketEvent(
                f"synthetic-bar-{index}",
                "synthetic.market",
                instrument,
                EventType.BAR,
                opening + timedelta(minutes=1),
                at,
                index,
                BarPayload(price, price + 1, price - 1, price, Decimal(10), 60, opening),
                source_epoch=1,
                provider_sequence=str(index),
                continuity="contiguous",
                delivery_kind="backfill",
            )
        )
    for index, maker in enumerate((False, True)):
        time = at - timedelta(seconds=1 - index)
        events.append(
            CanonicalMarketEvent(
                f"synthetic-trade-{index}",
                "synthetic.market",
                instrument,
                EventType.TRADE,
                time,
                time,
                index,
                TradePayload(Decimal(104), Decimal(3 if not maker else 1), maker),
                source_epoch=1,
                provider_sequence=str(index),
                continuity="contiguous",
            )
        )
        events.append(
            CanonicalMarketEvent(
                f"synthetic-quote-{index}",
                "synthetic.market",
                instrument,
                EventType.QUOTE,
                time,
                time,
                index,
                QuotePayload(Decimal(103), Decimal(104), Decimal(4 + index), Decimal(2)),
                source_epoch=1,
                provider_sequence=str(index),
                continuity="snapshot",
            )
        )
    observations = tuple(
        Observation(
            f"synthetic-{kind}",
            f"synthetic.{kind}",
            1,
            instrument,
            kind,
            "declared_fixture_signal",
            Decimal(value),
            "signed_signal",
            at,
            at,
            True,
        )
        for kind, value in (("macro", "-0.25"), ("narrative", "0.1"))
    )
    return LaneContext(
        instrument,
        at,
        tuple(events),
        observations,
        DataQuality(at, ((instrument, "healthy"),), lineage="synthetic-fixture"),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-context", type=Path)
    args = parser.parse_args()
    context = synthetic_context()
    if args.write_context:
        args.write_context.write_text(json.dumps(wire(context), indent=2) + "\n", encoding="utf-8")
    print(json.dumps([result.to_dict() for result in assess_all(context)], sort_keys=True))
