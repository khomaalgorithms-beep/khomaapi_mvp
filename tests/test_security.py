"""Unit tests for edge security: IP resolution, rate limiter, same-origin."""

from app import security as sec


class FakeReq:
    def __init__(self, headers=None, client_host="9.9.9.9"):
        self.headers = headers or {}

        class C:
            host = client_host
        self.client = C()


def test_client_ip_prefers_cloudflare():
    r = FakeReq({"cf-connecting-ip": "1.2.3.4",
                 "x-forwarded-for": "5.6.7.8, 9.9.9.9"})
    assert sec.client_ip(r) == "1.2.3.4"


def test_client_ip_falls_back_to_xff_then_peer():
    assert sec.client_ip(FakeReq({"x-forwarded-for": "5.6.7.8, 9.9.9.9"})) == "5.6.7.8"
    assert sec.client_ip(FakeReq({})) == "9.9.9.9"


def test_rate_limiter_blocks_over_limit():
    rl = sec.RateLimiter()
    t = 1000.0
    for i in range(5):
        allowed, _ = rl.hit("k", limit=5, window=60, now=t + i)
        assert allowed
    blocked, retry = rl.hit("k", limit=5, window=60, now=t + 5)
    assert blocked is False and retry >= 1


def test_rate_limiter_window_resets():
    rl = sec.RateLimiter()
    assert rl.hit("k", 2, 60, now=0)[0]
    assert rl.hit("k", 2, 60, now=1)[0]
    assert rl.hit("k", 2, 60, now=2)[0] is False    # 3rd within window blocked
    assert rl.hit("k", 2, 60, now=120)[0] is True   # window elapsed → allowed


def test_rate_limiter_count_add_clear():
    rl = sec.RateLimiter()
    rl.add("f", now=0)
    rl.add("f", now=1)
    assert rl.count("f", 900, now=2) == 2
    rl.clear("f")
    assert rl.count("f", 900, now=2) == 0


def test_same_origin_matches_and_rejects():
    host = "app.khomaapi.com"
    assert sec.is_same_origin(FakeReq({"host": host, "origin": f"https://{host}"})) is True
    assert sec.is_same_origin(FakeReq({"host": host, "referer": f"https://{host}/login"})) is True
    assert sec.is_same_origin(FakeReq({"host": host, "origin": "https://evil.com"})) is False
    # No Origin/Referer → allowed (SameSite=lax is the primary defense)
    assert sec.is_same_origin(FakeReq({"host": host})) is True


def test_security_headers_present():
    h = sec.SECURITY_HEADERS
    assert "Content-Security-Policy" in h
    assert h["X-Frame-Options"] == "DENY"
    assert "max-age" in h["Strict-Transport-Security"]
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
