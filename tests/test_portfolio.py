import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from vision.core.contracts import AssetClass, EventType, TimestampBasis
from vision.core.instruments import (
    CanonicalSymbol,
    InstrumentRecord,
    InstrumentRegistry,
    QuantityUnit,
    SpecProvenance,
    SymbolCatalog,
    digest,
)
from vision.core.state.portfolio import (
    DataQuality,
    Mark,
    PortfolioPolicy,
    PortfolioState,
    Position,
    Readiness,
    Side,
    evaluate,
)
from vision.core.state.replay import inputs_from_dict, record_from_dict, replay
from vision.market_data.adapters.binance import BinanceREST, Subscription
from vision.market_data.adapters.bybit import BybitREST
from vision.market_data.health.gate import HealthDecision, HealthStatus, stream_key
from vision.market_data.hub import MarketDataHub
from vision.market_data.instruments import refresh_public_spec


def record(spec, now, *, unit=QuantityUnit.BASE):
    return InstrumentRecord(
        spec,
        CanonicalSymbol(spec.asset_class, spec.base_currency, spec.quote_currency),
        unit,
        SpecProvenance(
            "binance.spot" if spec.venue == "BINANCE_SPOT" else "fixture",
            "synthetic-public-fixture",
            now,
            digest(spec.to_dict()),
            "fixture-v1",
        ),
        "linear_quote",
    )


def position(rec, now, *, side=Side.LONG, quantity="2", entry="100", identity="p1"):
    return Position(
        identity,
        rec.spec.instrument_id,
        rec.revision,
        side,
        Decimal(quantity),
        rec.quantity_unit,
        Decimal(entry),
        now - timedelta(seconds=10),
        "synthetic-input",
    )


def mark(rec, now, *, price="110", seq=1):
    return Mark(
        rec.spec.instrument_id,
        rec.revision,
        Decimal(price),
        now,
        now,
        f"mark-{seq}",
        rec.provenance.source,
        1,
        seq,
        TimestampBasis.EXCHANGE,
        "contiguous",
        "healthy",
        EventType.TRADE,
        str(seq),
    )


@pytest.fixture
def portfolio(now, binance_instrument):
    rec = record(binance_instrument, now)
    registry = InstrumentRegistry()
    registry.register(rec)
    state = PortfolioState(registry, clock=lambda: now)
    state.replace_positions((position(rec, now),))
    state.marks.put(mark(rec, now))
    return state, rec


@pytest.mark.parametrize(
    "side,price,pnl,net",
    [
        (Side.LONG, "110", "20", "220"),
        (Side.SHORT, "110", "-20", "-220"),
        (Side.LONG, "90", "-20", "180"),
        (Side.SHORT, "90", "20", "-180"),
    ],
)
def test_long_short_pnl(portfolio, now, side, price, pnl, net):
    state, rec = portfolio
    state.replace_positions((position(rec, now, side=side),))
    state.marks.put(mark(rec, now, price=price, seq=2))
    snap = state.snapshot("USDT")
    assert snap.readiness is Readiness.READY
    assert (snap.unrealized_pnl, snap.net_exposure, snap.gross_exposure) == (
        Decimal(pnl),
        Decimal(net),
        abs(Decimal(net)),
    )


@pytest.mark.parametrize("multiplier", ["0.01", "10", "100", "100000"])
def test_explicit_instrument_contract_size(now, binance_instrument, multiplier):
    spec = replace(
        binance_instrument,
        instrument_id="FIXTURE:XAUUSD",
        venue="FIXTURE",
        symbol="XAUUSD",
        asset_class=AssetClass.METALS,
        base_currency="XAU",
        quote_currency="USD",
        contract_size=Decimal(multiplier),
    )
    rec = record(spec, now, unit=QuantityUnit.CONTRACTS)
    registry = InstrumentRegistry()
    registry.register(rec)
    state = PortfolioState(registry, clock=lambda: now)
    state.replace_positions((position(rec, now, quantity="0.5", entry="2000"),))
    state.marks.put(mark(rec, now, price="2010"))
    snap = state.snapshot("USD")
    assert snap.unrealized_pnl == Decimal("5") * Decimal(multiplier)
    assert snap.gross_exposure == Decimal("1005") * Decimal(multiplier)


