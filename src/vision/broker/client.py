"""Cross-platform loopback client; never loads the Windows native package."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from vision.analysis.contracts import wire
from vision.broker.codec import receipt_from_dict, snapshot_from_dict
from vision.market_data.adapters.binance import NoRedirect
from vision.strategies.dsl import strict_json


class BrokerBridgeClient:
    def __init__(self, token, *, port=8765):
        if (
            type(port) is not int
            or not 1 <= port <= 65535
            or not isinstance(token, str)
            or len(token) < 32
        ):
            raise ValueError("Explicit bridge authorization/port required")
        self._token, self._port = token, port
        self._opener = build_opener(NoRedirect())

    def _request(self, path, body=None):
        if path not in {"health", "symbols", "snapshot", "size", "size-audit"}:
            raise ValueError("Read-only bridge endpoint required")
        request = Request(
            f"http://127.0.0.1:{self._port}/v1/{path}",
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=10) as reply:
                return strict_json(reply.read(2000001).decode(), limit=2000000)
        except (HTTPError, URLError, ValueError, OSError):
            raise RuntimeError("BROKER_BRIDGE_UNAVAILABLE") from None

    def health(self):
        return self._request("health")

    def symbols(self):
        return self._request("symbols")

    def snapshot(self):
        return snapshot_from_dict(self._request("snapshot"))

    def size(self, request, policy):
        return receipt_from_dict(
            self._request("size", {"request": wire(request), "policy": wire(policy)})
        )

    def audit_size(self, request, policy):
        from vision.broker.codec import replay

        value = self._request("size-audit", {"request": wire(request), "policy": wire(policy)})
        replay(value)
        return value
