# Windows VPS — DEMO operations

Status: deployment/observation implementation; actual VPS deployment and trading
acceptance pending. This branch combines the Command Center and existing guarded
Phase 12 gateway without changing their earlier branches or main. Real accounts
remain blocked. Nothing here starts an automatic entry strategy or arms execution.

## Requested experiment

Unlimited daily trade count (no artificial daily count cap), both BUY/SELL,
mandatory SL and requested TP, profit lock and trailing protection. Unlimited
count does not permit duplicate sends, overlapping entries, grid or martingale;
the existing gateway remains one isolated position at a time. Strategy rules,
per-trade risk and protection distances await the operator's specification.

`DemoPolicy.daily_loss_limit_enabled=false` explicitly disables only the daily
equity-loss veto for this DEMO experiment. It defaults to true; older private
server configurations retain true. The choice is committed into the risk-decision
identity and journal replay. Per-trade/aggregate risk, drawdown, exposure, account,
margin, source freshness and SL gates are still enforced. The shared risk governor
and real/live configuration are unchanged. A changed policy cannot rewrite a
committed execution's lineage.

`DemoPolicy.require_tp=true` makes TP mandatory before any native submission;
the VPS launcher requires it when enabling demo. Legacy non-VPS configurations
retain their earlier optional-TP default. No TP price or risk percentage is invented.

`vision.execution.demo.protection.preview` computes native-tick-aligned TP,
profit lock and tightening-only trailing SL proposals. It does **not** modify
broker SL/TP. Native protection amendment, uncertain amendment reconciliation,
restart recovery and an automatic strategy worker are pending; this deployment
must not be represented as unattended trading-ready.

## Install in the VPS user's interactive Windows session

Log into the VPS with RDP yourself; do not put its password in the repository,
chat, command line or scripts. Extract the reviewed source/build package into
`C:\VisionProV3`. Python 3.11 and the official MT5 Windows terminal must already
be installed. Open MT5 and log into the intended **DEMO** account manually.
Keep that Windows session logged in; the logon task uses this same user's token,
not SYSTEM. This setup does not promise operation after Windows logoff.

```powershell
cd C:\VisionProV3
.\scripts\setup-vps-demo.ps1
.\.venv\Scripts\python.exe scripts/inspect-vps-demo.py --terminal 'C:\Program Files\MetaTrader 5\terminal64.exe' --bridge-token-file data/private/bridge.token
```

Review the exact native account identity, account currency and four-symbol mapping
before pinning them. No acceptance is granted by inspection. Future native quote
timestamps, including the existing +3h discrepancy, are not shifted or bypassed.
Reconcile Phase 11 on the **VPS's own** connected account and terminal before
enabling demo submission. Previous local evidence does not accept a new VPS account.

Create `data/private/operations.json` with exact fields, verified HMAC identity,
verified account currency and absolute paths. For observation-only mode:

```json
{
  "viewer_token_file": "C:/VisionProV3/data/private/viewer.token",
  "dashboard_port": 8787,
  "account_identity": "REPLACE_WITH_VERIFIED_ACCOUNT_HMAC",
  "currency": "REPLACE_WITH_NATIVE_ACCOUNT_CURRENCY",
  "journal": null
}
```

Create `data/private/launcher.json`:

```json
{
  "OperationsConfig": "C:/VisionProV3/data/private/operations.json",
  "TerminalPath": "C:/Program Files/MetaTrader 5/terminal64.exe",
  "BridgeTokenFile": "C:/VisionProV3/data/private/bridge.token",
  "ViewerTokenFile": "C:/VisionProV3/data/private/viewer.token",
  "LogDirectory": "C:/VisionProV3/data/vps-observations",
  "EnableDemo": false
}
```

```powershell
.\scripts\start-vps-demo-from-config.ps1 -Config data/private/launcher.json
```

The dashboard stays on `127.0.0.1:8787`; view it in the VPS browser over RDP.
No public port, firewall change or public viewer token is required. The collector
authenticates with the separate viewer token and checks account identity/currency
on every read. Missing responses, mismatches and journal errors produce explicit
failed observations; they never become zero positions or zero PnL.

After checking foreground startup, optionally install the logon task:

```powershell
.\scripts\install-vps-demo-task.ps1 -LauncherConfig data/private/launcher.json
Start-ScheduledTask -TaskName VisionProV3-DemoOperations
```

Stop the foreground test before starting the task. Port collisions fail without
killing existing processes. A failed child stops its owned process group; the
task permits three bounded restarts, all DISARMED. It stores no Windows password.
Normal runtime stdout/stderr are timestamped private files. Monitor the task's
last result and preserve failures; do not assume uptime from task registration.

## Daily evidence and exports

Every 60 seconds the collector writes a new integrity-digested observation under
`data/vps-observations/YYYY-MM-DD/`, grouped by **IST receipt date**. It never
submits, closes, repairs or arms anything. At/after 20:00 IST it writes that day's
ZIP snapshot once; at the next date rollover it exports the completed previous
day. Startup after 20:00 collects a current snapshot, not invented earlier data.
Days while the process/PC was down stay missing. Existing ZIP files are not replaced.
The ZIP contains original JSON observations, CSV health chronology, a Markdown
summary and SHA-256 manifest. A configured journal is opened read-only, its chain
and demo lineage replayed, and its full snapshot included at export time.
This is a journal-at-export snapshot, not a claim every trade belongs to that day.
Daily PnL remains unavailable until broker-clock and deal-period attribution reconcile.

Manual export (new output filename each time):

```powershell
.\.venv\Scripts\python.exe -m vision.operations.demo export --directory data/vps-observations --date 2026-10-07 --output data/exports/demo-2026-10-07.zip
```

Add `--journal data/private/demo.sqlite` only for the actual gateway journal.
Empty or tampered days, mixed-account files and unknown journal ownership block
export. Copy these private ZIP files through RDP to download them; they contain
account economics and must not be committed to the public repository.

## Optional gateway process — still DISARMED

The existing private demo config schema is in PHASE12_DEMO_EXECUTION.md. Add
`DemoConfig` and `Journal` paths to launcher.json and the identical journal path
to operations.json. Gateway binds loopback port 8766 and uses the existing
separate broker key through its environment. This launcher does not expose it
publicly or reuse the viewer key. `EnableDemo=true` requires explicit matching
Phase 11 acceptance; startup/restart still never arms the gateway.

No connected-terminal order or protection amendment is part of installation.
Actual trading remains pending verified broker clocks/economics, selected strategy,
explicit per-trade risk/protection settings and a separately verified demo smoke run.
