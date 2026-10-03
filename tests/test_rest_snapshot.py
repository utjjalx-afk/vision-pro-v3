from datetime import timedelta

from vision.market_data.adapters.binance import Subscription
from vision.market_data.hub import MarketDataHub


class SnapshotSource:
    def __init__(self, instrument, trades, bars):
        self.spec = instrument
        self.trade_rows = trades
        self.bar_rows = bars

    def instrument(self, symbol):
        return self.spec

    def trades(self, symbol, *, limit):
        return self.trade_rows[:limit]

    def bars(self, subscription, *, limit):
        return self.bar_rows[-limit:]


def test_rest_hub_snapshot_admits_only_recent_data(now, raw_trade, binance_instrument):
    recent = {key: raw_trade[key] for key in ("a", "p", "q", "T", "m")}
    source = SnapshotSource(
        binance_instrument,
        [
            {**recent, "a": 99, "T": recent["T"] - 10000},
            recent,
            {**recent, "a": 101},
            {**recent, "a": 102, "p": "NaN"},
        ],
        [],
    )
    hub = MarketDataHub(clock=lambda: now)
    events = hub.snapshot(source, Subscription("BTCUSDT", ("trade",)))
    assert [event.sequence for event in events] == [100, 101]
    assert hub.rejected == {"stale_source": 1, "malformed": 1}
    assert len(hub.bus) == 0


def test_rest_latest_finalized_bar_only(now, raw_bar, binance_instrument):
    kline = raw_bar["k"]
    closed = [
        kline["t"],
        kline["o"],
        kline["h"],
        kline["l"],
        kline["c"],
        kline["v"],
        kline["T"],
        "0",
        0,
        "0",
        "0",
        "0",
    ]
    opened = list(closed)
    opened[0] += 60000
    opened[6] += 60000
    source = SnapshotSource(binance_instrument, [], [closed, opened])
    hub = MarketDataHub(clock=lambda: now)
    events = hub.snapshot(source, Subscription("BTCUSDT", ("bar",)))
    assert len(events) == 1
    assert events[0].payload.open_ts == now - timedelta(seconds=62)
