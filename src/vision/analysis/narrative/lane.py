"""Narrative replay interface; no LLM facts, network or control calls."""

from vision.analysis.contracts import Lane
from vision.analysis.fixtures import assess_fixture


def assess(context):
    return assess_fixture(context, Lane.NARRATIVE)
