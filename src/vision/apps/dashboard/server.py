"""Loopback-only, authenticated viewer Command Center. No execution dispatch exists."""

import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from vision.apps.dashboard.indicators import calculate
from vision.apps.dashboard.runtime import broker_feed, depth_feed, native_broker_feed, public_feed
from vision.apps.dashboard.state import DashboardState
from vision.broker.client import BrokerBridgeClient
from vision.journal.replay import replay
from vision.market_data.adapters.binance import (
    BinanceNormalizer,
    BinanceREST,
    Subscription,
    symbol_name,
)

INTERVALS = (60, 300, 900, 3600, 14400, 86400)


def changed(previous, current):
    """Nested object patches keep unchanged candle/indicator history off the wire."""
    if isinstance(current, dict) and isinstance(previous, dict):
        return {k: changed(previous.get(k), v) for k, v in current.items() if v != previous.get(k)}
    if isinstance(current, list) and isinstance(previous, list) and current and previous:
        key = (
            "time" if all(isinstance(v, dict) and "time" in v for v in current + previous) else "id"
        )
        if all(isinstance(v, dict) and key in v for v in current + previous):
            old = {v[key]: v for v in previous}
            return {
                "$merge_by": key,
                "$retain": [v[key] for v in current],
                "$rows": [v for v in current if v != old.get(v[key])],
            }
    return current