def test_context_independent_exact_decimals(portfolio, now):
    state, rec = portfolio
    state.replace_positions(
        (
            position(
                rec,
                now,
                quantity="0.000000000000000000001",
                entry="1000000000000000000000.000000001",
            ),
        )
    )
    state.marks.put(mark(rec, now, price="1000000000000000000000.000000003", seq=2))
    with localcontext() as context:
        context.prec = 3
        snap = state.snapshot("USDT")
    assert snap.unrealized_pnl == Decimal("2E-30")


def test_gross_does_not_net_opposing_positions(portfolio, now):
    state, rec = portfolio
    state.replace_positions(
        (position(rec, now), position(rec, now, side=Side.SHORT, identity="short"))
    )
    snap = state.snapshot("USDT")
    assert snap.net_exposure == 0 and snap.gross_exposure == 440 and snap.unrealized_pnl == 0


def test_empty_portfolio_explicit_currency(now):
    snap = PortfolioState(InstrumentRegistry(), clock=lambda: now).snapshot("USD")
    assert snap.readiness is Readiness.READY and snap.gross_exposure == Decimal(0)


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        Decimal("NaN"),
        Decimal(-1),
        Decimal(0),
        Decimal("Infinity"),
        Decimal("1E101"),
        Decimal("1" * 65),
    ],
)
def test_invalid_position_decimals(portfolio, now, value):
    _, rec = portfolio
    with pytest.raises(ValueError):
        replace(position(rec, now), quantity=value)


@pytest.mark.parametrize(
    "field,value", [("side", "long"), ("quantity_unit", "base"), ("position_id", " ")]
)
def test_typed_position_inputs(portfolio, now, field, value):
    _, rec = portfolio
    with pytest.raises(ValueError):
        replace(position(rec, now), **{field: value})


def test_frozen_snapshot_inputs(portfolio):
    state, rec = portfolio
    snap = state.snapshot("USDT")
    for item, field, value in (
        (snap, "gross_exposure", Decimal(1)),
        (snap.inputs.positions[0], "quantity", Decimal(1)),
        (snap.inputs.marks[0], "price", Decimal(1)),
        (rec, "quantity_unit", QuantityUnit.CONTRACTS),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(item, field, value)
    state.replace_positions(())
    assert len(snap.inputs.positions) == 1


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "old",
        "future",
        "source_old",
        "spec_missing",
        "spec_future",
        "spec_old",
        "unit",
        "revision",
        "unsupported",
        "position_future",
        "mark_spec",
    ],
)
def test_fail_closed_readiness(portfolio, now, problem):
    state, rec = portfolio
    expected = Readiness.INSTRUMENT_SPEC_INCOMPLETE
    if problem == "missing":
        state.marks._marks.clear()
        expected = Readiness.MISSING_MARKS
    elif problem in {"old", "future", "source_old"}:
        offset = timedelta(seconds=6) if problem != "future" else -timedelta(seconds=1)
        old = state.marks._marks[rec.spec.instrument_id]
        state.marks._marks[rec.spec.instrument_id] = replace(
            old,
            source_ts=now - offset,
            received_ts=now if problem == "source_old" else now - offset,
        )
        expected = Readiness.STALE_MARKS
    elif problem == "spec_missing":
        state.registry._current.clear()
    elif problem in {"spec_future", "spec_old"}:
        offset = timedelta(days=2) if problem == "spec_old" else -timedelta(seconds=1)
        state.registry._current[rec.spec.instrument_id] = replace(
            rec, provenance=replace(rec.provenance, observed_at=now - offset)
        )
    elif problem == "unsupported":
        state.registry.register(replace(rec, valuation_model="unsupported"))
    elif problem in {"unit", "revision", "position_future"}:
        changes = {
            "unit": {"quantity_unit": QuantityUnit.CONTRACTS},
            "revision": {"spec_revision": "unknown"},
            "position_future": {"opened_at": now + timedelta(seconds=1)},
        }[problem]
        state.replace_positions((replace(state._positions[0], **changes),))
    elif problem == "mark_spec":
        state.marks._marks[rec.spec.instrument_id] = replace(
            state.marks._marks[rec.spec.instrument_id], spec_revision="unknown"
        )
    snap = state.snapshot("USDT")
    assert snap.readiness is expected and snap.unrealized_pnl is None and not snap.valuations


