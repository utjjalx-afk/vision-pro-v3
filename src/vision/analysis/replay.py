"""Strict portable lane input replay. No provider payload, network or current clock."""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from vision.analysis.contracts import LaneContext, LanePolicy, Observation, number
from vision.analysis.runner import assess_all
from vision.core.codec import event_from_dict, utc_string
from vision.core.state.portfolio import DataQuality
from vision.core.state.replay import shape


def context_from_dict(value):
    try:
        return _context_from_dict(value)
    except (TypeError, KeyError, IndexError, OverflowError, InvalidOperation) as error:
        raise ValueError("Invalid lane replay structure") from error


def _context_from_dict(value):
    data = shape(value, LaneContext)
    policy = shape(data["policy"], LanePolicy)
    for key in ("max_age", "max_quality_age", "observation_age"):
        if type(policy[key]) is not int or not 0 < policy[key] <= 7 * 86400 * 1000000:
            raise ValueError("Bounded integer duration required")
        policy[key] = timedelta(microseconds=policy[key])
    policy = LanePolicy(**policy)
    for key, limit in (("events", policy.max_events), ("observations", 256)):
        if not isinstance(data[key], list) or len(data[key]) > limit:
            raise ValueError("Bounded replay array required")
    observations = []
    for row in data["observations"]:
        row = shape(row, Observation)
        value = row["value"]
        if (
            not isinstance(value, str)
            or len(value) > 80
            or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", value)
        ):
            raise ValueError("Signed exact decimal string required")
        row["value"] = Decimal(value)
        number(row["value"])
        for key in ("source_ts", "received_ts"):
            row[key] = utc_string(row[key])
        observations.append(Observation(**row))
    quality = shape(data["quality"], DataQuality)
    quality["as_of"] = utc_string(quality["as_of"])
    if (
        not isinstance(quality["states"], list)
        or len(quality["states"]) > 256
        or any(not isinstance(pair, list) or len(pair) != 2 for pair in quality["states"])
    ):
        raise ValueError("Bounded health array required")
    quality["states"] = tuple(tuple(pair) for pair in quality["states"])
    return LaneContext(
        data["instrument_id"],
        utc_string(data["as_of"]),
        tuple(event_from_dict(event) for event in data["events"]),
        tuple(observations),
        DataQuality(**quality),
        policy,
    )


def replay(value, *, expected_lineage=None):
    context = context_from_dict(value)
    if expected_lineage is not None and context.lineage_id != expected_lineage:
        raise ValueError("Lane context lineage mismatch")
    return assess_all(context)
