# Phase 3: instrument registry and portfolio state

This is a read-only foundation. Position inputs are explicitly supplied immutable
records; no fills, orders, account queries, strategies, agents, MT5, cash ledger or
live execution are implemented. Startup remains offline. Phase-2 public data is
the only network capability.

## Explicit instrument economics

`InstrumentRegistry` stores immutable `InstrumentRecord` objects. Each contains
the venue-specific InstrumentSpec, canonical base/quote/asset mapping, quantity
unit, declared valuation model and provenance. Provenance includes the public
metadata endpoint, source, observation time, normalization version and SHA-256
of the decoded metadata. Bybit's digest covers its decoded `result`, excluding
the transport envelope. The record revision hashes all these inputs, including
the observation time. Failed refresh invalidates the current record; bounded
historical revisions remain available for replay.

`refresh_public_spec` loads Binance/Bybit BTCUSDT (or another supported Spot
symbol) dynamically. It reads tick and lot precision from provider metadata.
Spot quantity is explicitly declared in base units, so contract_size=1 means one
base unit per quantity, not a global multiplier. Public Spot records reject any
other unit or source. Bybit minimum_quantity remains 0 because its deprecated
quantity minimum is not a current sizing constraint. These records do not support
execution sizing, margin rules or order validation.

The registry has no fallback multiplier. Contract quantities require an explicit
per-instrument contract_size and `linear_quote` valuation model. Inverse/nonlinear
contracts are unsupported and block readiness. Spec revision changes require
explicit position and mark rebinding; there is no silent reinterpretation of
existing quantities, even if a refresh changes only its observation time.

Sources: [Binance exchange information](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/general),
[Bybit instrument metadata](https://bybit-exchange.github.io/docs/v5/market/instrument).

## Symbol mapping

`CanonicalSymbol` identifies asset class, base currency and quote currency; it
does not erase the venue ID. `SymbolCatalog` provides explicit BTC/BTCUSDT and
ETH/ETHUSDT aliases for crypto pairs quoted in USDT, XAU/XAUUSD and XAG/XAGUSD for
metals quoted in USD, and EURUSD for EUR/USD. Adding BTCUSD produces a different
canonical symbol from BTCUSDT. USD and USDT are never equated.

The catalog is extensible through explicit additions. Broker suffixes, lowercase
symbols and unknown aliases are rejected rather than guessed. XAU/EURUSD/XAG
mapping does not create tradable specs or assume broker contract sizes; a complete
provenance-bearing record must be supplied before valuation.

## Immutable state and synchronized capture

`Position` specifies ID, actual venue instrument ID, pinned spec revision, side,
positive Decimal quantity, quantity unit, entry price, opened_at and input lineage.
`Mark` records price and spec revision plus source/event/provider sequence,
connection epoch, event type, timestamps, timestamp basis, continuity and health.
Trades supply their price; quotes supply an exact midpoint. Historical bars and
backfill records never become current marks. Duplicate admission cannot poison
a valid mark; unknown instruments cannot grow the cache.

Registry updates, position replacement, mark batches and snapshot capture share
one reentrant lock. A mark batch either commits completely or restores the prior
cache. `PortfolioState.snapshot` captures one UTC `as_of` under that lock and
freezes positions, marks, specs, policy and quality into `PortfolioInputs`.
`snapshot_from_hub` additionally captures Phase-2 health/divergence synchronously
on the hub's owning event loop. Hub ingestion remains single-owner; do not mutate
the hub from another thread while capturing its quality. Core registry/portfolio
state supports concurrent writers and readers through its shared lock.

Hub public CLI paths register dynamic provenance-bearing specs and feed admitted
trade/quote events into the portfolio mark cache. Legacy manual `hub.register`
without a complete record cannot make portfolio specs ready. Positions retain
actual venue IDs: Bybit failover never substitutes Bybit prices into a Binance
position automatically.

## PnL, exposure and currencies

For the explicit linear quote model:

```text
base_units = position.quantity * pinned_spec.contract_size
unrealized_pnl = (mark - entry_price) * base_units * side_sign
gross_exposure = mark * base_units
net_exposure = gross_exposure * side_sign
```

Long side_sign is +1 and short is -1. Gross exposure sums both sides rather than
netting them. Values are denominated in the instrument's quote currency. Arithmetic
uses an isolated Decimal context with exactness trapping and bounded inputs;
ambient Decimal precision cannot silently round results. Wire values are decimal
strings. No float conversion, fee calculation, realized PnL, funding, cash equity,
lot rounding or automatic currency conversion is included.

When valid positions have another quote currency than the explicit reporting
currency, each currency can have separate exact valuations/totals, but reporting
totals remain null with FX_CONVERSION_REQUIRED. No USD/USDT peg or implicit FX
rate is assumed.

## Readiness and fail-closed behavior

All issues and reasons are recorded. Primary readiness follows this precedence:

| State | Trigger | Reporting totals |
| --- | --- | --- |
| DATA_DIVERGENT | Phase-2 divergence latch | null |
| INSTRUMENT_SPEC_INCOMPLETE | Missing/expired/future spec, revision/unit/source mismatch, unsupported model or numeric range | null |
| MISSING_MARKS | Required position has no current mark | null |
| STALE_MARKS | Old/future mark, skew, missing health, disconnected/gapped/degraded feed | null |
| FX_CONVERSION_REQUIRED | Quote/reporting currencies differ | null; separate currency totals only |
| READY | Every required check passes | exact Decimal totals |

Default mark source/receipt age limit is five seconds; maximum cross-mark source
and receipt skew is two seconds; metadata age limit is 24 hours. Defaults are
explicit immutable policy fields and all cutoffs are inclusive. Future timestamps
block readiness. Receipt-time Binance quote marks require explicit
`PortfolioPolicy(allow_receipt_marks=True)`; this permission cannot conceal an
unresolved malformed/degraded stream. Trade marks and quote midpoint marks are
observations for research, not executable bid/ask liquidation prices.

An empty portfolio has exact zero totals in the explicitly requested currency.
READY is data readiness only and does not grant any trading capability.

## Replay and verification

`snapshot.inputs.to_dict()` is a portable replay bundle containing every valuation
input. `replay(bundle, expected_lineage=snapshot.lineage_id)` checks typed shapes,
metadata revision hashes and the SHA-256 input lineage, then recomputes through
the same pure evaluator. It never consults the current registry, a provider or
the wall clock. The digest detects changed inputs relative to the expected digest;
it is not an authentication signature. No persistence backend is included.

```bash
python -m vision instrument-specs --provider both --symbol BTCUSDT
python -m vision instrument-specs --provider both --symbol ETHUSDT
python -m pytest tests/test_portfolio.py
```

```python
# For an already registered public record and a caller-supplied synthetic position:
hub.portfolio.replace_positions((position,))
# hub.ingest(event) performs Phase-2 admission and updates eligible marks.
snapshot = hub.portfolio.snapshot_from_hub(hub, "USDT")
bundle = snapshot.inputs.to_dict()
from vision.core.state.replay import replay

assert replay(bundle, expected_lineage=snapshot.lineage_id) == snapshot
```

Windows/Linux Python 3.11/3.12 and Docker CI run the full deterministic suite,
including concurrency, long/short contract arithmetic, mixed currencies, stale
and skewed marks, metadata invalidation, revision rebinding, replay and Phase-2
quality blocking. Live public metadata smoke checks run separately from CI.
