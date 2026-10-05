"""Windows-only native reader; cross-platform core never imports MT5 eagerly."""

import hashlib
import hmac
import math
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock

from vision.analysis.contracts import wire
from vision.broker.models import (
    BrokerAccount,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
    BrokerSpec,
)
from vision.core.instruments import digest

VERSION = "phase11-v1"
CANONICAL = {
    "metals:XAU/USD": ("XAU", "USD"),
    "metals:XAG/USD": ("XAG", "USD"),
    "forex:EUR/USD": ("EUR", "USD"),
    "crypto:BTC/USD": ("BTC", "USD"),
}


class BridgeUnavailable(RuntimeError):
    """Safe category only; native diagnostics/account data are never logged."""


def native_decimal(value):
    if type(value) not in {float, int} or not math.isfinite(value):
        raise BridgeUnavailable("BROKER_NUMBER_UNAVAILABLE")
    return Decimal(str(value))


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.#+-]{1,64}", value):
        raise ValueError("Explicit bounded broker symbol required")
    return value


class MT5Reader:
    def __init__(self, native, mapping, identity_key, *, clock=lambda: datetime.now(UTC)):
        if (
            not isinstance(mapping, dict)
            or not 1 <= len(mapping) <= 4
            or set(mapping) - set(CANONICAL)
        ):
            raise ValueError("Explicit canonical-to-broker mapping required")
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("Ambiguous broker mapping")
        for name in mapping.values():
            symbol(name)
        if not isinstance(identity_key, bytes) or len(identity_key) < 32:
            raise ValueError("Runtime identity key required")
        self._native, self.mapping, self._key = native, dict(mapping), identity_key
        self.clock, self.lock = clock, RLock()
        self._expected_identity = None

    @classmethod
    def connect(cls, mapping, identity_key, *, terminal_path):
        from vision.config import load_settings

        load_settings(os.environ)
        if sys.platform != "win32":
            raise BridgeUnavailable("WINDOWS_MT5_REQUIRED")
        import MetaTrader5 as native

        if not native.initialize(path=terminal_path, timeout=10000):
            raise BridgeUnavailable("TERMINAL_UNAVAILABLE")
        reader = cls(native, mapping, identity_key)
        reader.account()  # Demo-only gate; never login/switch accounts programmatically.
        for name in reader.mapping.values():
            info = native.symbol_info(name)
            if info is None or (not info.visible and not native.symbol_select(name, True)):
                raise BridgeUnavailable("SYMBOL_NOT_VISIBLE_OR_UNAVAILABLE")
        return reader

    def _id(self, value):
        return hmac.new(self._key, str(value).encode(), hashlib.sha256).hexdigest()

    def account(self):
        info, terminal = self._native.account_info(), self._native.terminal_info()
        if info is None or terminal is None or info.trade_mode != 0 or not terminal.connected:
            raise BridgeUnavailable("CONNECTED_DEMO_ACCOUNT_REQUIRED")
        identity = self._id((info.login, info.server))
        if self._expected_identity is not None and identity != self._expected_identity:
            raise BridgeUnavailable("ACCOUNT_IDENTITY_CHANGED")
        self._expected_identity = identity
        return BrokerAccount(
            identity,
            info.currency,
            native_decimal(info.equity),
            native_decimal(info.balance),
            native_decimal(info.margin_free),
            True,
            True,
            self.clock(),
        )

    def discovery(self):
        with self.lock:
            self.account()
            rows = self._native.symbols_get()
            if rows is None or len(rows) > 20000:
                raise BridgeUnavailable("SYMBOL_DISCOVERY_UNAVAILABLE")
            # Suggestions are not selections; explicit operator mapping remains required.
            return {
                canonical: [
                    row.name
                    for row in rows
                    if row.currency_base == base and row.currency_profit == quote
                ][:100]
                for canonical, (base, quote) in CANONICAL.items()
            }

    def snapshot(self):
        with self.lock:
            account = self.account()
            rows = self._native.positions_get()
            orders = self._native.orders_get()
            if rows is None or orders is None or len(rows) > 256 or len(orders) > 256:
                raise BridgeUnavailable("ACCOUNT_RISK_UNAVAILABLE")
            positions = tuple(
                BrokerPosition(
                    self._id(row.ticket),
                    row.symbol,
                    {0: "LONG", 1: "SHORT"}[row.type],
                    native_decimal(row.volume),
                    native_decimal(row.price_open),
                    native_decimal(row.sl),
                )
                for row in rows
            )
            reverse = {v: k for k, v in self.mapping.items()}
            names = sorted(set(reverse) | {p.symbol for p in positions})
            specs, quotes = [], []
            for name in names:
                symbol(name)
                info = self._native.symbol_info(name)
                if info is None or not info.visible:
                    raise BridgeUnavailable("SYMBOL_NOT_VISIBLE_OR_UNAVAILABLE")
                canonical = reverse.get(name, "broker:UNMAPPED")
                if (
                    canonical in CANONICAL
                    and (info.currency_base, info.currency_profit) != CANONICAL[canonical]
                ):
                    raise BridgeUnavailable("BROKER_MAPPING_ECONOMICS_MISMATCH")
                at = self.clock()
                specs.append(
                    BrokerSpec(
                        name,
                        canonical,
                        *(
                            native_decimal(getattr(info, field))
                            for field in (
                                "volume_min",
                                "volume_max",
                                "volume_step",
                                "volume_limit",
                                "point",
                                "trade_tick_size",
                                "trade_tick_value_profit",
                                "trade_tick_value_loss",
                                "trade_contract_size",
                            )
                        ),
                        info.trade_stops_level,
                        info.trade_freeze_level,
                        info.trade_mode,
                        info.currency_profit,
                        info.currency_margin,
                        at,
                        account.identity,
                    )
                )
                tick = self._native.symbol_info_tick(name)
                if tick is None or type(tick.time_msc) is not int or tick.time_msc <= 0:
                    raise BridgeUnavailable("BROKER_QUOTE_UNAVAILABLE")
                quotes.append(
                    BrokerQuote(
                        name,
                        native_decimal(tick.bid),
                        native_decimal(tick.ask),
                        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=tick.time_msc),
                        self.clock(),
                    )
                )
            after = self.account()
            again = self._native.positions_get()
            pending = self._native.orders_get()
            if (
                again is None
                or pending is None
                or tuple(rows) != tuple(again)
                or tuple(orders) != tuple(pending)
            ):
                raise BridgeUnavailable("BROKER_STATE_CHANGED_DURING_CAPTURE")
            if wire(account) | {"observed_at": "ignored"} != wire(after) | {
                "observed_at": "ignored"
            }:
                raise BridgeUnavailable("BROKER_ACCOUNT_CHANGED_DURING_CAPTURE")
            return BrokerSnapshot(
                after, tuple(specs), tuple(quotes), positions, len(orders), self.clock(), VERSION
            )

    def profit(self, side, name, volume, entry, stop):
        with self.lock:
            self.account()
            result = self._native.order_calc_profit(
                {"LONG": 0, "SHORT": 1}[side],
                symbol(name),
                float(volume),
                float(entry),
                float(stop),
            )
            return None if result is None else native_decimal(result)

    def margin(self, side, name, volume, entry):
        with self.lock:
            self.account()
            result = self._native.order_calc_margin(
                {"LONG": 0, "SHORT": 1}[side], symbol(name), float(volume), float(entry)
            )
            return None if result is None else native_decimal(result)

    def health(self):
        with self.lock:
            account = self.account()
            version = self._native.version()
            if not isinstance(version, tuple) or len(version) != 3:
                raise BridgeUnavailable("TERMINAL_VERSION_UNAVAILABLE")
            return {
                "version": VERSION,
                "terminal_version": list(version),
                "account_identity": account.identity,
                "currency": account.currency,
                "demo": True,
                "connected": True,
                "order_dispatch_available": False,
            }

    def size(self, request, policy):
        from vision.broker.codec import receipt_from_dict

        return receipt_from_dict(self.audit_size(request, policy)["result"])

    def audit_size(self, request, policy):
        from dataclasses import replace

        from vision.broker.sizer import size

        with self.lock:
            snapshot = self.snapshot()
            now = self.clock()
            result = size(snapshot, request, policy, self, now=now)
            after = self.snapshot()

            def stable(value):
                data = wire(value)
                data.pop("as_of")
                data["account"].pop("observed_at")
                for row in (*data["specs"], *data["quotes"]):
                    row.pop("observed_at")
                return digest(data)

            if stable(snapshot) != stable(after):
                result = replace(
                    result, status="BLOCKED", reasons=("BROKER_STATE_CHANGED_DURING_SIZING",)
                )
            return {
                "snapshot": wire(snapshot),
                "after_snapshot": wire(after),
                "request": wire(request),
                "policy": wire(policy),
                "now": wire(now),
                "calculations": wire(result.calculations),
                "result": result.to_dict(),
            }
