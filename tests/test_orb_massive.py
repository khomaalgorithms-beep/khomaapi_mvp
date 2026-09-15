"""Pure Massive-feed logic — no network. AM->Bar parsing (UTC ms -> ET), frame parsing, status
detection, ticker/subscription mapping, and the dispatch that routes a ticker to its exec root."""
from datetime import date, datetime, timezone

from app.orb_massive import (am_to_bar, parse_frames, status_of, front_ticker,
                            subscriptions_for, MassiveFeed, fetch_daily_ranges,
                            fetch_today_bars)
from app.orb_selective import Bar


def _am(sym="NQZ6", o=20000, h=20010, l=19990, c=20005, v=1234, start_ms=1789045800000):
    return {"ev": "AM", "sym": sym, "o": o, "h": h, "l": l, "c": c, "v": v,
            "s": start_ms, "e": start_ms + 60000}


def test_am_to_bar_ohlcv_and_et_time():
    b = am_to_bar(_am())
    assert (b.o, b.h, b.l, b.c, b.v) == (20000.0, 20010.0, 19990.0, 20005.0, 1234.0)
    # 1789045800000 ms = 2026-09-10 13:10 UTC -> 09:10 ET (EDT) -> minute_of_day 550
    assert b.dt.tzinfo is not None and b.minute_of_day == 9 * 60 + 10


def test_am_to_bar_rejects_non_am():
    assert am_to_bar({"ev": "status", "status": "connected"}) is None
    assert am_to_bar({"ev": "AM", "sym": "NQZ6"}) is None      # missing timestamp


def test_parse_frames_array_and_status():
    frames = parse_frames('[{"ev":"status","status":"auth_success"},{"ev":"AM","sym":"NQZ6"}]')
    assert len(frames) == 2
    assert status_of(frames) == "auth_success"
    assert parse_frames("not json") == []
    assert status_of([{"ev": "AM"}]) is None


def test_front_ticker_quarterly_roll():
    assert front_ticker("NQ", date(2026, 9, 14)) == "NQZ6"     # past Sep roll -> Dec
    assert front_ticker("NQ", date(2026, 6, 1)) == "NQM6"      # June
    assert front_ticker("RTY", date(2026, 1, 5)) == "RTYH6"    # March


def test_subscriptions_map_full_data_to_micro_execution():
    # signal computed on full NQ/RTY data; executed on micro MNQ/M2K
    subs = subscriptions_for(["MNQ", "M2K"], today=date(2026, 9, 14))
    assert subs == {"NQZ6": "MNQ", "RTYZ6": "M2K"}


def test_dispatch_routes_ticker_to_root_and_calls_on_bar():
    seen = []
    feed = MassiveFeed(get_key=lambda: "k", subscriptions={"NQZ6": "MNQ", "RTYZ6": "M2K"},
                       on_bar=lambda root, bar: seen.append((root, bar.c)))
    feed._dispatch([_am(sym="NQZ6", c=20005), _am(sym="RTYZ6", c=2300),
                    _am(sym="ESZ6", c=5000),                 # not subscribed -> ignored
                    {"ev": "status", "status": "connected"}])  # non-AM -> ignored
    assert seen == [("MNQ", 20005.0), ("M2K", 2300.0)]


def test_dispatch_survives_bad_callback():
    def boom(root, bar):
        raise RuntimeError("x")
    feed = MassiveFeed(get_key=lambda: "k", subscriptions={"NQZ6": "MNQ"}, on_bar=boom)
    feed._dispatch([_am()])                                    # must not raise


# ---- REST backfill / ATR-seed fetchers (network monkeypatched) ----
class _Resp:
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return self._p


def _patch_requests_get(monkeypatch, payload, captured):
    import requests
    def fake_get(url, params=None, timeout=None):
        captured["url"] = url; captured["params"] = params or {}
        return _Resp(payload)
    monkeypatch.setattr(requests, "get", fake_get)


def test_fetch_daily_ranges_computes_true_range_excluding_today(monkeypatch):
    now = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)     # "today" = 2026-09-15
    payload = {"results": [
        {"high": 100, "low": 90, "close": 95, "session_end_date": "2026-09-11"},
        {"high": 108, "low": 96, "close": 104, "session_end_date": "2026-09-12"},
        {"high": 120, "low": 110, "close": 118, "session_end_date": "2026-09-15"},  # today -> excluded
    ]}
    cap = {}
    _patch_requests_get(monkeypatch, payload, cap)
    ranges = fetch_daily_ranges("KEY", "MNQ", now=now)
    # day1 TR = 100-90 = 10; day2 TR = max(108-96, |108-95|, |96-95|) = 13; today dropped
    assert ranges == [10.0, 13.0]
    assert cap["params"]["resolution"] == "1day"


def test_fetch_daily_ranges_empty_on_error(monkeypatch):
    import requests
    def boom(*a, **k):
        raise RuntimeError("net down")
    monkeypatch.setattr(requests, "get", boom)
    assert fetch_daily_ranges("KEY", "MNQ") == []


def test_fetch_today_bars_parses_ns_and_sorts_ascending(monkeypatch):
    now = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
    base = int(datetime(2026, 9, 15, 13, 32, tzinfo=timezone.utc).timestamp() * 1e9)  # ns
    payload = {"results": [                                     # DESCENDING as Massive returns
        {"window_start": base + 60_000_000_000, "open": 2, "high": 3, "low": 1, "close": 2, "volume": 5},
        {"window_start": base, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 4},
    ]}
    cap = {}
    _patch_requests_get(monkeypatch, payload, cap)
    bars = fetch_today_bars("KEY", "MNQ", now=now)
    assert [b.c for b in bars] == [1.0, 2.0]                    # sorted ascending by time
    assert cap["params"]["resolution"] == "1min"
