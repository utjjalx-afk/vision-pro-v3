"""Explicit synthetic offline example. No imported module runs a strategy automatically."""

import argparse
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from journal_research import demo_broker

from vision.analysis.contracts import wire
from vision.core.contracts import BarPayload, CanonicalMarketEvent, EventType
from vision.journal.models import Experiment
from vision.journal.repository import SQLiteRepository, canonical, export
from vision.journal.service import ResearchJournal
from vision.research.audit import SuiteDefinition, record_suite
from vision.research.backtest import AcceptancePolicy, BacktestInput, StrategyDefinition, run


def fixture(commit_sha):
    broker, record, at = demo_broker()
    rows = ((100, 101, 99, 100, 10), (100, 111, 99, 110, 10), (110, 122, 100, 120, 10))
    events = tuple(
        CanonicalMarketEvent(
            f"synthetic-bar-{index}",
            "binance.spot",
            record.spec.instrument_id,
            EventType.BAR,
            at + timedelta(minutes=index + 1),
            at + timedelta(minutes=index + 1),
            index,
            BarPayload(*(Decimal(v) for v in row), 60, at + timedelta(minutes=index)),
            continuity="contiguous",
            delivery_kind="backfill",
        )
        for index, row in enumerate(rows)
    )
    return BacktestInput(
        events,
        record,
        broker.rules[record.spec.instrument_id],
        StrategyDefinition(lookback=2, warmup=2),
        broker.costs,
        broker.limits,
        AcceptancePolicy(minimum_trades=1),
        Decimal(10000),
        commit_sha,
    )


def main():
    parser = argparse.ArgumentParser(description="Synthetic failure-oriented backtest example")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--commit-sha", required=True)
    args = parser.parse_args()
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    base = fixture(args.commit_sha)
    result = run(base)
    (directory / "input.json").write_text(canonical(wire(base)) + "\n", encoding="utf-8")
    (directory / "run.json").write_text(
        canonical({"input": wire(base), "run": result.to_dict()}) + "\n", encoding="utf-8"
    )
    definition = SuiteDefinition(
        base,
        (base.costs, replace(base.costs, slippage_bps=Decimal(20))),
        (base.strategy, replace(base.strategy, threshold=Decimal("0.02"))),
        train=4,
        test=4,
        holdout=4,
    )
    at = base.events[-1].received_ts
    repository = SQLiteRepository(directory / "research.sqlite")
    try:
        journal = ResearchJournal(repository, clock=lambda: at)
        journal.open_experiment(
            Experiment(
                "synthetic-backtest",
                "synthetic-family",
                canonical(definition.config()),
                args.commit_sha,
                (base.record.revision,),
                (("binance.spot", 0),),
                None,
                base.costs,
                at,
            )
        )
        entry = record_suite(journal, "synthetic-backtest", definition)
        (directory / "journal.json").write_text(
            canonical(export(repository)) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "run": result.status.value,
                    "suite": entry.payload["report"]["status"],
                    "promotion": "NONE",
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
