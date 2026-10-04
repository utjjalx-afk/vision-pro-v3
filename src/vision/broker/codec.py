"""Strict bounded JSON DTO parsing; no float money or arbitrary native invocation."""

import re
from dataclasses import fields
from datetime import timedelta
from decimal import Decimal

from vision.broker.models import (
    BrokerAccount,
    BrokerPosition,
    BrokerQuote,
    BrokerSnapshot,
    BrokerSpec,
    Calculation,
    SizeRequest,
    SizingPolicy,
    SizingReceipt,
)
from vision.core.codec import utc_string


def decimal(value):
    if (
        not isinstance(value, str)
        or len(value) > 100
        or not re.fullmatch(r"-?\d+(?:\.\d+)?", value)
    ):
        raise ValueError("Exact decimal string required")
    return Decimal(value)


def dto(cls, value):
    if type(value) is not dict or set(value) != {f.name for f in fields(cls)}:
        raise ValueError("Exact broker DTO fields required")
    data = dict(value)
    for f in fields(cls):
        if f.type == Decimal or f.type == Decimal | None:
            data[f.name] = None if data[f.name] is None else decimal(data[f.name])
        elif f.type is not None and f.type.__class__ is type and f.type.__name__ == "datetime":
            data[f.name] = utc_string(data[f.name])
        elif f.type == timedelta:
            if type(data[f.name]) is not int:
                raise ValueError("Integer microsecond duration required")
            data[f.name] = timedelta(microseconds=data[f.name])
    return cls(**data)


def receipt_from_dict(value):
    data = dict(value)
    identity = data.pop("receipt_id")
    data["reasons"] = tuple(data["reasons"])
    data["calculations"] = tuple(dto(Calculation, row) for row in data["calculations"])
    result = dto(SizingReceipt, data)
    if result.receipt_id != identity:
        raise ValueError("Sizing receipt integrity mismatch")
    return result


def snapshot_from_dict(value):
    data = dict(value)
    data["account"] = dto(BrokerAccount, data["account"])
    for field, cls in (
        ("specs", BrokerSpec),
        ("quotes", BrokerQuote),
        ("positions", BrokerPosition),
    ):
        if type(data[field]) is not list or len(data[field]) > 256:
            raise ValueError("Bounded broker rows required")
        data[field] = tuple(dto(cls, row) for row in data[field])
    data["as_of"] = utc_string(data["as_of"])
    return BrokerSnapshot(**data)


def replay(value):
    from vision.broker.sizer import size

    if type(value) is not dict or set(value) - {"after_snapshot"} != {
        "snapshot",
        "request",
        "policy",
        "now",
        "calculations",
        "result",
    }:
        raise ValueError("Exact sizing replay envelope required")
    if type(value["calculations"]) is not list or len(value["calculations"]) > 259:
        raise ValueError("Calculation replay bound exceeded")

    class Recorded:
        def __init__(self):
            self.rows = iter(dto(Calculation, row) for row in value["calculations"])

        def call(self, kind, side, symbol, volume, entry, stop):
            row = next(self.rows)
            if (row.kind, row.side, row.symbol, row.volume, row.entry, row.stop) != (
                kind,
                side,
                symbol,
                volume,
                entry,
                stop,
            ):
                raise ValueError("Native calculation replay mismatch")
            return row.result

        def profit(self, side, symbol, volume, entry, stop):
            return self.call("profit", side, symbol, volume, entry, stop)

        def margin(self, side, symbol, volume, entry):
            return self.call("margin", side, symbol, volume, entry, None)

    recorded = Recorded()
    if any(
        row["account_currency"] != value["snapshot"]["account"]["currency"]
        for row in value["calculations"]
    ):
        raise ValueError("Mixed calculation account currencies")
    result = size(
        snapshot_from_dict(value["snapshot"]),
        dto(SizeRequest, value["request"]),
        dto(SizingPolicy, value["policy"]),
        recorded,
        now=utc_string(value["now"]),
    )
    if "after_snapshot" in value:
        from dataclasses import replace

        from vision.analysis.contracts import wire
        from vision.core.instruments import digest

        def stable(snapshot):
            data = wire(snapshot)
            data.pop("as_of")
            data["account"].pop("observed_at")
            for row in (*data["specs"], *data["quotes"]):
                row.pop("observed_at")
            return digest(data)

        if stable(snapshot_from_dict(value["snapshot"])) != stable(
            snapshot_from_dict(value["after_snapshot"])
        ):
            result = replace(
                result, status="BLOCKED", reasons=("BROKER_STATE_CHANGED_DURING_SIZING",)
            )
    if next(recorded.rows, None) is not None or result.to_dict() != value["result"]:
        raise ValueError("Sizing result replay mismatch")
    return result
