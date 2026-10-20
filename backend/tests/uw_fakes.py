"""Network-free stand-ins for Unusual Whales HTTP responses.

`FakeUWSession` replaces `requests.Session` inside a real `UWRestClient`, so
the REAL adapter code (status handling, retries, normalization) runs while
every response comes from an in-memory route table. Routes are matched by
substring of the request URL; a route value may be a FakeResponse, a list of
FakeResponses (served in order, last one repeats), or a callable
`(url, params) -> FakeResponse`. Unmatched URLs return 404 so a missing route
is loud rather than silently empty.
"""
from __future__ import annotations

import json

import requests

from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient


class FakeResponse:
    def __init__(self, status_code: int = 200, body=None, raw_text: str | None = None, headers=None):
        self.status_code = status_code
        self._body = body
        self._raw_text = raw_text
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        return self._raw_text if self._raw_text is not None else json.dumps(self._body)

    def json(self):
        if self._raw_text is not None:
            return json.loads(self._raw_text)  # raises ValueError for malformed bodies, like requests
        return self._body


def ok(data) -> FakeResponse:
    return FakeResponse(200, {"data": data})


class Timeout:
    """Route value that raises a requests timeout instead of responding."""


class FakeUWSession:
    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                if callable(value) and not isinstance(value, FakeResponse):
                    value = value(url, dict(params or {}))
                if value is Timeout or isinstance(value, Timeout):
                    raise requests.exceptions.ReadTimeout(f"simulated timeout for {url}")
                return value
        return FakeResponse(404, {"error": f"no fake route for {url}"})


def fake_live_adapter(routes: dict, watchlist=None, max_retries: int = 0) -> LiveUWAdapter:
    session = FakeUWSession(routes)
    client = UWRestClient(
        "fake-test-key", base_url="http://127.0.0.1:9", session=session,
        max_retries=max_retries, sleep_fn=lambda _s: None, circuit_breaker_threshold=1000,
    )
    adapter = LiveUWAdapter("fake-test-key", client=client, watchlist=watchlist or [])
    adapter.fake_session = session
    return adapter
