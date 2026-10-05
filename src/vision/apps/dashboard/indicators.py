"""Bounded chart analytics. Missing warm-up stays None, never an invented zero."""

from decimal import Decimal

D = Decimal


def sma(values, period):
    out = []
    for i in range(len(values)):
        w = values[max(0, i - period + 1) : i + 1]
        out.append(sum(w, D(0)) / period if len(w) == period and None not in w else None)
    return out


def smooth(values, period, *, wilder=False):
    out, previous = [], None
    alpha = D(1) / period if wilder else D(2) / (period + 1)
    for i, value in enumerate(values):
        if value is None:
            previous = None
        elif previous is None:
            w = values[max(0, i - period + 1) : i + 1]
            if len(w) == period and None not in w:
                previous = sum(w, D(0)) / period
        else:
            previous += alpha * (value - previous)
        out.append(previous)
    return out


def calculate(bars):
    if len(bars) > 512:
        raise ValueError("Bounded indicator window required")
    if not bars:
        return {}
    close = [b.close for b in bars]
    high, low = [b.high for b in bars], [b.low for b in bars]
    result = {f"ema{p}": smooth(close, p) for p in (20, 50, 100, 200)}
    result["sma20"] = mean = sma(close, 20)
    deviation = []
    for i, m in enumerate(mean):
        deviation.append(
            None if m is None else (sum((v - m) ** 2 for v in close[i - 19 : i + 1]) / 20).sqrt()
        )
    result["bbUpper"] = [
        None if m is None else m + 2 * s for m, s in zip(mean, deviation, strict=True)
    ]
    result["bbLower"] = [
        None if m is None else m - 2 * s for m, s in zip(mean, deviation, strict=True)
    ]
    volume, pv, day, vwap = D(0), D(0), None, []
    complete = False
    for b in bars:
        date = b.open_ts.date() if b.open_ts else None
        if date != day:
            volume, pv, day = D(0), D(0), date
            complete = bool(
                b.open_ts and b.open_ts.hour == b.open_ts.minute == b.open_ts.second == 0
            )
        if b.volume_basis == "price_count":
            vwap.append(None)
            continue
        volume += b.volume
        pv += ((b.high + b.low + b.close) / 3) * b.volume
        vwap.append(pv / volume if volume and complete else None)
    result["vwap"] = vwap
    changes = [None] + [b - a for a, b in zip(close, close[1:], strict=False)]
    gains = smooth([None if v is None else max(v, D(0)) for v in changes], 14, wilder=True)
    losses = smooth([None if v is None else max(-v, D(0)) for v in changes], 14, wilder=True)
    result["_gain"] = gains
    result["_loss"] = losses
    result["rsi"] = [
        None
        if g is None
        else D(50)
        if g == loss == 0
        else D(100)
        if loss == 0
        else 100 - 100 / (1 + g / loss)
        for g, loss in zip(gains, losses, strict=True)
    ]
    tr, plus, minus = [], [None], [None]
    for i, b in enumerate(bars):
        tr.append(
            b.high - b.low
            if i == 0
            else max(b.high - b.low, abs(b.high - close[i - 1]), abs(b.low - close[i - 1]))
        )
        if i:
            up, down = b.high - high[i - 1], low[i - 1] - b.low
            plus.append(up if up > down and up > 0 else D(0))
            minus.append(down if down > up and down > 0 else D(0))
    result["atr"] = atr = smooth(tr, 14, wilder=True)
    ps, ms = smooth(plus, 14, wilder=True), smooth(minus, 14, wilder=True)
    result["_plus"] = ps
    result["_minus"] = ms
    pdi = [
        None if p is None or a is None else D(0) if a == 0 else 100 * p / a
        for p, a in zip(ps, atr, strict=True)
    ]
    mdi = [
        None if m is None or a is None else D(0) if a == 0 else 100 * m / a
        for m, a in zip(ms, atr, strict=True)
    ]
    dx = [
        None if p is None or m is None else D(0) if p + m == 0 else 100 * abs(p - m) / (p + m)
        for p, m in zip(pdi, mdi, strict=True)
    ]
    result["adx"] = smooth(dx, 14, wilder=True)
    fast, slow = smooth(close, 12), smooth(close, 26)
    result["_fast"] = fast
    result["_slow"] = slow
    result["macd"] = macd = [
        None if f is None or s is None else f - s for f, s in zip(fast, slow, strict=True)
    ]
    result["macdSignal"] = signal = smooth(macd, 9)
    result["macdHist"] = [
        None if m is None or s is None else m - s for m, s in zip(macd, signal, strict=True)
    ]
    raw = []
    for i, c in enumerate(close):
        if i < 13:
            raw.append(None)
        else:
            lo, hi = min(low[i - 13 : i + 1]), max(high[i - 13 : i + 1])
            raw.append(D(50) if hi == lo else 100 * (c - lo) / (hi - lo))
    result["stochK"] = sma(raw, 3)
    result["stochD"] = sma(result["stochK"], 3)
    result["volume"] = [b.volume for b in bars]
    return result


