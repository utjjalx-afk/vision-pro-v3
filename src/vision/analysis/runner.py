"""Independent pure lanes: each receives only the same immutable input context."""

from vision.analysis.contracts import LaneContext
from vision.analysis.flow.lane import assess as flow
from vision.analysis.macro.lane import assess as macro
from vision.analysis.narrative.lane import assess as narrative
from vision.analysis.technical.lane import assess as technical


def assess_all(context):
    if not isinstance(context, LaneContext):
        raise ValueError("LaneContext required")
    return tuple(lane(context) for lane in (technical, flow, macro, narrative))
