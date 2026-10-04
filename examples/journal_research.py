"""Synthetic offline demonstration, not a strategy or exchange/account connector."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from vision.core.contracts import (
    AssetClass,
    CanonicalMarketEvent,
    EventType,
    InstrumentSpec,
    QuotePayload,
)
from vision.core.instruments import (
    CanonicalSymbol,
    InstrumentRecord,
    QuantityUnit,
    SpecProvenance,
    digest,
)
from vision.core.state.portfolio import DataQuality, Side
from vision.execution.paper.broker import PaperBroker
from vision.execution.paper.models import PaperOrder
from vision.execution.paper.rules import rules_from_metadata
from vision.failure_memory.models import FailureRecord, FailureStatus, ValidationStage
from vision.journal.models import Experiment
from vision.journal.replay import replay
from vision.journal.repository import SQLiteRepository, canonical, export
from vision.journal.service import FailureMemory, ResearchJournal


def demo_broker():
    at = datetime(2026, 1, 1, tzinfo=UTC)
    metadata = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.001",
                        "maxQty": "10",
                        "stepSize": "0.001",
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "minNotional": "5",
                        "applyToMarket": True,
                        "avgPriceMins": 0,
                    },
                ],
            }
        ]
    }
    spec = InstrumentSpec(
        "BINANCE:SPOT:BTCUSDT",
        "BINANCE_SPOT",
        "BTCUSDT",
        AssetClass.CRYPTO,
        "BTC",
        "USDT",
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal(1),
    )
    record = InstrumentRecord(
        spec,
        CanonicalSymbol(AssetClass.CRYPTO, "BTC", "USDT"),
        QuantityUnit.BASE,
        SpecProvenance(
            "binance.spot", "synthetic-public-fixture", at, digest(metadata), "synthetic-v1"
        ),
        "linear_quote",
    )
    broker = PaperBroker("USDT", Decimal(10000), at)
    broker.register(record, rules_from_metadata(record, metadata))
    quote = CanonicalMarketEvent(
        "synthetic-quote",
        "binance.spot",
        spec.instrument_id,
        EventType.QUOTE,
        at,
        at,
        1,
        QuotePayload(Decimal(100), Decimal(101), Decimal(10), Decimal(10)),
    )
    broker.update_quote(quote, DataQuality(at, ((spec.instrument_id, "healthy"),)))
    return broker, record, at


def main():
    parser = argparse.ArgumentParser(description="Synthetic offline durable research example")
    parser.add_argument("--database", required=True)
    parser.add_argument("--export", required=True)
    parser.add_argument("--commit-sha", required=True)
    args = parser.parse_args()
    if Path(args.database).exists() or Path(args.export).exists():
        raise ValueError("New demonstration database and export required")
    broker, record, at = demo_broker()
    clock = [at]
    repository = SQLiteRepository(args.database)
    try:
        journal = ResearchJournal(repository, clock=lambda: clock[0])
        journal.open_experiment(
            Experiment(
                "synthetic-paper",
                "synthetic-family",
                canonical({"fixture": True}),
                args.commit_sha,
                (record.revision,),
                (("binance.spot", 0),),
                None,
                broker.costs,
                at,
            )
        )
        entry = broker.submit(
            PaperOrder(
                "synthetic-entry",
                record.spec.instrument_id,
                record.revision,
                Side.LONG,
                Decimal(1),
                Decimal(90),
                at,
            )
        )
        if entry.fill is None:
            raise RuntimeError("Synthetic paper entry rejected")
        journal.paper("synthetic-paper", broker.checkpoint(), "synthetic-entry")
        clock[0] += timedelta(seconds=1)
        quote = CanonicalMarketEvent(
            "synthetic-exit-quote",
            "binance.spot",
            record.spec.instrument_id,
            EventType.QUOTE,
            clock[0],
            clock[0],
            2,
            QuotePayload(Decimal(110), Decimal(111), Decimal(10), Decimal(10)),
        )
        broker.update_quote(quote, DataQuality(clock[0], ((record.spec.instrument_id, "healthy"),)))
        broker.close(entry.fill.position_id, "synthetic-close", clock[0])
        receipt = journal.paper("synthetic-paper", broker.checkpoint(), "synthetic-entry")
        journal.finalize("synthetic-paper", receipt.entry_id)
        FailureMemory(journal).record(
            "synthetic-paper",
            FailureRecord(
                "unvalidated-variant",
                FailureStatus.BLOCKED,
                ValidationStage.VALIDATION,
                "Synthetic demonstration provides no strategy edge evidence",
                ("synthetic-exit-quote",),
            ),
        )
        value = export(repository)
        report = replay(value)
        with Path(args.export).open("x", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        print(json.dumps(report, sort_keys=True))
    finally:
        repository.close()


if __name__ == "__main__":
    main()