def test_usdt_not_usd_mixed_currency_safety(portfolio, now):
    state, rec = portfolio
    usd = record(
        replace(
            rec.spec,
            instrument_id="FIXTURE:EURUSD",
            venue="FIXTURE",
            symbol="EURUSD",
            asset_class=AssetClass.FOREX,
            base_currency="EUR",
            quote_currency="USD",
        ),
        now,
    )
    state.registry.register(usd)
    state.replace_positions((position(rec, now), position(usd, now, identity="eur")))
    state.marks.put(mark(usd, now))
    snap = state.snapshot("USD")
    assert snap.readiness is Readiness.FX_CONVERSION_REQUIRED
    assert snap.unrealized_pnl is None and snap.gross_exposure is None and snap.net_exposure is None
    assert {t.currency: t.unrealized_pnl for t in snap.currency_totals} == {
        "USD": Decimal(20),
        "USDT": Decimal(20),
    }


def add_eth(state, rec, now):
    eth = record(
        replace(
            rec.spec, instrument_id="BINANCE:SPOT:ETHUSDT", symbol="ETHUSDT", base_currency="ETH"
        ),
        now,
    )
    state.registry.register(eth)
    state.replace_positions((position(rec, now), position(eth, now, identity="eth")))
    return eth


def test_mark_timestamp_skew(portfolio, now):
    state, rec = portfolio
    eth = add_eth(state, rec, now)
    state.marks.put(mark(eth, now - timedelta(seconds=3)))
    snap = state.snapshot("USDT")
    assert snap.readiness is Readiness.STALE_MARKS and "mark_timestamp_skew" in snap.reasons


def test_atomic_mark_batch_rollback(portfolio, now):
    state, rec = portfolio
    before = state.snapshot("USDT")
    valid = mark(rec, now, price="120", seq=2)
    with pytest.raises(ValueError):
        state.marks.put_many((valid, replace(valid, instrument_id="unknown")))
    assert state.snapshot("USDT").to_dict() == before.to_dict()


def test_concurrent_atomic_as_of_no_torn_mark_batch(portfolio, now):
    state, rec = portfolio
    eth = add_eth(state, rec, now)
    state.marks.put(mark(eth, now))

    def update():
        for seq in range(2, 102):
            state.marks.put_many(
                (mark(rec, now, price=str(seq), seq=seq), mark(eth, now, price=str(seq), seq=seq))
            )

    def capture():
        for _ in range(200):
            snap = state.snapshot("USDT")
            assert snap.as_of == snap.inputs.quality.as_of == now
            assert len({m.price for m in snap.inputs.marks}) == 1

    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in (pool.submit(update), pool.submit(capture), pool.submit(capture)):
            future.result(timeout=10)


def test_replay_uses_frozen_inputs(portfolio):
    state, rec = portfolio
    original = state.snapshot("USDT")
    payload = json.loads(json.dumps(original.inputs.to_dict()))
    state.registry.register(replace(rec, spec=replace(rec.spec, tick_size=Decimal("0.1"))))
    state.replace_positions(())
    assert replay(payload, expected_lineage=original.lineage_id).to_dict() == original.to_dict()
    assert evaluate(original.inputs) == original


