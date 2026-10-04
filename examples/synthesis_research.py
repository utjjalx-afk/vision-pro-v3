"""Synthetic Phase-6 plumbing: no eligible outcomes means WAIT and no intent."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from lanes_research import synthetic_context

from vision.analysis.contracts import wire
from vision.analysis.runner import assess_all
from vision.analysis.synthesizer.contracts import SynthesisInput
from vision.analysis.synthesizer.engine import synthesize
from vision.core.contracts import AssetClass, InstrumentSpec
from vision.core.instruments import (
    CanonicalSymbol,
    InstrumentRecord,
    QuantityUnit,
    SpecProvenance,
    digest,
)
from vision.intents.models import candidate


def synthetic_inputs():
    context = synthetic_context()
    spec = InstrumentSpec(
        context.instrument_id,
        "BINANCE_SPOT",
        "BTCUSDT",
        AssetClass.CRYPTO,
        "BTC",
        "USDT",
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal(1),
    )
    record = InstrumentRecord(
        spec,
        CanonicalSymbol(AssetClass.CRYPTO, "BTC", "USDT"),
        QuantityUnit.BASE,
        SpecProvenance(
            "binance.spot",
            "offline-fixture",
            context.as_of,
            digest({"synthetic": True}),
            "fixture-v1",
        ),
        "linear_quote",
    )
    return SynthesisInput(record, context.as_of, assess_all(context), (), context.quality)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-input", type=Path)
    args = parser.parse_args()
    inputs = synthetic_inputs()
    if args.write_input:
        args.write_input.write_text(json.dumps(wire(inputs), indent=2) + "\n", encoding="utf-8")
    decision = synthesize(inputs)
    intent = candidate(decision)
    print(
        json.dumps(
            {"decision": decision.to_dict(), "intent": intent.to_dict() if intent else None},
            sort_keys=True,
        )
    )