class IndicatorWindow:
    """Carry recursive state across display retention; corrections reseed explicitly."""

    def __init__(self):
        self.bars = []
        self.series = {}
        self.session_day = None
        self.session_volume = D(0)
        self.session_pv = D(0)
        self.session_complete = False

    def reset(self, bars):
        self.bars = list(bars)
        self.series = calculate(self.bars)
        self.session_day = None
        self.session_volume, self.session_pv = D(0), D(0)
        self.session_complete = False
        for b in bars:
            self._session(b)

    def _session(self, b):
        day = b.open_ts.date() if b.open_ts else None
        if day != self.session_day:
            self.session_day = day
            self.session_volume, self.session_pv = D(0), D(0)
            self.session_complete = bool(
                b.open_ts and b.open_ts.hour == b.open_ts.minute == b.open_ts.second == 0
            )
        if b.volume_basis == "price_count":
            return None
        self.session_volume += b.volume
        self.session_pv += ((b.high + b.low + b.close) / 3) * b.volume
        return (
            self.session_pv / self.session_volume
            if self.session_complete and self.session_volume
            else None
        )

    def append(self, b):
        if not self.bars or b.open_ts <= self.bars[-1].open_ts:
            raise ValueError("Indicator append requires a strictly newer candle")
        previous = self.bars[-1]
        last = {k: values[-1] for k, values in self.series.items()}
        window = (self.bars + [b])[-512:]
        result = calculate(window)

        def carry(key, value, period, wilder=False):
            old = last.get(key)
            if old is not None:
                alpha = D(1) / period if wilder else D(2) / (period + 1)
                result[key][-1] = old + alpha * (value - old)
            return result[key][-1]

        for period in (20, 50, 100, 200):
            carry(f"ema{period}", b.close, period)
        fast, slow = carry("_fast", b.close, 12), carry("_slow", b.close, 26)
        if fast is not None and slow is not None:
            result["macd"][-1] = macd = fast - slow
            signal = carry("macdSignal", macd, 9)
            result["macdHist"][-1] = macd - signal if signal is not None else None
        change = b.close - previous.close
        gain = carry("_gain", max(change, D(0)), 14, True)
        loss = carry("_loss", max(-change, D(0)), 14, True)
        if gain is not None and loss is not None:
            result["rsi"][-1] = (
                D(50)
                if gain == loss == 0
                else D(100)
                if loss == 0
                else 100 - 100 / (1 + gain / loss)
            )
        tr = max(b.high - b.low, abs(b.high - previous.close), abs(b.low - previous.close))
        carry("atr", tr, 14, True)
        up, down = b.high - previous.high, previous.low - b.low
        plus = carry("_plus", up if up > down and up > 0 else D(0), 14, True)
        minus = carry("_minus", down if down > up and down > 0 else D(0), 14, True)
        if plus is not None and minus is not None:
            dx = D(0) if plus + minus == 0 else 100 * abs(plus - minus) / (plus + minus)
            carry("adx", dx, 14, True)
        result["vwap"][-1] = self._session(b)
        # Historical points stay stable; append only one point to each retained series.
        self.series = {
            k: (self.series.get(k, []) + [values[-1]])[-512:] for k, values in result.items()
        }
        self.bars = window
