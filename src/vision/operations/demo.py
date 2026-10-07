"""Continuous DEMO observations, verified journal snapshots and daily ZIP exports."""

import argparse
import csv
import hashlib
import io
import json
import sqlite3
import time
import uuid
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener
from zoneinfo import ZoneInfo

from vision.execution.demo.journal import replay
from vision.execution.demo.parity import parity
from vision.journal.repository import SQLiteRepository, verify
from vision.strategies.dsl import strict_json

IST = ZoneInfo("Asia/Kolkata")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def journal_snapshot(path, account_identity):
    if path is None:
        return {"state": "UNAVAILABLE", "reason": "NO_JOURNAL_CONFIGURED"}
    repository = SQLiteRepository(path, readonly=True)
    try:
        entries = repository.entries()
        heads = replay(entries)
        if any(h["lineage"]["account_identity"] != account_identity for h in heads.values()):
            raise ValueError("JOURNAL_ACCOUNT_MISMATCH")
        return {
            "state": "VERIFIED",
            "journal_head": verify(entries),
            "entry_count": len(entries),
            "orders": [{"client_order_id": cid, "state": h["state"]} for cid, h in heads.items()],
            "parity": [
                {k: v for k, v in parity(h).items() if k != "broker_facts"}
                for h in heads.values()
                if "request" in h["details"]
            ],
            "pnl_period_attribution": "UNVERIFIED_BROKER_CLOCK",
        }
    finally:
        repository.close()


def fetch_report(viewer_token_file, port):
    # Fixed loopback destination. This client has no order/arm/close endpoint.
    origin = f"http://127.0.0.1:{port}"
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    token = Path(viewer_token_file).read_text(encoding="utf-8").strip()
    request = Request(
        origin + "/auth/session",
        data=encode({"token": token}),
        headers={"Origin": origin, "Content-Type": "application/json"},
    )
    with opener.open(request, timeout=10) as response:
        response.read(4096)
    with opener.open(origin + "/api/research/report", timeout=15) as response:
        raw = response.read(2000001)
    if len(raw) > 2000000:
        raise ValueError("REPORT_TOO_LARGE")
    return json.loads(raw)


def observe(config, *, now=None, fetch=fetch_report):
    now = now or datetime.now(UTC)
    try:
        report = fetch(config["viewer_token_file"], config["dashboard_port"])
        account = report.get("account") or {}
        if (
            account.get("demo") is not True
            or account.get("connected") is not True
            or account.get("identity") != config["account_identity"]
            or account.get("currency") != config["currency"]
            or report.get("live_enabled") is not False
        ):
            raise ValueError("PINNED_CONNECTED_DEMO_REQUIRED")
        journal = journal_snapshot(config.get("journal"), config["account_identity"])
        return {
            "schema": "vps-demo-observation-v1",
            "observed_at": now.isoformat(),
            "day_ist": now.astimezone(IST).date().isoformat(),
            "status": report["broker_state"],
            "account_identity": config["account_identity"],
            "currency": config["currency"],
            "report": report,
            "execution_journal": journal,
            "collector_orders": 0,
            "live_enabled": False,
        }
    except (OSError, ValueError, RuntimeError, KeyError, ArithmeticError, sqlite3.Error) as error:
        # Never persist raw exceptions, HTTP bodies, tokens or unpinned account data.
        return {
            "schema": "vps-demo-observation-v1",
            "observed_at": now.isoformat(),
            "day_ist": now.astimezone(IST).date().isoformat(),
            "status": "COLLECTION_FAILED",
            "reason": "PIN_OR_REPORT_OR_JOURNAL_UNAVAILABLE",
            "category": type(error).__name__,
            "collector_orders": 0,
            "live_enabled": False,
        }


def persist(directory, value):
    directory = Path(directory) / date.fromisoformat(value["day_ist"]).isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    body = {"evidence": value, "sha256": hashlib.sha256(encode(value)).hexdigest()}
    path = directory / f"observation-{uuid.uuid4().hex}.json"
    with path.open("xb") as handle:
        handle.write(encode(body))
    return path