def create_app(state=None, *, token, static=None, feed=None, bridge=None, port=8787, monitor=None):
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("Private viewer token required")
    state = state or DashboardState()
    token_hash = hashlib.sha256(token.encode()).digest()
    sessions, attempts = {}, []
    active_sockets = set()
    origin = f"http://127.0.0.1:{port}"

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if feed:
            symbol, intervals = feed
            tasks = [
                asyncio.create_task(public_feed(state, symbol, intervals)),
                asyncio.create_task(depth_feed(state, symbol)),
            ]
        if monitor:
            tasks.append(asyncio.create_task(native_broker_feed(state, monitor)))
        elif bridge:
            tasks.append(asyncio.create_task(broker_feed(state, bridge)))
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.state.projection = state

    def authorized(cookie):
        expires = sessions.get(cookie, 0)
        return expires > time.monotonic()

    @app.middleware("http")
    async def guard(request, call_next):
        if request.url.path.startswith("/api/") and not authorized(
            request.cookies.get("vision_viewer")
        ):
            return JSONResponse({"detail": "Viewer authentication required"}, status_code=401)
        if request.url.path.startswith("/api/") and request.method != "GET":
            return JSONResponse({"detail": "Read-only phase"}, status_code=405)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.post("/auth/session")
    async def login(request: Request):
        if request.headers.get("origin") != origin:
            raise HTTPException(403, "Same-origin login required")
        now = time.monotonic()
        attempts[:] = [t for t in attempts if now - t < 60]
        if len(attempts) >= 10:
            raise HTTPException(429, "Login rate limit")
        attempts.append(now)
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 1024:
                raise HTTPException(413, "Bounded login required")
        try:
            value = json.loads(raw)
            secret = value["token"]
            if set(value) != {"token"} or not isinstance(secret, str):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "Invalid login") from None
        if not hmac.compare_digest(hashlib.sha256(secret.encode()).digest(), token_hash):
            raise HTTPException(401, "Invalid viewer token")
        for key in tuple(sessions):
            if sessions[key] <= now:
                del sessions[key]
        if len(sessions) >= 16:
            raise HTTPException(429, "Session capacity")
        session = secrets.token_urlsafe(48)
        sessions[session] = now + 3600
        response = JSONResponse({"role": "VIEWER", "expires_in": 3600})
        response.set_cookie(
            "vision_viewer", session, httponly=True, samesite="strict", max_age=3600
        )
        return response

    @app.post("/auth/logout")
    async def logout(request: Request):
        if request.headers.get("origin") != origin:
            raise HTTPException(403, "Same-origin required")
        sessions.pop(request.cookies.get("vision_viewer"), None)
        response = JSONResponse({"role": None})
        response.delete_cookie("vision_viewer")
        return response

    async def selected(instrument, seconds):
        if instrument.startswith("MT5:DEMO:"):
            if (
                monitor is None
                or seconds not in INTERVALS
                or instrument not in {s["instrument_id"] for s in monitor.instruments}
            ):
                raise HTTPException(400, "Native demo monitor is unavailable")
            try:
                market = await asyncio.to_thread(monitor.market, instrument, seconds)
            except (ValueError, RuntimeError, OSError):
                raise HTTPException(
                    503, "Native DEMO history unavailable or identity changed"
                ) from None
            data = state.summary(market_override=market)
            data["agents"] = {
                "assessments": [],
                "decision": None,
                "reason": "BROKER_HISTORY_IS_NOT_APPROVED_ANALYSIS",
            }
            data["instruments"] += monitor.instruments
            return data
        if (
            seconds not in INTERVALS
            or len(instrument) > 120
            or instrument not in state.hub.instruments
            and instrument != "BINANCE:SPOT:BTCUSDT"
        ):
            raise HTTPException(400, "Supported bounded market selection required")
        data = state.summary(instrument, seconds)
        if monitor:
            data["instruments"] += monitor.instruments
        return data

    @app.get("/api/research/report")
    async def research_report(format: str = "json"):
        from vision.apps.dashboard.research import daily_report, markdown

        try:
            deals = await asyncio.to_thread(monitor.deals_report) if monitor else None
        except (ValueError, RuntimeError, OSError):
            raise HTTPException(503, "Pinned native DEMO deal history unavailable") from None
        report = daily_report(state, deals)
        name = f"mt5-demo-{report['date_ist']}"
        if format == "md":
            return Response(
                markdown(report),
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
            )
        if format != "json":
            raise HTTPException(400, "JSON or Markdown report required")
        return JSONResponse(
            report, headers={"Content-Disposition": f'attachment; filename="{name}.json"'}
        )

    @app.get("/api/dashboard/summary")
    async def summary(instrument: str = "BINANCE:SPOT:BTCUSDT", seconds: int = 300):
        return await selected(instrument, seconds)

    @app.get("/api/market/history")
    async def history(instrument: str, seconds: int, before: int):
        await selected(instrument, seconds)
        if instrument.startswith("MT5:DEMO:"):
            try:
                return await asyncio.to_thread(monitor.market, instrument, seconds, before=before)
            except (ValueError, RuntimeError, OSError):
                raise HTTPException(503, "Native historical data unavailable") from None
        spec = state.hub.instruments.get(instrument)
        if spec is None or spec.venue != "BINANCE_SPOT" or not 0 < before <= int(time.time()):
            raise HTTPException(400, "Registered Binance historical selection required")
        intervals = dict(zip(INTERVALS, ("1m", "5m", "15m", "1h", "4h", "1d"), strict=True))
        subscription = Subscription(spec.symbol, ("bar",), intervals[seconds])
        try:
            rows = await asyncio.to_thread(
                BinanceREST().bars, subscription, end=before * 1000 - 1, limit=512
            )
            normalizer = BinanceNormalizer(subscription)
            events = [normalizer.rest_bar(row, state.clock()) for row in rows]
            events = [replace(e, delivery_kind="backfill") for e in events if e is not None]
            events.sort(key=lambda e: e.payload.open_ts)
            candles = [
                {
                    "time": int(e.payload.open_ts.timestamp()),
                    **{
                        k: str(getattr(e.payload, k))
                        for k in ("open", "high", "low", "close", "volume")
                    },
                    "event_id": e.event_id,
                    "delivery_kind": "backfill",
                    "source_epoch": e.source_epoch,
                }
                for e in events
            ]
            indicators = {
                k: [
                    {"time": candles[i]["time"], "value": str(v)}
                    for i, v in enumerate(values)
                    if v is not None
                ]
                for k, values in calculate([e.payload for e in events]).items()
                if not k.startswith("_")
            }
            return {
                "candles": candles,
                "indicators": indicators,
                "source": "binance.spot",
                "health": "HISTORICAL",
                "spec": spec.to_dict(),
            }
        except (RuntimeError, OSError, ValueError):
            raise HTTPException(503, "Historical data unavailable") from None

    sections = {
        "market/candles": "market",
        "market/quotes": "market",
        "orderflow/trades": "market",
        "orderflow/footprint": "market",
        "orderflow/depth": "market",
        "agents": "agents",
        "synthesis": "agents",
        "risk": "risk",
        "portfolio": "portfolio",
        "execution": "execution",
        "journal": "journal",
        "system-health": "system",
    }

    @app.get("/api/{section:path}")
    async def section(section: str, instrument: str = "BINANCE:SPOT:BTCUSDT", seconds: int = 300):
        if section not in sections:
            raise HTTPException(404, "Unknown read projection")
        data = await selected(instrument, seconds)
        if section == "journal":
            return {"report": data["journal"], "entries": data["journal_entries"]}
        if section == "market/candles":
            return {
                k: data["market"][k]
                for k in ("candles", "indicators", "spec", "source", "health", "source_epoch")
            }
        if section == "market/quotes":
            return {k: data["market"][k] for k in ("quote", "health", "quote_age_seconds")}
        if section == "orderflow/depth":
            return data["market"]["depth"]
        if section.startswith("orderflow/"):
            return data["market"]["flow"]
        return data[sections[section]]

    @app.websocket("/ws/{channel}")
    async def stream(ws: WebSocket, channel: str):
        if (
            channel not in {"dashboard", "market", "orderflow"}
            or len(active_sockets) >= 8
            or ws.headers.get("origin") != origin
            or not authorized(ws.cookies.get("vision_viewer"))
        ):
            await ws.close(code=1008)
            return
        instrument = ws.query_params.get("instrument", "BINANCE:SPOT:BTCUSDT")
        try:
            seconds = int(ws.query_params.get("seconds", "300"))
            data = await selected(instrument, seconds)
        except (ValueError, HTTPException):
            await ws.close(code=1008)
            return
        await ws.accept()
        active_sockets.add(ws)
        # At most one in-flight batch per socket. Slow clients close, then REST resnapshot.
        count, previous = 0, None
        try:
            while authorized(ws.cookies.get("vision_viewer")):
                data = await selected(instrument, seconds)
                if channel == "market":
                    data = data["market"]
                elif channel == "orderflow":
                    data = data["market"]["flow"]
                if count % 20 == 0 or previous is None:
                    batch = {"kind": "snapshot", "sequence": count, "data": data}
                else:
                    patch = changed(previous, data)
                    # Candles / indicators change only on bar arrivals, not every raw tick.
                    batch = {"kind": "patch", "sequence": count, "data": patch}
                await asyncio.wait_for(ws.send_json(batch), 2)
                previous, count = data, count + 1
                await asyncio.sleep(0.25)
            await ws.close(code=1008)
        except (WebSocketDisconnect, RuntimeError, TimeoutError):
            return
        finally:
            active_sockets.discard(ws)

    if static and (Path(static) / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=Path(static) / "assets"), name="assets")

        @app.get("/")
        async def index():
            return FileResponse(Path(static) / "index.html")

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Read-only Vision Pro V3 Command Center")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--feed", choices=["off", "binance"], default="off")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--mt5-terminal", type=Path)
    parser.add_argument("--bridge-token-file", type=Path)
    parser.add_argument("--bridge-port", type=int, default=8765)
    parser.add_argument("--journal-export", type=Path)
    parser.add_argument("--static", type=Path, default=Path("apps/dashboard/dist"))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a nonprivileged port")
    symbol_name(args.symbol)
    args.token_file.parent.mkdir(parents=True, exist_ok=True)
    if not args.token_file.exists():
        args.token_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        args.token_file.chmod(0o600)
    token = args.token_file.read_text(encoding="utf-8").strip()
    state = DashboardState()
    if args.journal_export:
        raw = args.journal_export.read_bytes()
        if len(raw) > 50000000:
            raise ValueError("Journal export exceeds bound")
        value = json.loads(raw)
        state.journal_report = replay(value)
        state.journal = tuple(value["entries"])
    bridge = (
        BrokerBridgeClient(args.bridge_token_file.read_text().strip(), port=args.bridge_port)
        if args.bridge_token_file
        else None
    )
    monitor = None
    if args.mt5_terminal:
        from vision.apps.dashboard.mt5_market import SYMBOLS, DemoMarketReader
        from vision.broker.mt5_bridge import MT5Reader

        if not args.bridge_token_file:
            parser.error("Native demo monitor requires the existing private bridge identity key")
        reader = MT5Reader.connect(
            {v: k for k, v in SYMBOLS.items()},
            args.bridge_token_file.read_bytes().strip(),
            terminal_path=str(args.mt5_terminal),
        )
        monitor = DemoMarketReader(reader)
    feed = (args.symbol, ("1m", "5m", "15m", "1h", "4h", "1d")) if args.feed == "binance" else None
    app = create_app(
        state,
        token=token,
        static=args.static,
        feed=feed,
        bridge=bridge,
        port=args.port,
        monitor=monitor,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
