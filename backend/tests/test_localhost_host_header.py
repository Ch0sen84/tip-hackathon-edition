"""QA remediation D2: localhost:8000 returned 400 while 127.0.0.1:8000
worked, because TrustedHostMiddleware's default allowed-hosts list only
ever contained whichever single literal value BIND_HOST/ALLOWED_HOST
resolved to (127.0.0.1 by default) -- never the "localhost" spelling.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_localhost_host_header_accepted_by_default(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/health", headers={"Host": "localhost:8000"})
    assert resp.status_code == 200


def test_127_0_0_1_host_header_still_accepted_by_default(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/health", headers={"Host": "127.0.0.1:8000"})
    assert resp.status_code == 200


def test_foreign_host_header_still_rejected(app_main):
    """D2's widening must never become "trust any Host header" -- a
    genuinely foreign hostname must still 400, exactly as before."""
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
