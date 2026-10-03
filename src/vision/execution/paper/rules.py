"""Public metadata rules for a deliberately limited deterministic MARKET simulator."""

from decimal import Decimal
from math import gcd, lcm

from vision.core.codec import decimal_string
from vision.core.instruments import digest
from vision.core.state.portfolio import number
from vision.execution.paper.models import MarketRules, exact


def common_step(first, second):
    number(first)
    number(second)
    if first <= 0 or second < 0:
        raise ValueError("Invalid explicit quantity increments")
    if not second:
        return first
    a, b = first.as_integer_ratio()
    c, d = second.as_integer_ratio()
    with exact():
        return Decimal(lcm(a, c)) / Decimal(gcd(b, d))


def rules_from_metadata(record, metadata):
    if record.provenance.metadata_digest != digest(metadata):
        raise ValueError("Market rules require original metadata provenance")
    spec = record.spec
    try:
        if spec.venue == "BINANCE_SPOT":
            rows = metadata["symbols"]
            if len(rows) != 1 or rows[0]["symbol"] != spec.symbol:
                raise ValueError("Wrong rules")
            filters = {row["filterType"]: row for row in rows[0]["filters"]}
            lot = filters["LOT_SIZE"]
            market = filters.get("MARKET_LOT_SIZE", lot)
            # Zero MARKET step/min disables only that override, not LOT_SIZE.
            step = common_step(decimal_string(lot["stepSize"]), decimal_string(market["stepSize"]))
            minimum = max(decimal_string(lot["minQty"]), decimal_string(market["minQty"]))
            market_max = decimal_string(market["maxQty"])
            maximum = (
                min(decimal_string(lot["maxQty"]), market_max)
                if market_max
                else decimal_string(lot["maxQty"])
            )
            notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL"))
            if notional is None:
                raise ValueError("Notional constraint unavailable")
            if notional.get("avgPriceMins", 0) != 0:
                raise ValueError("Reference/average-price market notional is unsupported")
            minimum_notional = decimal_string(notional.get("minNotional", notional.get("notional")))
            maximum_notional = (
                decimal_string(notional["maxNotional"])
                if "maxNotional" in notional and notional.get("applyMaxToMarket") is True
                else None
            )
        elif spec.venue == "BYBIT_SPOT":
            rows = metadata["list"]
            if len(rows) != 1 or rows[0]["symbol"] != spec.symbol:
                raise ValueError("Wrong rules")
            lot = rows[0]["lotSizeFilter"]
            step = decimal_string(lot["basePrecision"])
            minimum = spec.minimum_quantity
            maximum = decimal_string(lot["maxMarketOrderQty"])
            minimum_notional = decimal_string(lot["minOrderAmt"])
            maximum_notional = None
        else:
            raise ValueError("Margin model is unverified")
        return MarketRules(
            spec.instrument_id,
            record.revision,
            minimum,
            maximum,
            step,
            minimum_notional,
            maximum_notional,
            digest(metadata),
        )
    except (KeyError, TypeError, IndexError):
        raise ValueError("Incomplete verified MARKET constraints") from None
