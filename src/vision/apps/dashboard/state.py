"""Read projections of admitted canonical events and verified backend evidence."""

from collections import OrderedDict, deque
from datetime import UTC, datetime

from vision.analysis.context import capture_from_hub
from vision.analysis.contracts import wire
from vision.analysis.runner import assess_all
from vision.analysis.synthesizer.contracts import SynthesisInput
from vision.analysis.synthesizer.engine import synthesize
from vision.apps.dashboard.flow import DepthBook, Footprint
from vision.apps.dashboard.indicators import IndicatorWindow
from vision.core.contracts import BarPayload, QuotePayload, TradePayload
from vision.market_data.hub import MarketDataHub
from vision.risk.governor import HardRiskGovernor, RiskLimits


class DashboardState:
    def __init__(self, *, clock=lambda: datetime.now(UTC)):
        self.clock, self.hub = clock, MarketDataHub(clock=clock)
        self.bars, self.events, self.flow, self.quotes, self.books = {}, {}, {}, {}, {}
        self.version, self.broker = 0, None
        self.journal, self.journal_report = (), None
        self.connections = {
            s: {"state": "UNAVAILABLE", "reason": "NOT_CONFIGURED"}
            for s in ("binance.spot", "bybit.spot", "oanda", "mt5")
        }
        self.timeline = deque(maxlen=200)
        self.ofi, self.previous_quotes = {}, {}
        self._indicator_cache = {}
        self._indicator_windows = {}
        self.flow_faults = {}

    def history(self, events):
        for e in events:
            if e.delivery_kind != "backfill" or not isinstance(e.payload, BarPayload):
                raise ValueError("Explicit canonical historical bars required")
            self._bar(e)

    def _bar(self, e):
        key = (e.instrument_id, e.payload.interval_seconds)
        series = self.bars.setdefault(key, OrderedDict())
        if series and next(iter(series.values())).source != e.source:
            series.clear()
        t = int(e.payload.open_ts.timestamp()) if e.payload.open_ts else None
        if t is None:
            raise ValueError("Explicit candle opening time required")
        previous_time = next(reversed(series)) if series else None
        engine = self._indicator_windows.get(key)
        if engine is not None and previous_time is not None and t > previous_time:
            engine.append(e.payload)
        else:
            self._indicator_windows.pop(key, None)
        series[t] = e
        self.bars[key] = OrderedDict(sorted(series.items())[-512:])
        self._indicator_cache.pop(key, None)

    def admitted(self, e):
        """Called only after owning hub accepts and drains its bus."""
        self.version += 1
        self.events.setdefault(e.instrument_id, deque(maxlen=512)).append(e)
        if isinstance(e.payload, BarPayload):
            self._bar(e)
        elif isinstance(e.payload, TradePayload):
            spec = self.hub.instruments[e.instrument_id]
            for seconds in (60, 300, 900, 3600, 14400, 86400):
                aggregator = self.flow.setdefault(
                    (e.instrument_id, seconds), Footprint(spec.tick_size * 100, seconds)
                )
                try:
                    if (e.instrument_id, seconds) not in self.flow_faults:
                        aggregator.admit(e)
                except ValueError:
                    self.flow_faults[(e.instrument_id, seconds)] = (
                        "FLOW_CAPACITY_OR_CONTINUITY_BLOCKED"
                    )
        elif isinstance(e.payload, QuotePayload):
            key = (e.source, e.instrument_id, e.source_epoch)
            previous = self.previous_quotes.get(key)
            q = e.payload
            if previous is not None:
                # Cont et al. level-one OFI, explicitly base-quantity units.
                self.ofi[key] = self.ofi.get(key, 0) + (
                    (q.bid_quantity if q.bid >= previous.bid else 0)
                    - (previous.bid_quantity if q.bid <= previous.bid else 0)
                    - (q.ask_quantity if q.ask <= previous.ask else 0)
                    + (previous.ask_quantity if q.ask >= previous.ask else 0)
                )
            self.previous_quotes[key] = q
            self.quotes[e.instrument_id] = e
        self.timeline.appendleft(
            {
                "id": e.event_id,
                "kind": e.event_type.value,
                "source": e.source,
                "at": e.received_ts.isoformat(),
            }
        )

    def book(self, instrument):
        return self.books.setdefault(instrument, DepthBook("binance.spot", instrument))

    def broker_projection(self):
        b, now = self.broker, self.clock()
        if b is None:
            return {"state": "UNAVAILABLE", "reason": "BRIDGE_NOT_CONNECTED", "snapshot": None}
        age = (now - b.as_of).total_seconds()
        reasons = []
        if not b.account.demo:
            reasons.append("REAL_ACCOUNT_BLOCKED")
        if not b.account.connected:
            reasons.append("BROKER_DISCONNECTED")
        if not 0 <= age <= 5:
            reasons.append("STALE_OR_FUTURE_BROKER_SNAPSHOT")
        for q in b.quotes:
            seconds = (now - q.source_at).total_seconds()
            if seconds < 0:
                reasons.append("FUTURE_BROKER_QUOTE")
            elif seconds > 5:
                reasons.append("STALE_BROKER_QUOTE")
        quote_status = [
            {
                "symbol": q.symbol,
                "source_age_seconds": (now - q.source_at).total_seconds(),
                "receipt_age_seconds": (now - q.observed_at).total_seconds(),
                "state": "BLOCKED"
                if not 0 <= (now - q.source_at).total_seconds() <= 5
                else "HEALTHY",
            }
            for q in b.quotes
        ]
        return {
            "state": "BLOCKED" if reasons else "CONNECTED_DEMO",
            "reasons": sorted(set(reasons)),
            "quote_status": quote_status,
            "connected": b.account.connected,
            "account_mode": "DEMO" if b.account.demo else "REAL",
            "age_seconds": age,
            "snapshot_id": b.snapshot_id,
            "snapshot": wire(b),
        }

    def lanes(self, instrument, seconds):
        if instrument not in self.hub.instruments:
            return {"assessments": [], "decision": None, "reason": "NO_CANONICAL_CONTEXT"}
        recent = [
            e for e in self.events.get(instrument, ()) if not isinstance(e.payload, BarPayload)
        ][-256:]
        selected_bars = list(self.bars.get((instrument, seconds), {}).values())[-200:]
        events = sorted(recent + selected_bars, key=lambda e: (e.received_ts, e.event_id))
        try:
            context = capture_from_hub(self.hub, instrument, events[-512:])
            lanes = assess_all(context)
            record = self.hub.registry.get(instrument)
            decision = (
                None
                if record is None
                else synthesize(SynthesisInput(record, context.as_of, lanes, (), context.quality))
            )
            return {
                "assessments": [wire(a) for a in lanes],
                "decision": wire(decision),
                "reason": None,
                "reliability_basis": "No attached prospective outcomes; lanes UNPROVEN",
            }
        except ValueError:
            return {"assessments": [], "decision": None, "reason": "INVALID_OR_STALE_CONTEXT"}

    def market(self, instrument, seconds):
        now = self.clock()
        values = list(self.bars.get((instrument, seconds), {}).values())
        spec = self.hub.instruments.get(instrument)
        candles = [
            {
                "time": int(e.payload.open_ts.timestamp()),
                **{
                    k: str(getattr(e.payload, k))
                    for k in ("open", "high", "low", "close", "volume")
                },
                "event_id": e.event_id,
                "delivery_kind": e.delivery_kind,
                "source_epoch": e.source_epoch,
            }
            for e in values
        ]
        key = (instrument, seconds)
        if key not in self._indicator_cache:
            engine = self._indicator_windows.get(key)
            if engine is None:
                engine = IndicatorWindow()
                engine.reset([e.payload for e in values])
                self._indicator_windows[key] = engine
            self._indicator_cache[key] = engine.series
        indicators = {
            name: [
                {"time": candles[i]["time"], "value": str(v)}
                for i, v in enumerate(series)
                if v is not None
            ]
            for name, series in self._indicator_cache[key].items()
            if not name.startswith("_")
        }
        quote = self.quotes.get(instrument)
        age = (now - quote.source_ts).total_seconds() if quote else None
        receipt_age = (now - quote.received_ts).total_seconds() if quote else None
        healthy = age is not None and 0 <= age <= 5 and 0 <= receipt_age <= 5
        statuses = self.hub.gate.snapshot(now)
        prefix = f"{quote.source}:{instrument}" if quote else ""
        live_states = [
            str(statuses.get(f"{prefix}:{kind}", "warming_up")) for kind in ("quote", "trade")
        ]
        if quote is None:
            health = "UNAVAILABLE"
        elif not healthy:
            health = "STALE"
        else:
            health = next(
                (
                    state.upper()
                    for state in ("gap", "disconnected", "stale", "warming_up", "degraded")
                    if state in live_states
                ),
                "HEALTHY",
            )
        healthy = health == "HEALTHY"
        candle_health = str(statuses.get(f"{prefix}:bar:{seconds}", "warming_up")).upper()
        f = self.flow.get(key)
        flow = f.snapshot() if f else {"candles": [], "trades": [], "reason": "NO_TRADE_FEED"}
        flow["health"] = (
            "BLOCKED"
            if key in self.flow_faults
            else str(statuses.get(f"{prefix}:trade", "warming_up")).upper()
        )
        flow["reason"] = self.flow_faults.get(key)
        flow["signals_permitted"] = healthy and key not in self.flow_faults
        ofi_key = (quote.source, instrument, quote.source_epoch) if quote else None
        flow["ofi"] = str(self.ofi[ofi_key]) if ofi_key in self.ofi else None
        flow["ofi_basis"] = "level_one_base_quantity_since_epoch_start"
        indicators["cvd"] = [{"time": c["time"], "value": c["cvd"]} for c in flow["candles"]]
        indicators["delta"] = [{"time": c["time"], "value": c["delta"]} for c in flow["candles"]]
        return {
            "instrument_id": instrument,
            "spec": spec.to_dict() if spec else None,
            "source": quote.source if quote else values[-1].source if values else None,
            "source_epoch": quote.source_epoch if quote else None,
            "quote_age_seconds": age,
            "receipt_age_seconds": receipt_age,
            "candle_age_seconds": (now - values[-1].source_ts).total_seconds() if values else None,
            "health": health,
            "new_signals_permitted": healthy and candle_health == "HEALTHY",
            "candle_health": candle_health,
            "quote_timestamp_basis": quote.timestamp_basis.value if quote else None,
            "candles": candles,
            "indicators": indicators,
            "quote": quote.to_dict() if quote else None,
            "flow": flow,
            "depth": self.book(instrument).snapshot(now),
            "derivatives": {
                "funding": None,
                "open_interest": None,
                "liquidations": None,
                "reason": "SPOT_FEED_NO_DERIVATIVE_METRICS",
            },
        }

    def summary(self, instrument="BINANCE:SPOT:BTCUSDT", seconds=300):
        market, broker = self.market(instrument, seconds), self.broker_projection()
        reasons = HardRiskGovernor(RiskLimits()).assess(
            equity=None,
            trade_risk=None,
            open_risk=None,
            projected_equity=None,
            symbol_exposure=None,
            gross_exposure=None,
            daily_baseline=None,
            high_water=None,
        )
        risk = {
            "state": "BLOCKED",
            "reasons": list(reasons) + ["NO_APPROVED_RISK_CONTEXT"],
            "open_risk": None,
            "daily_equity_loss": None,
            "drawdown": None,
            "risk_limit_fraction": "0.05",
            "source": "HardRiskGovernor",
        }
        return {
            "schema_version": "phase11.5-v1",
            "version": self.version,
            "as_of": self.clock().isoformat(),
            "mode": "READ_ONLY / PAPER / DEMO",
            "live_enabled": False,
            "live_eligible": False,
            "phase11": "IMPLEMENTED / ACCEPTANCE PENDING",
            "market": market,
            "agents": self.lanes(instrument, seconds),
            "risk": risk,
            "broker": broker,
            "execution": {
                "state": "DISARMED",
                "read_only": True,
                "reason": "PHASE11_ACCEPTANCE_PENDING",
                "timeline": [],
            },
            "portfolio": {
                "source": "MT5",
                "account": broker["snapshot"]["account"] if broker["snapshot"] else None,
                "positions": broker["snapshot"]["positions"] if broker["snapshot"] else [],
                "unavailable_metrics": ["realized_pnl", "daily_pnl", "open_risk"],
            },
            "journal": self.journal_report,
            "journal_entries": list(self.journal[-200:]),
            "timeline": list(self.timeline),
            "connections": self.connections,
            "instruments": [s.to_dict() for s in self.hub.instruments.values()],
            "system": {
                "hub": self.hub.status(),
                "retention": {"candles": 512, "events": 512, "footprint_candles": 64},
                "execution_controls": False,
            },
        }
