"""Load public metadata with explicit Spot unit semantics and provenance."""

from datetime import UTC, datetime

from vision.core.instruments import (
    CanonicalSymbol,
    InstrumentRecord,
    QuantityUnit,
    SpecProvenance,
    digest,
)
from vision.market_data.adapters.binance import BinanceREST, MarketDataError, symbol_name
from vision.market_data.adapters.bybit import BybitREST


def refresh_public_spec(registry, rest, symbol, *, clock=lambda: datetime.now(UTC)):
    symbol_name(symbol)
    if isinstance(rest, BybitREST):
        endpoint, source = "instruments-info", "bybit.spot"
    elif isinstance(rest, BinanceREST):
        endpoint, source = "exchangeInfo", "binance.spot"
    else:
        raise ValueError("Only supported public Spot metadata providers are allowed")
    try:
        metadata = rest.get(endpoint, {"symbol": symbol})
        spec = rest.parse_instrument(symbol, metadata)
        record = InstrumentRecord(
            spec,
            CanonicalSymbol(spec.asset_class, spec.base_currency, spec.quote_currency),
            QuantityUnit.BASE,
            SpecProvenance(
                source,
                f"{rest.BASE}/{endpoint}",
                clock(),
                digest(metadata),
                "spot-base-quantity-v1",
            ),
            "linear_quote",
        )
        return registry.register(record)
    except (ValueError, MarketDataError):
        venue = source.split(".")[0].upper()
        registry.invalidate(f"{venue}:SPOT:{symbol}")
        raise
