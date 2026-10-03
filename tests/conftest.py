from datetime import UTC, datetime
from decimal import Decimal

import pytest

from vision.core.contracts import AssetClass, InstrumentSpec
from vision.market_data.adapters.binance import BinanceNormalizer, Subscription, epoch_ms


@pytest.fixture
def now():
    return datetime(2026, 1, 1, 0, 1, 2, tzinfo=UTC)


@pytest.fixture
def subscription():
    return Subscription("BTCUSDT")


@pytest.fixture
def normalizer(subscription):
    return BinanceNormalizer(subscription)


@pytest.fixture
def raw_trade(now):
    return {
        "e": "aggTrade",
        "s": "BTCUSDT",
        "a": 100,
        "E": epoch_ms(now),
        "T": epoch_ms(now) - 100,
        "p": "123.4567890123456789",
        "q": "0.002",
        "m": True,
    }


@pytest.fixture
def raw_quote():
    return {"u": 100, "s": "BTCUSDT", "b": "123.45", "a": "123.46", "B": "2", "A": "0"}


@pytest.fixture
def raw_bar(now):
    opening = epoch_ms(now) - 62000
    return {
        "e": "kline",
        "s": "BTCUSDT",
        "E": opening + 60000,
        "k": {
            "s": "BTCUSDT",
            "i": "1m",
            "t": opening,
            "T": opening + 59999,
            "o": "123",
            "h": "124",
            "l": "122",
            "c": "123.5",
            "v": "20",
            "x": True,
        },
    }


@pytest.fixture
def market_event(normalizer, raw_trade, now):
    return normalizer.message(raw_trade, now)


@pytest.fixture
def binance_instrument():
    return InstrumentSpec(
        instrument_id="BINANCE:SPOT:BTCUSDT",
        venue="BINANCE_SPOT",
        symbol="BTCUSDT",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        contract_size=Decimal("1"),
    )
