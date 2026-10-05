"""Private, immutable daily DEMO observation reports. No execution or orders."""

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener

IST = timezone(timedelta(hours=5, minutes=30))


def daily_report(state, native_deals=None):
    snapshot = state.summary()
    broker = snapshot["broker"]
    native = broker.get("snapshot") or {}
    now = state.clock()
    account = native.get("account")
    return {
        "schema": "demo-research-v1",
        "captured_at": now.isoformat(),
        "date_ist": now.astimezone(IST).date().isoformat(),
        "mode": "DEMO_OBSERVATION_ONLY",
        "execution_authorized": False,
        "live_enabled": False,
        "phase11": snapshot["phase11"],
        "broker_state": broker["state"],
        "broker_reasons": broker.get("reasons", [broker.get("reason")]),
        "account": account,
        "snapshot_id": broker.get("snapshot_id"),
        "specs": native.get("specs", []),
        "quotes": native.get("quotes", []),
        "quote_checks": broker.get("quote_status", []),
        "positions": native.get("positions"),
        "pending_orders": native.get("pending_orders"),
        "risk": snapshot["risk"],
        "market_health": snapshot["market"]["health"],
        "connections": snapshot["connections"],
        "agents": snapshot["agents"],
        "journal_verified": snapshot["journal"] is not None,
        "trades_placed_by_collector": 0,
        "native_deal_history": native_deals,
        "daily_pnl": None,
        "daily_pnl_reason": "NO_VERIFIED_DEAL_JOURNAL_OR_BROKER_CLOCK",
        "research_conclusion": (
            "Diagnostic observation; no trading-edge or execution acceptance claim"
        ),
    }


def markdown(report):
    account = report.get("account") or {}
    rows = "\n".join(
        f"| {q['symbol']} | {q['state']} | {q['source_age_seconds']:.3f} | "
        f"{q['receipt_age_seconds']:.3f} |"
        for q in report["quote_checks"]
    )
    return f"""# MT5 demo daily report — {report["date_ist"]}

Captured UTC: {report["captured_at"]}. Mode: {report["mode"]}.
Broker: {report["broker_state"]}. Reasons: {", ".join(str(v) for v in report["broker_reasons"])}.
Account DEMO: {account.get("demo", "UNKNOWN")}; currency: {account.get("currency", "UNKNOWN")}.
Balance: {account.get("balance", "UNKNOWN")}; equity: {account.get("equity", "UNKNOWN")}.
Free margin: {account.get("free_margin", "UNKNOWN")}.

| Symbol | State | Source age seconds | Receipt age seconds |
|---|---|---:|---:|
{rows}

Positions: {len(report["positions"]) if report["positions"] is not None else "UNKNOWN"}.
Pending orders: {report["pending_orders"]}.
Daily PnL: UNAVAILABLE ({report["daily_pnl_reason"]}).
Phase 11: {report["phase11"]}. Execution DISARMED; live OFF; collector placed no trade.

{report["research_conclusion"]}.
A blocked/missing day is evidence, not a PASS. No timestamp was shifted.
"""


def capture(token_file, output_dir, port=8787, *, start_date=None):
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    origin = f"http://127.0.0.1:{port}"
    token = Path(token_file).read_text(encoding="utf-8").strip()
    with opener.open(
        Request(
            origin + "/auth/session",
            data=json.dumps({"token": token}).encode(),
            headers={"Origin": origin, "Content-Type": "application/json"},
        ),
        timeout=10,
    ) as response:
        response.read()
    with opener.open(origin + "/api/research/report", timeout=15) as response:
        raw = response.read(2000001)
    if len(raw) > 2000000:
        raise ValueError("Bounded report required")
    report = json.loads(raw)
    if (
        report.get("schema") != "demo-research-v1"
        or report.get("live_enabled") is not False
        or report.get("execution_authorized") is not False
    ):
        raise ValueError("Only read-only demo research reports are supported")
    account = report.get("account")
    if account is not None and account.get("demo") is not True:
        raise ValueError("Real account report blocked")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "research-manifest.json"
    if manifest.exists():
        config = json.loads(manifest.read_text(encoding="utf-8"))
        if account and config.get("account_identity") not in (None, account["identity"]):
            raise ValueError("Research account changed; no mixed-account week")
        if account and config.get("account_identity") is None:
            config["account_identity"] = account["identity"]
            manifest.write_text(json.dumps(config, indent=2), encoding="utf-8")
    else:
        config = {
            "schema": "demo-research-week-v1",
            "start_date": start_date or report["date_ist"],
            "days_required": 7,
            "account_identity": account["identity"] if account else None,
            "scheduled_time": "20:00 Asia/Kolkata",
            "mode": "DEMO_OBSERVATION_ONLY",
        }
        manifest.write_text(json.dumps(config, indent=2), encoding="utf-8")
    stamp = report["captured_at"].replace(":", "").replace("+", "_")
    target = directory / f"capture-{stamp}.json"
    report["evidence_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with target.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
    target.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    start = datetime.fromisoformat(config["start_date"]).date()
    expected = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    daily = {}
    for path in sorted(directory.glob("capture-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        digest = value.pop("evidence_sha256")
        if (
            hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            != digest
        ):
            raise ValueError("Research evidence digest mismatch")
        if value["date_ist"] in expected:
            daily[value["date_ist"]] = value
    missing = [day for day in expected if day not in daily]
    summary = {
        "schema": "demo-research-week-v1",
        "expected_dates": expected,
        "days_collected": len(daily),
        "missing_dates": missing,
        "status": "IN_PROGRESS" if missing else "OBSERVATION_WEEK_COMPLETE",
        "execution_acceptance": "NOT_GRANTED",
        "performance_conclusion": "INSUFFICIENT_VERIFIED_TRADE_EVIDENCE",
        "days": [
            {
                "date": day,
                "broker_state": value["broker_state"],
                "reasons": value["broker_reasons"],
                "snapshot_id": value["snapshot_id"],
            }
            for day, value in sorted(daily.items())
        ],
    }
    (directory / "weekly-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = "\n".join(
        f"| {v['date']} | {v['broker_state']} | {', '.join(map(str, v['reasons']))} |"
        for v in summary["days"]
    )
    (directory / "weekly-summary.md").write_text(
        f"""# Seven-day MT5 DEMO research

Status: {summary["status"]}. {len(daily)}/7 dates collected.

| Day IST | Broker | Reasons |
|---|---|---|
{lines}

Missing: {", ".join(missing) or "none"}.
Execution acceptance NOT GRANTED. Performance evidence insufficient;
diagnostic observations cannot establish strategy edge.
""",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_file": str(target.resolve()),
                "broker_state": report["broker_state"],
                "days_collected": len(daily),
                "week_status": summary["status"],
            }
        )
    )
    return target


def main():
    parser = argparse.ArgumentParser(
        description="Extract private MT5 demo observation reports; no orders"
    )
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Nonprivileged loopback port required")
    if args.start_date:
        datetime.fromisoformat(args.start_date).date()
    capture(args.token_file, args.output_dir, args.port, start_date=args.start_date)


if __name__ == "__main__":
    main()