@pytest.mark.parametrize(
    "change", ["float", "unknown", "revision", "policy", "lineage", "duplicate"]
)
def test_invalid_replay_or_tampered_inputs(portfolio, change):
    state, _ = portfolio
    snap = state.snapshot("USDT")
    payload = snap.inputs.to_dict()
    if change == "float":
        payload["positions"][0]["quantity"] = 1.1
    if change == "unknown":
        payload["extra"] = True
    if change == "revision":
        payload["records"][0]["spec"]["contract_size"] = "2"
    if change == "policy":
        payload["policy"]["max_age_us"] = True
    if change == "lineage":
        payload["generation"] += 1
    if change == "duplicate":
        payload["positions"] *= 2
    with pytest.raises(ValueError):
        replay(payload, expected_lineage=snap.lineage_id)


@pytest.mark.parametrize(
    "alias,key",
    [
        ("BTC", "crypto:BTC/USDT"),
        ("ETHUSDT", "crypto:ETH/USDT"),
        ("XAU", "metals:XAU/USD"),
        ("EURUSD", "forex:EUR/USD"),
        ("XAGUSD", "metals:XAG/USD"),
    ],
)
def test_canonical_symbols(alias, key):
    assert SymbolCatalog().resolve(alias).key == key


def test_extensible_mapping_without_venue_assumptions():
    catalog = SymbolCatalog()
    symbol = CanonicalSymbol(AssetClass.CRYPTO, "BTC", "USD")
    catalog.add(symbol, ("BTCUSD",))
    assert catalog.resolve("BTCUSD") != catalog.resolve("BTCUSDT")
    for alias in ("XAUUSD.a", "btcusdt", "BTC/USD"):
        with pytest.raises(ValueError):
            catalog.resolve(alias)
    with pytest.raises(ValueError):
        catalog.add(symbol, ("BTC",))


def test_registry_history_bounds_and_no_economics_changes(portfolio, now):
    _, rec = portfolio
    registry = InstrumentRegistry(capacity=1, history_limit=2)
    registry.register(rec)
    revisions = [rec]
    for i in range(1, 3):
        newer = replace(
            rec, provenance=replace(rec.provenance, observed_at=now + timedelta(seconds=i))
        )
        registry.register(newer)
        revisions.append(newer)
    assert registry.get(rec.spec.instrument_id, rec.revision) is None
    assert registry.get(rec.spec.instrument_id, revisions[1].revision) == revisions[1]
    with pytest.raises(ValueError):
        registry.register(rec)
    with pytest.raises(ValueError):
        registry.register(replace(revisions[-1], quantity_unit=QuantityUnit.CONTRACTS))
    with pytest.raises(ValueError):
        registry.register(record(replace(rec.spec, instrument_id="other"), now))


def test_no_base_multiplier_override_or_incomplete_mapping(portfolio):
    _, rec = portfolio
    with pytest.raises(ValueError):
        replace(rec, spec=replace(rec.spec, contract_size=Decimal(100)))
    with pytest.raises(ValueError):
        replace(rec, quantity_unit="base")
    with pytest.raises(ValueError):
        replace(rec, canonical=CanonicalSymbol(AssetClass.CRYPTO, "ETH", "USDT"))
    assert record_from_dict(rec.to_dict()) == rec


