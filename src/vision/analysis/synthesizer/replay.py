"""Strict synthesis replay from lane, metadata and prospective grading records."""

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from vision.analysis.contracts import Availability, Bias, Evidence, Feature, Lane, LaneAssessment
from vision.analysis.synthesizer.contracts import SynthesisInput, SynthesisPolicy
from vision.analysis.synthesizer.engine import synthesize
from vision.analysis.synthesizer.reliability import ReliabilityPolicy
from vision.core.codec import event_from_dict, utc_string
from vision.core.state.portfolio import DataQuality
from vision.core.state.replay import record_from_dict, shape
from vision.outcomes.prospective import ForecastCommitment, GradedOutcome, GradingPolicy


def decimal(value):
    if (
        not isinstance(value, str)
        or len(value) > 1100
        or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", value)
    ):
        raise ValueError("Exact bounded decimal string required")
    result = Decimal(value)
    if len(result.as_tuple().digits) > 1024 or abs(result.as_tuple().exponent) > 512:
        raise ValueError("Derived decimal exceeds replay bounds")
    return result


def array(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Bounded replay array required")
    return value


def quality(value):
    data = shape(value, DataQuality)
    data["as_of"] = utc_string(data["as_of"])
    states = array(data["states"], 256)
    if any(not isinstance(pair, list) or len(pair) != 2 for pair in states):
        raise ValueError("Explicit health pairs required")
    data["states"] = tuple(tuple(pair) for pair in states)
    return DataQuality(**data)


def assessment(value):
    data = shape(value, LaneAssessment)
    data["lane"] = Lane(data["lane"])
    data["status"] = Availability(data["status"])
    data["bias"] = Bias(data["bias"]) if data["bias"] is not None else None
    for key in ("score", "confidence"):
        data[key] = decimal(data[key]) if data[key] is not None else None
    data["timestamp"] = utc_string(data["timestamp"])
    data["data_quality"] = quality(data["data_quality"])
    ev = []
    for row in array(data["evidence"], 2048):
        row = shape(row, Evidence)
        for key in ("source_ts", "received_ts"):
            row[key] = utc_string(row[key])
        ev.append(Evidence(**row))
    features = []
    for row in array(data["features"], 256):
        row = shape(row, Feature)
        row["value"] = decimal(row["value"]) if row["value"] is not None else None
        features.append(Feature(**row))
    data["evidence"], data["features"] = tuple(ev), tuple(features)
    data["reasons"] = tuple(array(data["reasons"], 256))
    return LaneAssessment(**data)


def policy(value, cls, duration_keys=(), decimal_keys=()):
    data = shape(value, cls)
    for key in duration_keys:
        if type(data[key]) is not int or not 0 < data[key] <= 7 * 86400 * 1000000:
            raise ValueError("Bounded integer duration required")
        data[key] = timedelta(microseconds=data[key])
    for key in decimal_keys:
        data[key] = decimal(data[key])
    return data


def grading_policy(value):
    return GradingPolicy(
        **policy(
            value, GradingPolicy, ("horizon", "max_age", "terminal_tolerance"), ("minimum_move",)
        )
    )


def outcome(value):
    data = shape(value, GradedOutcome)
    commitment = shape(data["commitment"], ForecastCommitment)
    commitment["assessment"] = assessment(commitment["assessment"])
    commitment["baseline"] = event_from_dict(commitment["baseline"])
    commitment["registered_at"] = utc_string(commitment["registered_at"])
    commitment["policy"] = grading_policy(commitment["policy"])
    return GradedOutcome(
        ForecastCommitment(**commitment),
        event_from_dict(data["terminal"]),
        utc_string(data["graded_at"]),
        quality(data["quality"]),
    )


def inputs_from_dict(value):
    try:
        data = shape(value, SynthesisInput)
        reliability = policy(
            data["reliability_policy"], ReliabilityPolicy, decimal_keys=("prior_strength",)
        )
        reliability["grading_policy"] = grading_policy(reliability["grading_policy"])
        synthesis = SynthesisPolicy(
            **policy(
                data["policy"],
                SynthesisPolicy,
                ("max_age", "max_spec_age"),
                (
                    "minimum_support",
                    "minimum_direction",
                    "maximum_disagreement",
                    "maximum_lane_share",
                ),
            )
        )
        return SynthesisInput(
            record_from_dict(data["record"]),
            utc_string(data["as_of"]),
            tuple(assessment(row) for row in array(data["assessments"], 4)),
            tuple(outcome(row) for row in array(data["outcomes"], 10000)),
            quality(data["quality"]),
            ReliabilityPolicy(**reliability),
            synthesis,
            data["regime"],
        )
    except (TypeError, KeyError, IndexError, OverflowError, InvalidOperation) as error:
        raise ValueError("Invalid synthesis replay structure") from error


def replay(value, *, expected_lineage=None):
    inputs = inputs_from_dict(value)
    if expected_lineage is not None and inputs.lineage_id != expected_lineage:
        raise ValueError("Synthesis input lineage mismatch")
    return synthesize(inputs)
