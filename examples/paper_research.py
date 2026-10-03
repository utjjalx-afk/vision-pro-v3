"""Synthetic offline demonstration, not a strategy or exchange/account connector."""

import json
from datetime import UTC, datetime
from decimal import Decimal

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

at = datetime(2026, 1, 1, tzinfo=UTC)
metadata = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10", "stepSize": "0.001"},
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
result = broker.submit(
    PaperOrder(
        "synthetic-entry",
        spec.instrument_id,
        record.revision,
        Side.LONG,
        Decimal(1),
        Decimal(90),
        at,
    )
)
if result.fill is None:
    raise RuntimeError("Synthetic demonstration was risk rejected")
broker.close(result.fill.position_id, "synthetic-close", at)
print(json.dumps(broker.snapshot(at).to_dict(), sort_keys=True))