@pytest.mark.parametrize("provider", ["binance", "bybit"])
def test_dynamic_public_specs_with_metadata_provenance(now, provider):
    if provider == "binance":
        metadata = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                    ],
                }
            ]
        }

        class Rest(BinanceREST):
            def get(self, endpoint, params):
                return metadata
    else:
        metadata = {
            "list": [
                {
                    "symbol": "BTCUSDT",
                    "status": "Trading",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "priceFilter": {"tickSize": "0.1"},
                    "lotSizeFilter": {"basePrecision": "0.000001"},
                }
            ]
        }

        class Rest(BybitREST):
            def get(self, endpoint, params):
                return metadata

    registry = InstrumentRegistry()
    rec = refresh_public_spec(registry, Rest(), "BTCUSDT", clock=lambda: now)
    assert rec.provenance.metadata_digest == digest(metadata) and rec.provenance.observed_at == now
    assert (
        rec.canonical.key == "crypto:BTC/USDT"
        and rec.quantity_unit is QuantityUnit.BASE
        and rec.spec.contract_size == 1
    )
    assert rec.spec.tick_size == Decimal("0.01" if provider == "binance" else "0.1")


@pytest.mark.parametrize("problem", ["disconnected", "gap", "stale", "divergent", "malformed"])
def test_phase2_health_blocks_portfolio(now, binance_instrument, market_event, problem):
    rec = record(binance_instrument, now)
    hub = MarketDataHub(clock=lambda: now)
    hub.register(rec.spec, Subscription("BTCUSDT", ("trade",)), record=rec)
    hub.portfolio.replace_positions((position(rec, now),))
    assert hub.ingest(market_event).accepted
    assert hub.portfolio.snapshot_from_hub(hub, "USDT").readiness is Readiness.READY
    gate = hub.gate.streams[stream_key(market_event)]
    as_of = now
    if problem == "disconnected":
        gate.disconnected = True
    if problem == "gap":
        gate.gap = True
    if problem == "stale":
        as_of = now + timedelta(seconds=20)
    if problem == "malformed":
        hub.malformed()
    if problem == "divergent":

        class Selector:
            divergent = True

        hub.failover = Selector()
    snap = hub.portfolio.snapshot_from_hub(hub, "USDT", as_of=as_of)
    assert snap.readiness is (
        Readiness.DATA_DIVERGENT if problem == "divergent" else Readiness.STALE_MARKS
    )
    assert snap.unrealized_pnl is None


def test_receipt_policy_does_not_hide_degraded_health(portfolio, now):
    state, rec = portfolio
    state.marks.put(
        replace(
            mark(rec, now, seq=2),
            timestamp_basis=TimestampBasis.RECEIPT,
            health="degraded",
            event_type=EventType.QUOTE,
        )
    )
    assert state.snapshot("USDT").readiness is Readiness.STALE_MARKS
    state.policy = PortfolioPolicy(allow_receipt_marks=True)
    assert state.snapshot("USDT").readiness is Readiness.READY
    state.marks._health[rec.spec.instrument_id] = "degraded"
    assert state.snapshot("USDT").readiness is Readiness.STALE_MARKS


def test_rejected_backfill_and_duplicate_marks(portfolio, market_event):
    state, rec = portfolio
    before = state.snapshot("USDT")
    decision = HealthDecision(False, HealthStatus.DEGRADED, ("duplicate",))
    assert (
        not state.marks.admit(market_event, decision)
        and state.snapshot("USDT").to_dict() == before.to_dict()
    )
    assert not state.marks.admit(replace(market_event, instrument_id="unknown"), decision)
    assert "unknown" not in state.marks._health
    assert not state.marks.admit(
        replace(market_event, delivery_kind="backfill"), HealthDecision(True, HealthStatus.HEALTHY)
    )
    assert state.snapshot("USDT").readiness is Readiness.STALE_MARKS