def daily_export(directory, day, output, *, journal=None):
    day = date.fromisoformat(day).isoformat()
    paths = sorted((Path(directory) / day).glob("observation-*.json"))
    if not paths or len(paths) > 10000:
        raise ValueError("BOUNDED_EXISTING_DAILY_EVIDENCE_REQUIRED")
    files, rows, pins = {}, [], set()
    total = 0
    for path in paths:
        total += path.stat().st_size
        if total > 100000000:
            raise ValueError("DAILY_EXPORT_EXCEEDS_100MB")
        raw = path.read_bytes()
        body = json.loads(raw)
        evidence = body["evidence"]
        if (
            body["sha256"] != hashlib.sha256(encode(evidence)).hexdigest()
            or evidence["day_ist"] != day
            or datetime.fromisoformat(evidence["observed_at"]).astimezone(IST).date().isoformat()
            != day
        ):
            raise ValueError("DAILY_EVIDENCE_INTEGRITY_FAILURE")
        if evidence["status"] != "COLLECTION_FAILED":
            account = evidence["report"]["account"]
            if (
                account["identity"] != evidence["account_identity"]
                or account["currency"] != evidence["currency"]
                or account["demo"] is not True
            ):
                raise ValueError("DAILY_EVIDENCE_ACCOUNT_MISMATCH")
            pins.add((evidence["account_identity"], evidence["currency"]))
        files[path.name] = raw
        rows.append((evidence["observed_at"], evidence["status"], evidence.get("reason", "")))
    if len(pins) > 1:
        raise ValueError("MIXED_ACCOUNT_EXPORT_BLOCKED")
    if journal is not None:
        if not pins:
            raise ValueError("PINNED_ACCOUNT_REQUIRED_FOR_JOURNAL_EXPORT")
        identity = next(iter(pins))[0]
        journal_snapshot(journal, identity)  # Verify demo lineage and ownership first.
        repository = SQLiteRepository(journal, readonly=True)
        try:
            entries = repository.entries()
            heads = replay(entries)
            if any(h["lineage"]["account_identity"] != identity for h in heads.values()):
                raise ValueError("JOURNAL_ACCOUNT_MISMATCH")
            files["journal-at-export.json"] = encode(
                {"version": 1, "entries": [e.to_dict() for e in entries], "head": verify(entries)}
            )
            trades = io.StringIO(newline="")
            writer = csv.writer(trades)
            writer.writerow(
                (
                    "client_order_id",
                    "state",
                    "symbol",
                    "side",
                    "currency",
                    "broker_reported_lifetime_pnl",
                    "period_attribution",
                )
            )
            for cid, head in heads.items():
                details = head["details"]
                request = details.get("request", {})
                text = [
                    cid,
                    head["state"],
                    request.get("symbol", ""),
                    request.get("side", ""),
                    details.get("currency", ""),
                ]
                text = ["'" + v if v.startswith(("=", "+", "-", "@")) else v for v in text]
                pnl = details.get("realized_pnl")
                if pnl is not None and not Decimal(pnl).is_finite():
                    raise ValueError("FINITE_BROKER_PNL_REQUIRED")
                writer.writerow(
                    (
                        *text,
                        "" if pnl is None else str(Decimal(pnl)),
                        "JOURNAL_AT_EXPORT_NOT_DAILY_PNL",
                    )
                )
            files["trades-at-export.csv"] = trades.getvalue().encode()
        finally:
            repository.close()
    csv_file = io.StringIO(newline="")
    writer = csv.writer(csv_file)
    writer.writerow(("observed_at_utc", "broker_status", "collection_reason"))
    writer.writerows(sorted(rows))
    files["observations.csv"] = csv_file.getvalue().encode()
    failed = sum(row[1] == "COLLECTION_FAILED" for row in rows)
    files["summary.md"] = (
        f"# MT5 DEMO observations — {day} IST\n\n"
        f"Captures: {len(rows)}; collection failures: {failed}.\n"
        "Timestamp grouping uses collector receipt time, not broker deal time.\n"
        "No automatic trading or strategy acceptance is implied. Daily PnL unavailable\n"
        "until broker-clock and deal-period attribution reconcile. Collector places zero orders.\n"
    ).encode()
    files["manifest.json"] = encode(
        {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())}
    )
    if sum(len(raw) for raw in files.values()) > 100000000:
        raise ValueError("DAILY_EXPORT_EXCEEDS_100MB")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output


def load_config(path):
    value = strict_json(Path(path).read_text(encoding="utf-8"))
    expected = {"viewer_token_file", "dashboard_port", "account_identity", "currency", "journal"}
    if type(value) is not dict or set(value) != expected:
        raise ValueError("EXACT_OPERATIONS_CONFIG_REQUIRED")
    if type(value["dashboard_port"]) is not int or not 1024 <= value["dashboard_port"] <= 65535:
        raise ValueError("NONPRIVILEGED_LOOPBACK_PORT_REQUIRED")
    if not all(
        isinstance(value[k], str) and value[k] for k in expected - {"dashboard_port", "journal"}
    ):
        raise ValueError("EXPLICIT_ACCOUNT_CURRENCY_AND_TOKEN_FILE_REQUIRED")
    return value


def scheduled_exports(directory, observation, previous_day, hour, *, journal=None, now=None):
    """Export failures never stop broker monitoring or disappear from the log."""
    day = observation["day_ist"]
    targets = []
    if previous_day is not None and previous_day != day:
        targets.append((previous_day, f"{previous_day}-final.zip"))
    if (now or datetime.now(UTC)).astimezone(IST).hour >= hour:
        targets.append((day, f"{day}-{hour:02d}h-snapshot.zip"))
    for export_day, name in targets:
        target = Path(directory) / "exports" / name
        if target.exists():
            continue
        try:
            daily_export(directory, export_day, target, journal=journal)
        except (OSError, ValueError, KeyError, ArithmeticError, sqlite3.Error):
            persist(
                directory,
                {**observation, "status": "COLLECTION_FAILED", "reason": "DAILY_EXPORT_FAILED"},
            )
            print(json.dumps({"status": "DAILY_EXPORT_FAILED", "day": export_day}), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Read-only VPS DEMO logger and daily ZIP export")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--config", type=Path, required=True)
    collect.add_argument("--directory", type=Path, required=True)
    collect.add_argument("--interval", type=int, default=60)
    collect.add_argument("--once", action="store_true")
    collect.add_argument("--daily-export-hour", type=int, default=20)
    bundle = commands.add_parser("export")
    bundle.add_argument("--directory", type=Path, required=True)
    bundle.add_argument("--date", required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--journal", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        print(daily_export(args.directory, args.date, args.output, journal=args.journal))
        return
    if not 10 <= args.interval <= 3600:
        parser.error("Collector interval must be 10–3600 seconds")
    if not 0 <= args.daily_export_hour <= 23:
        parser.error("Daily export hour must be 0–23 in Asia/Kolkata")
    config = load_config(args.config)
    previous_day = None
    try:
        while True:
            observation = observe(config)
            path = persist(args.directory, observation)
            print(json.dumps({"status": observation["status"], "file": str(path)}), flush=True)
            day = observation["day_ist"]
            scheduled_exports(
                args.directory,
                observation,
                previous_day,
                args.daily_export_hour,
                journal=config["journal"],
            )
            previous_day = day
            if args.once:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
