"""Macro replay interface; a production macro feed is unavailable."""

from vision.analysis.contracts import Lane
from vision.analysis.fixtures import assess_fixture


def assess(context):
    return assess_fixture(context, Lane.MACRO)