def test_shared_as_of_and_all_readiness_issues(portfolio, now):
    state, rec = portfolio
    with pytest.raises(ValueError):
        state.snapshot("USDT", quality=DataQuality(now - timedelta(seconds=1), ()))
    state.marks._marks.clear()
    snap = state.snapshot("USD", quality=DataQuality(now, (), True))
    assert snap.readiness is Readiness.DATA_DIVERGENT
    assert set(snap.issues) == {
        Readiness.DATA_DIVERGENT,
        Readiness.MISSING_MARKS,
        Readiness.FX_CONVERSION_REQUIRED,
    }
    assert inputs_from_dict(snap.inputs.to_dict()) == snap.inputs


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", 0),
        ("source_epoch", 0),
        ("source_ts", None),
        ("spec_revision", "unknown"),
        ("source", "wrong"),
    ],
)
def test_mark_regression_and_provenance_rejected(portfolio, now, field, value):
    state, rec = portfolio
    if field == "source_ts":
        value = now - timedelta(seconds=1)
    with pytest.raises(ValueError):
        state.marks.put(replace(mark(rec, now, seq=2), **{field: value}))


def test_failed_metadata_refresh_invalidates_current_but_preserves_replay(portfolio):
    state, rec = portfolio
    original = state.snapshot("USDT")

    class Failed(BinanceREST):
        def get(self, endpoint, params):
            raise ValueError("Synthetic unsupported metadata")

    with pytest.raises(ValueError):
        refresh_public_spec(state.registry, Failed(), "BTCUSDT")
    assert state.snapshot("USDT").readiness is Readiness.INSTRUMENT_SPEC_INCOMPLETE
    assert state.registry.get(rec.spec.instrument_id, rec.revision) == rec
    assert replay(original.inputs.to_dict()).to_dict() == original.to_dict()


def test_registry_invalidation_cannot_bypass_capacity(portfolio, now):
    _, rec = portfolio
    registry = InstrumentRegistry(capacity=1)
    registry.register(rec)
    registry.invalidate(rec.spec.instrument_id)
    with pytest.raises(ValueError):
        registry.register(record(replace(rec.spec, instrument_id="other"), now))


def test_cached_snapshot_spec_change_requires_explicit_rebinding(portfolio, now):
    state, rec = portfolio
    state.registry.register(replace(rec, spec=replace(rec.spec, tick_size=Decimal("0.2"))))
    assert state.snapshot("USDT").readiness is Readiness.INSTRUMENT_SPEC_INCOMPLETE


def test_hub_receipt_policy_does_not_hide_malformed_other_channel(
    now, binance_instrument, normalizer, raw_quote
):
    rec = record(binance_instrument, now)
    hub = MarketDataHub(clock=lambda: now)
    hub.register(rec.spec, Subscription("BTCUSDT", ("trade", "quote")), record=rec)
    hub.portfolio.policy = PortfolioPolicy(allow_receipt_marks=True)
    hub.portfolio.replace_positions((position(rec, now),))
    quote = normalizer.message(raw_quote, now)
    hub.ingest(quote)
    assert hub.portfolio.snapshot_from_hub(hub, "USDT").readiness is Readiness.READY
    hub.malformed((f"binance.spot:{rec.spec.instrument_id}:trade",))
    assert hub.portfolio.snapshot_from_hub(hub, "USDT").readiness is Readiness.STALE_MARKS


def test_bar_events_never_become_current_marks(now, binance_instrument, normalizer, raw_bar):
    rec = record(binance_instrument, now)
    hub = MarketDataHub(clock=lambda: now)
    hub.register(rec.spec, Subscription("BTCUSDT", ("bar",)), record=rec)
    hub.portfolio.replace_positions((position(rec, now),))
    hub.ingest(normalizer.message(raw_bar, now))
    assert hub.portfolio.snapshot_from_hub(hub, "USDT").readiness is Readiness.MISSING_MARKS


def test_snapshot_constructor_cannot_accept_mutable_or_blocked_totals(portfolio):
    state, _ = portfolio
    snap = state.snapshot("USDT")
    with pytest.raises(ValueError):
        replace(snap, valuations=list(snap.valuations))
    with pytest.raises(ValueError):
        replace(snap, readiness=Readiness.STALE_MARKS, issues=(Readiness.STALE_MARKS,))
    with pytest.raises(ValueError):
        replace(snap, lineage_id="invalid")
