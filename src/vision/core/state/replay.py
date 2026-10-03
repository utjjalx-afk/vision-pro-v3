"""Strict portable portfolio inputs: replay never reads providers or current state."""

from dataclasses import fields
from datetime import timedelta

from vision.core.codec import decimal_string, instrument_from_dict, utc_string
from vision.core.contracts import AssetClass, EventType, TimestampBasis
from vision.core.instruments import CanonicalSymbol, InstrumentRecord, QuantityUnit, SpecProvenance
from vision.core.state.portfolio import (
    DataQuality,
    Mark,
    PortfolioInputs,
    PortfolioPolicy,
    Position,
    Side,
    evaluate,
)


def shape(value, cls):
    if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
        raise ValueError("Missing or unknown replay fields")
    return dict(value)


def record_from_dict(value):
    if not isinstance(value, dict) or set(value) != {
        "spec",
        "canonical",
        "quantity_unit",
        "provenance",
        "valuation_model",
        "revision",
    }:
        raise ValueError("Complete record required")
    asset, pair = value["canonical"].split(":")
    base, quote = pair.split("/")
    provenance = shape(value["provenance"], SpecProvenance)
    provenance["observed_at"] = utc_string(provenance["observed_at"])
    record = InstrumentRecord(
        instrument_from_dict(value["spec"]),
        CanonicalSymbol(AssetClass(asset), base, quote),
        QuantityUnit(value["quantity_unit"]),
        SpecProvenance(**provenance),
        value["valuation_model"],
    )
    if record.revision != value["revision"]:
        raise ValueError("Instrument revision digest mismatch")
    return record


def inputs_from_dict(value):
    data = shape(value, PortfolioInputs)
    for key in ("positions", "marks", "records"):
        if not isinstance(data[key], list) or len(data[key]) > 256:
            raise ValueError("Bounded replay arrays required")
    positions = []
    for row in data["positions"]:
        position = shape(row, Position)
        position["side"] = Side(position["side"])
        position["quantity_unit"] = QuantityUnit(position["quantity_unit"])
        position["opened_at"] = utc_string(position["opened_at"])
        for key in ("quantity", "entry_price"):
            position[key] = decimal_string(position[key])
        positions.append(Position(**position))
    marks = []
    for row in data["marks"]:
        mark = shape(row, Mark)
        mark["price"] = decimal_string(mark["price"])
        mark["timestamp_basis"] = TimestampBasis(mark["timestamp_basis"])
        mark["event_type"] = EventType(mark["event_type"])
        for key in ("source_ts", "received_ts"):
            mark[key] = utc_string(mark[key])
        marks.append(Mark(**mark))
    policy = data["policy"]
    if not isinstance(policy, dict) or set(policy) != {
        "max_age_us",
        "max_skew_us",
        "max_spec_age_us",
        "allow_receipt_marks",
    }:
        raise ValueError("Explicit replay policy required")
    durations = []
    for key in ("max_age_us", "max_skew_us", "max_spec_age_us"):
        if type(policy[key]) is not int or not 0 < policy[key] <= 365 * 86400 * 1000000:
            raise ValueError("Bounded integer policy duration required")
        durations.append(timedelta(microseconds=policy[key]))
    quality = shape(data["quality"], DataQuality)
    quality["as_of"] = utc_string(quality["as_of"])
    if not isinstance(quality["states"], list) or len(quality["states"]) > 256:
        raise ValueError("Bounded health states required")
    quality["states"] = tuple(tuple(pair) for pair in quality["states"])
    return PortfolioInputs(
        utc_string(data["as_of"]),
        data["reporting_currency"],
        tuple(positions),
        tuple(marks),
        tuple(record_from_dict(row) for row in data["records"]),
        DataQuality(**quality),
        PortfolioPolicy(*durations, policy["allow_receipt_marks"]),
        data["generation"],
    )


def replay(value, *, expected_lineage=None):
    snapshot = evaluate(inputs_from_dict(value))
    if expected_lineage is not None and snapshot.lineage_id != expected_lineage:
        raise ValueError("Portfolio input lineage digest mismatch")
    return snapshot
