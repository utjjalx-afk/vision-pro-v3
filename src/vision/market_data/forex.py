"""Explicit authorized snapshot and deterministic offline FX/metals replay."""

from vision.analysis.contracts import wire
from vision.core.codec import utc_string
from vision.core.instruments import digest
from vision.market_data.adapters.oanda import OandaNormalizer, parse_metadata
from vision.market_data.hub import MarketDataHub
from vision.market_data.sessions import SessionState, calendar_from_dict


def snapshot(rest, symbol, granularity, calendar, *, clock, source_epoch):
    """One bounded GET snapshot; credentials and raw account responses never escape."""
    # Validate all requested semantics even when closed and no network is necessary.
    from vision.market_data.adapters.oanda import GRANULARITIES

    OandaNormalizer(symbol, rest.environment, source_epoch=source_epoch)
    if granularity not in GRANULARITIES:
        raise ValueError("Unsupported fixed UTC candle granularity")
    now = clock()
    if calendar.state(now) is not SessionState.OPEN:
        return {"session": calendar.state(now).value, "events": [], "network_requests": 0}
    response = rest.metadata(symbol)
    rows = response["instruments"]
    if not isinstance(rows, list) or len(rows) != 1 or rows[0]["name"] != symbol:
        raise ValueError("Authorized instrument unavailable")
    metadata = parse_metadata(rows[0], rest.environment, clock())
    normalizer = OandaNormalizer(symbol, rest.environment, source_epoch=source_epoch)
    hub = MarketDataHub(clock=clock)
    hub.register_market(metadata, calendar, granularity=granularity)
    rows = rest.pricing(symbol)["prices"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Requested quote unavailable")
    quote = normalizer.quote(rows[0], clock())
    quote_decision = hub.ingest(quote)
    response = rest.candles(symbol, granularity, count=2)
    bars = normalizer.candles(response, clock(), granularity=granularity, delivery_kind="live")
    # Historical REST candles do not enter the live bus. Admit latest finalized pair only.
    decisions = [hub.ingest(bar) for bar in bars[-2:]]
    return {
        "metadata": wire(metadata),
        "status": hub.status(),
        "events": [event.to_dict() for event in hub.bus.drain()],
        "decisions": wire((quote_decision, *decisions)),
        "network_requests": 3,
    }


def replay(value):
    """Synthetic/projected data only: no provider, credentials, clock or network dependency."""
    fields = {
        "version",
        "symbol",
        "environment",
        "granularity",
        "source_epoch",
        "calendar",
        "metadata",
        "observed_at",
        "actions",
    }
    if type(value) is not dict or set(value) != fields or value["version"] != "phase10-v1":
        raise ValueError("Exact Phase-10 replay envelope required")
    if type(value["actions"]) is not list or len(value["actions"]) > 1000:
        raise ValueError("Replay action bound exceeded")
    calendar = calendar_from_dict(value["calendar"])
    now = utc_string(value["observed_at"])
    metadata = parse_metadata(value["metadata"], value["environment"], now)
    if metadata.symbol != value["symbol"]:
        raise ValueError("Replay metadata mismatch")
    normalizer = OandaNormalizer(
        value["symbol"], value["environment"], source_epoch=value["source_epoch"]
    )
    hub = MarketDataHub(clock=lambda: now)
    hub.register_market(metadata, calendar, granularity=value["granularity"])
    trace = []
    for action in value["actions"]:
        if type(action) is not dict or set(action) != {"at", "kind", "payload"}:
            raise ValueError("Exact replay action required")
        at = utc_string(action["at"])
        if at < now:
            raise ValueError("Replay clock cannot regress")
        now = at
        kind, payload = action["kind"], action["payload"]
        decisions = []
        if kind == "quote":
            decisions.append(hub.ingest(normalizer.quote(payload, now)))
        elif kind == "candles":
            for event in normalizer.candles(
                payload, now, granularity=value["granularity"], delivery_kind="live"
            ):
                decisions.append(hub.ingest(event))
        elif kind == "disconnect" and payload is None:
            hub.gate.disconnected()
        elif kind == "reconnect":
            if type(payload) is not dict or set(payload) != {"source_epoch"}:
                raise ValueError("Exact reconnect epoch required")
            if (
                type(payload["source_epoch"]) is not int
                or payload["source_epoch"] <= normalizer.source_epoch
            ):
                raise ValueError("Reconnect source epoch must increase")
            normalizer = OandaNormalizer(
                value["symbol"], value["environment"], source_epoch=payload["source_epoch"]
            )
        elif kind == "recover":
            if type(payload) is not dict or set(payload) != {"pending", "history"}:
                raise ValueError("Exact candle recovery payload required")
            pending = normalizer.candles(
                payload["pending"], now, granularity=value["granularity"], delivery_kind="live"
            )
            history = normalizer.candles(
                payload["history"], now, granularity=value["granularity"], delivery_kind="backfill"
            )
            if len(pending) != 2:
                raise ValueError("Recovery requires one pending bid/ask candle")
            for event in pending:
                decisions.append(
                    hub.recover_market_bar(
                        event,
                        tuple(
                            bar
                            for bar in history
                            if bar.payload.price_basis == event.payload.price_basis
                        ),
                    )
                )
        elif kind == "status" and payload is None:
            pass
        else:
            raise ValueError("Unsupported replay action")
        trace.append(
            {
                "at": action["at"],
                "decisions": wire(tuple(decisions)),
                "events": [event.to_dict() for event in hub.bus.drain()],
                "status": hub.status(),
            }
        )
    result = {"version": "phase10-v1", "input_hash": digest(value), "trace": trace}
    return {**result, "replay_hash": digest(result)}
