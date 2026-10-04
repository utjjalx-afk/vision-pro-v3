"""Authenticated loopback-only calculation API; MT5 access is serialized."""

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from vision.analysis.contracts import wire
from vision.broker.codec import dto
from vision.broker.models import SizeRequest, SizingPolicy
from vision.broker.mt5_bridge import MT5Reader
from vision.strategies.dsl import strict_json


def make_server(reader, token, *, port=8765):
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 256
        or not token.isascii()
        or any(c.isspace() for c in token)
    ):
        raise ValueError("Strong runtime bridge bearer token required")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid loopback port")

    class Handler(BaseHTTPRequestHandler):
        server_version = "VisionBroker/phase11-v1"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass  # No access logs, bodies, account identities or bearer tokens.

        def response(self, status, value):
            data = json.dumps(value, allow_nan=False, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def authorized(self):
            values = self.headers.get_all("Authorization", [])
            if len(values) != 1 or not hmac.compare_digest(values[0], "Bearer " + token):
                self.response(401, {"error": "UNAUTHORIZED"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            try:
                if self.path == "/v1/health":
                    result = reader.health()
                elif self.path == "/v1/symbols":
                    result = reader.discovery()
                elif self.path == "/v1/snapshot":
                    result = wire(reader.snapshot())
                else:
                    self.response(404, {"error": "ENDPOINT_UNAVAILABLE"})
                    return
                self.response(200, result)
            except (RuntimeError, ValueError, TypeError, KeyError, OSError):
                self.response(503, {"error": "BROKER_TRUTH_UNAVAILABLE"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path not in {"/v1/size", "/v1/size-audit"}:
                self.response(404, {"error": "ENDPOINT_UNAVAILABLE"})
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Explicit body framing required")
                length = int(lengths[0])
                if not 1 <= length <= 100000:
                    raise ValueError("Body bound exceeded")
                value = strict_json(self.rfile.read(length).decode())
                if type(value) is not dict or set(value) != {"request", "policy"}:
                    raise ValueError("Exact sizing request envelope required")
                method = reader.audit_size if self.path == "/v1/size-audit" else reader.size
                result = method(
                    dto(SizeRequest, value["request"]), dto(SizingPolicy, value["policy"])
                )
                self.response(200, result if self.path == "/v1/size-audit" else result.to_dict())
            except RuntimeError:
                self.response(503, {"error": "BROKER_TRUTH_UNAVAILABLE"})
            except (ValueError, TypeError, KeyError, OSError, OverflowError):
                self.response(400, {"error": "INVALID_SIZING_REQUEST"})

    class SilentServer(HTTPServer):
        def handle_error(self, *_):
            pass  # Never print socket/native exception text or request data.

    return SilentServer(("127.0.0.1", port), Handler)


def main():
    from pathlib import Path

    from vision.config import load_settings

    parser = argparse.ArgumentParser(description="Windows demo-only MT5 calculation bridge")
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--terminal-path", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        load_settings(os.environ)
        token = os.environ.get("MT5_BRIDGE_TOKEN", "")
        with Path(args.mapping).open("rb") as handle:
            mapping = strict_json(handle.read(100001).decode())
        reader = MT5Reader.connect(mapping, token.encode(), terminal_path=args.terminal_path)
        server = make_server(reader, token, port=args.port)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, ImportError, RuntimeError):
        parser.exit(2, "MT5 bridge BLOCKED: unavailable demo terminal or invalid configuration\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
