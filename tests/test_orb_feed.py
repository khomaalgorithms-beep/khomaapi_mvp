"""Pure market-data bar logic for the live feed — no network. Covers trade->bar aggregation,
broker chart-stream completion-by-supersession, Tradovate WS frame parsing, and chart-packet
mapping (incl. UTC->ET conversion and up/down volume summing)."""
from datetime import datetime, timezone

from app.orb_feed import (MinuteAggregator, BarStreamTracker, parse_frames,
                          chart_bars, parse_iso, _to_et)
from app.orb_selective import Bar


def utc(h, m, s=0):
    return datetime(2026, 1, 2, h, m, s, tzinfo=timezone.utc)


# ---- MinuteAggregator ----
def test_aggregator_builds_ohlcv_and_emits_on_boundary():
    agg = MinuteAggregator()
    # three prints in the same minute
    assert agg.add(utc(14, 30, 1), 100.0, 5) is None
    assert agg.add(utc(14, 30, 30), 103.0, 2) is None   # new high
    assert agg.add(utc(14, 30, 59), 99.0, 3) is None    # new low, last
    # a print in the NEXT minute closes the prior bar
    done = agg.add(utc(14, 31, 2), 101.0, 1)
    assert done is not None
    assert (done.o, done.h, done.l, done.c, done.v) == (100.0, 103.0, 99.0, 99.0, 10)


def test_aggregator_ignores_out_of_order_print():
    agg = MinuteAggregator()
    agg.add(utc(14, 31, 5), 100.0, 1)
    # a late print stamped in a PRIOR minute must not rewrite the current bar or emit
    assert agg.add(utc(14, 30, 59), 999.0, 1) is None
    b = agg.flush()
    assert b.h == 100.0 and b.o == 100.0


def test_aggregator_flush_closes_partial_bar():
    agg = MinuteAggregator()
    agg.add(utc(15, 55, 10), 50.0, 4)
    b = agg.flush()
    assert b and b.c == 50.0 and b.v == 4
    assert agg.flush() is None   # nothing left


# ---- BarStreamTracker ----
def test_tracker_emits_previous_bar_when_new_minute_starts():
    tr = BarStreamTracker()
    b930 = Bar(_to_et(utc(14, 30)), 1, 2, 0.5, 1.5, 10)
    # forming-bar updates for the same minute never complete
    assert tr.update("NQ", b930) is None
    assert tr.update("NQ", Bar(_to_et(utc(14, 30)), 1, 3, 0.5, 2.0, 20)) is None
    # a bar for the next minute completes the (updated) 09:30 bar
    done = tr.update("NQ", Bar(_to_et(utc(14, 31)), 2, 2, 2, 2, 1))
    assert done and done.h == 3 and done.v == 20


def test_tracker_is_per_contract():
    tr = BarStreamTracker()
    tr.update("NQ", Bar(_to_et(utc(14, 30)), 1, 1, 1, 1, 1))
    tr.update("RTY", Bar(_to_et(utc(14, 30)), 9, 9, 9, 9, 1))
    # advancing NQ must not complete RTY's bar
    assert tr.update("NQ", Bar(_to_et(utc(14, 31)), 2, 2, 2, 2, 1)) is not None
    assert tr.forming("RTY").o == 9


def test_tracker_update_many_backfill():
    tr = BarStreamTracker()
    bars = [Bar(_to_et(utc(14, 30 + i)), i, i, i, i, i) for i in range(5)]
    done = tr.update_many("NQ", bars)
    assert len(done) == 4                 # all but the last (still forming)
    assert tr.forming("NQ").dt == bars[-1].dt


# ---- frame parsing ----
def test_parse_frames_kinds():
    assert parse_frames("o") == [("open", None)]
    assert parse_frames("h") == [("heartbeat", None)]
    assert parse_frames("c") == [("close", None)]
    assert parse_frames("") == []
    kind, payload = parse_frames('a[{"e":"chart"}]')[0]
    assert kind == "data" and payload == [{"e": "chart"}]


def test_parse_frames_bad_json_is_dropped():
    assert parse_frames("a[not json") == []


# ---- chart packet mapping ----
def test_chart_bars_maps_ohlcv_and_sums_volume():
    msg = {"e": "chart", "d": {"charts": [{"id": 7, "bars": [
        {"timestamp": "2026-01-02T14:30:00.000Z", "open": 100, "high": 104,
         "low": 99, "close": 103, "upVolume": 30, "downVolume": 12},
    ]}]}}
    out = chart_bars(msg)
    assert set(out) == {7}
    b = out[7][0]
    assert (b.o, b.h, b.l, b.c, b.v) == (100.0, 104.0, 99.0, 103.0, 42.0)
    # UTC 14:30 -> ET 09:30 (EST, winter) -> minute_of_day 570 (the opening-range start)
    assert b.minute_of_day == 9 * 60 + 30


def test_chart_bars_ignores_non_chart_and_empty():
    assert chart_bars({"e": "md"}) == {}
    assert chart_bars({"e": "chart", "d": {"charts": []}}) == {}


def test_parse_iso_handles_z_suffix():
    dt = parse_iso("2026-01-02T14:30:00Z")
    assert dt.tzinfo is not None and dt.hour == 14
