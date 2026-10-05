"""Offline smoke probe run inside the newly built Command Center container."""

import json
import time
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

opener = build_opener(HTTPCookieProcessor(CookieJar()))
base = "http://127.0.0.1:8787"
for _attempt in range(30):
    try:
        with opener.open(base, timeout=2) as reply:
            assert b"assets/" in reply.read()
        break
    except URLError:
        time.sleep(0.2)
else:
    raise RuntimeError("Dashboard did not become ready")
try:
    opener.open(base + "/api/dashboard/summary")
except HTTPError as error:
    assert error.code == 401
else:
    raise RuntimeError("Unauthenticated API was exposed")
token = Path("/tmp/viewer.token").read_text().strip()
login = Request(
    base + "/auth/session",
    data=json.dumps({"token": token}).encode(),
    headers={"Origin": base, "Content-Type": "application/json"},
)
with opener.open(login) as reply:
    assert json.load(reply)["role"] == "VIEWER"
with opener.open(base + "/api/dashboard/summary") as reply:
    data = json.load(reply)
assert data["live_enabled"] is False
assert data["execution"]["state"] == "DISARMED"
assert data["market"]["health"] == "UNAVAILABLE"
assert data["portfolio"]["account"] is None
print("Offline dashboard HTML/auth/provenance/execution smoke passed")
