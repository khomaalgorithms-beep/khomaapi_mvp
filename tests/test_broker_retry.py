"""Reliability tests for the Tradovate REST helpers (_get / _post).

The production copy-trading path funnels every broker call through these two
functions. Before this, any transient failure was swallowed to None -> an opaque
"request failed" with no retry, dropping ~half of live orders.

The dangerous part is retrying ORDER placement: if the request was already sent,
a retry could place a DUPLICATE order. So the contract we lock in here:
  * _get (reads) retry freely (idempotent).
  * _post retries ONLY connection-phase failures (never reached the broker).
  * _post NEVER retries a ReadTimeout (sent, no response) -> duplicate-order safety.
"""
from app import tradovate_oauth as tvo

REQ = tvo.requests.exceptions


class FakeResp:
    def __init__(self, data=None, status=200, text="", raise_json=False):
        self._data = data; self.status_code = status; self.text = text; self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._data


def test_get_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(tvo, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(tvo, "_RETRY_ATTEMPTS", 3)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise REQ.ConnectionError("blip")
        return FakeResp({"ok": True})

    monkeypatch.setattr(tvo.requests, "get", fake_get)
    assert tvo._get("demo", "t", "/positions") == {"ok": True}
    assert calls["n"] == 2


def test_get_returns_none_after_exhausting(monkeypatch):
    monkeypatch.setattr(tvo, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(tvo, "_RETRY_ATTEMPTS", 3)
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        raise REQ.ConnectionError("down")

    monkeypatch.setattr(tvo.requests, "get", fake_get)
    assert tvo._get("demo", "t", "/positions") is None
    assert calls["n"] == 3


def test_post_retries_connect_phase_then_succeeds(monkeypatch):
    monkeypatch.setattr(tvo, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(tvo, "_RETRY_ATTEMPTS", 3)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise REQ.ConnectTimeout("slow connect")   # never reached broker -> safe to retry
        return FakeResp({"orderId": 123})

    monkeypatch.setattr(tvo.requests, "post", fake_post)
    assert tvo._post("demo", "t", "/order/placeorder", {}) == {"orderId": 123}
    assert calls["n"] == 2


def test_post_never_retries_read_timeout(monkeypatch):
    """CRITICAL: request was sent, no response -> order status unknown -> exactly ONE
    attempt, distinct error. Retrying here could place a duplicate order."""
    monkeypatch.setattr(tvo, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(tvo, "_RETRY_ATTEMPTS", 3)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        raise REQ.ReadTimeout("no response")

    monkeypatch.setattr(tvo.requests, "post", fake_post)
    out = tvo._post("demo", "t", "/order/placeorder", {})
    assert calls["n"] == 1                       # <-- the safety guarantee
    assert out["error"] == "timeout after send"


def test_post_connect_error_exhausts_to_error_dict(monkeypatch):
    monkeypatch.setattr(tvo, "_RETRY_BACKOFF", 0)
    monkeypatch.setattr(tvo, "_RETRY_ATTEMPTS", 3)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        raise REQ.ConnectionError("broker unreachable")

    monkeypatch.setattr(tvo.requests, "post", fake_post)
    out = tvo._post("demo", "t", "/order/placeorder", {})
    assert calls["n"] == 3
    assert out["error"] == "connect failed"


def test_post_success_returns_json(monkeypatch):
    monkeypatch.setattr(tvo.requests, "post", lambda *a, **k: FakeResp({"orderId": 9}))
    assert tvo._post("demo", "t", "/order/placeorder", {}) == {"orderId": 9}


def test_post_non_json_response_surfaces_status_once(monkeypatch):
    monkeypatch.setattr(tvo, "_RETRY_BACKOFF", 0)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return FakeResp(status=502, text="bad gateway", raise_json=True)

    monkeypatch.setattr(tvo.requests, "post", fake_post)
    out = tvo._post("demo", "t", "/order/placeorder", {})
    assert calls["n"] == 1                       # a received response is never retried
    assert out["error"].startswith("http 502")


def test_post_error_dict_is_treated_as_failure_by_order_ok():
    """The structured errors must register as NOT-ok so the caller reports them."""
    from app import main as appmod
    assert appmod._order_ok({"error": "connect failed"}) is False
    assert appmod._order_ok({"error": "timeout after send"}) is False
    assert appmod._order_ok({"orderId": 1}) is True
