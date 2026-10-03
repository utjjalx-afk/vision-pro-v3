"""Explicit symbol economics and immutable, versioned instrument metadata."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from threading import RLock

from vision.core.contracts import AssetClass, InstrumentSpec, _identifier, _utc


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class QuantityUnit(StrEnum):
    BASE = "base"
    CONTRACTS = "contracts"


@dataclass(frozen=True, slots=True)
class CanonicalSymbol:
    asset_class: AssetClass
    base: str
    quote: str

    def __post_init__(self):
        if not isinstance(self.asset_class, AssetClass):
            raise ValueError("Explicit asset class required")
        for currency in (self.base, self.quote):
            if (
                not isinstance(currency, str)
                or not currency.isalnum()
                or currency != currency.upper()
            ):
                raise ValueError("Currencies must be uppercase codes")
        if self.base == self.quote:
            raise ValueError("Base and quote must differ")

    @property
    def key(self):
        return f"{self.asset_class.value}:{self.base}/{self.quote}"


class SymbolCatalog:
    """Explicit aliases only: no suffix stripping, USD/USDT peg or broker inference."""

    def __init__(self):
        self._aliases = {}
        for asset, base, quote, aliases in (
            (AssetClass.CRYPTO, "BTC", "USDT", ("BTC", "BTCUSDT")),
            (AssetClass.CRYPTO, "ETH", "USDT", ("ETH", "ETHUSDT")),
            (AssetClass.METALS, "XAU", "USD", ("XAU", "XAUUSD")),
            (AssetClass.FOREX, "EUR", "USD", ("EURUSD",)),
            (AssetClass.METALS, "XAG", "USD", ("XAG", "XAGUSD")),
        ):
            self.add(CanonicalSymbol(asset, base, quote), aliases)

    def add(self, symbol, aliases=()):
        if not isinstance(symbol, CanonicalSymbol):
            raise ValueError("Canonical symbol required")
        names = (symbol.key, *aliases)
        for name in names:
            _identifier(name)
            if name in self._aliases and self._aliases[name] != symbol:
                raise ValueError("Alias already maps to different economics")
        self._aliases.update(dict.fromkeys(names, symbol))

    def resolve(self, alias):
        try:
            return self._aliases[alias]
        except (KeyError, TypeError):
            raise ValueError("Unknown explicit symbol mapping") from None


@dataclass(frozen=True, slots=True)
class SpecProvenance:
    source: str
    reference: str
    observed_at: datetime
    metadata_digest: str
    normalization: str

    def __post_init__(self):
        for value in (self.source, self.reference, self.normalization):
            _identifier(value)
        _utc(self.observed_at)
        if (
            not isinstance(self.metadata_digest, str)
            or len(self.metadata_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.metadata_digest)
        ):
            raise ValueError("SHA-256 metadata digest required")

    def to_dict(self):
        return {
            "source": self.source,
            "reference": self.reference,
            "observed_at": _utc(self.observed_at),
            "metadata_digest": self.metadata_digest,
            "normalization": self.normalization,
        }


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    spec: InstrumentSpec
    canonical: CanonicalSymbol
    quantity_unit: QuantityUnit
    provenance: SpecProvenance
    valuation_model: str

    def __post_init__(self):
        if (
            not isinstance(self.spec, InstrumentSpec)
            or not isinstance(self.canonical, CanonicalSymbol)
            or not isinstance(self.provenance, SpecProvenance)
        ):
            raise ValueError("Complete typed spec, mapping and provenance required")
        if not isinstance(self.quantity_unit, QuantityUnit):
            raise ValueError("Explicit quantity unit required")
        if self.valuation_model not in {"linear_quote", "unsupported"}:
            raise ValueError("Explicit valuation model required")
        if (self.spec.asset_class, self.spec.base_currency, self.spec.quote_currency) != (
            self.canonical.asset_class,
            self.canonical.base,
            self.canonical.quote,
        ):
            raise ValueError("Canonical mapping and venue economics disagree")
        if self.quantity_unit is QuantityUnit.BASE and self.spec.contract_size != Decimal("1"):
            raise ValueError("Base quantities must declare one base unit per quantity")
        if self.spec.venue in {"BINANCE_SPOT", "BYBIT_SPOT"}:
            expected = f"{self.spec.venue.removesuffix('_SPOT').lower()}.spot"
            if self.provenance.source != expected or self.quantity_unit is not QuantityUnit.BASE:
                raise ValueError(
                    "Public Spot specs must use provider provenance and base quantities"
                )

    def content(self):
        return {
            "spec": self.spec.to_dict(),
            "canonical": self.canonical.key,
            "quantity_unit": self.quantity_unit.value,
            "provenance": self.provenance.to_dict(),
            "valuation_model": self.valuation_model,
        }

    @property
    def revision(self):
        return digest(self.content())

    def to_dict(self):
        return {**self.content(), "revision": self.revision}


class InstrumentRegistry:
    def __init__(self, *, capacity=256, history_limit=16):
        if any(type(n) is not int or n < 1 for n in (capacity, history_limit)):
            raise ValueError("Positive registry bounds required")
        self.capacity, self.history_limit = capacity, history_limit
        self.lock = RLock()
        self._current = {}
        self._history = {}

    def register(self, record: InstrumentRecord):
        if not isinstance(record, InstrumentRecord):
            raise ValueError("Complete instrument record required")
        with self.lock:
            key = record.spec.instrument_id
            previous = self._current.get(key)
            if previous is None and key in self._history:
                previous = next(reversed(self._history[key].values()))
            if key not in self._history and len(self._history) >= self.capacity:
                raise ValueError("Instrument registry capacity exceeded")
            if previous is not None:
                if record.provenance.observed_at < previous.provenance.observed_at:
                    raise ValueError("Metadata observation cannot regress")
                if (
                    previous.canonical != record.canonical
                    or previous.quantity_unit != record.quantity_unit
                ):
                    raise ValueError("Instrument identity economics cannot change")
            self._current[key] = record
            history = self._history.setdefault(key, {})
            history[record.revision] = record
            while len(history) > self.history_limit:
                del history[next(iter(history))]
            return record

    def invalidate(self, instrument_id):
        with self.lock:
            self._current.pop(instrument_id, None)

    def get(self, instrument_id, revision=None):
        with self.lock:
            if revision is None:
                return self._current.get(instrument_id)
            return self._history.get(instrument_id, {}).get(revision)

    def records(self):
        with self.lock:
            return tuple(self._current[key] for key in sorted(self._current))
