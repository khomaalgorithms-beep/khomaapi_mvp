"""Selective-ORB signal engine (the server-side brain). Pure logic — no DB, no broker.
Covers: decisive long/short breakouts, the four rejection gates (dead range, exhausted
range, weak volume, wick-only, indecisive body), the 11:00 cutoff, and the volume-less
(cash-index) case. These mirror the validated backtest's entry rules exactly."""
from datetime import datetime
from app.orb_selective import Bar, find_signal, opening_range, daily_true_range

P = dict(stop_points=30, target_points=75, min_or_points=8, tick=0.25, buffer_ticks=2)
ATR = 50.0   # OR range 10 -> frac 0.20, inside [0.08, 0.75]


def mk(hh, mm, o, h, l, c, v=100):
    return Bar(datetime(2026, 1, 2, hh, mm), o, h, l, c, v)


def or_bars(hi=100.0, lo=90.0, v=100):
    # 15 one-minute bars 09:30..09:44 -> OR high=hi, low=lo
    return [mk(9, 30 + i, 95, hi, lo, 95, v) for i in range(15)]


def day(post, hi=100.0, lo=90.0, orv=100):
    return or_bars(hi, lo, orv) + post


def test_opening_range_window():
    orr = opening_range(or_bars())
    assert orr == (100.0, 90.0, 100 * 15, 15)


def test_clean_long_breakout():
    # bar closes above OR high, decisive body, 2x volume -> LONG at trigger 100.5
    sig = find_signal(day([mk(9, 50, 100, 103, 99.5, 102.5, 200)]), ATR, **P)
    assert sig and sig.side == "long"
    assert sig.trigger == 100.5 and sig.stop_points == 30 and sig.target_points == 75
    assert sig.or_high == 100.0 and sig.or_low == 90.0


def test_clean_short_breakout():
    sig = find_signal(day([mk(9, 50, 90, 90.5, 87, 88, 200)]), ATR, **P)
    assert sig and sig.side == "short" and sig.trigger == 89.5


def test_reject_weak_volume():
    # breaks and closes beyond, decisive body, but volume < 1.2x average -> skip
    assert find_signal(day([mk(9, 50, 100, 103, 99.5, 102.5, 60)]), ATR, **P) is None


def test_reject_wick_only():
    # pokes above the level but CLOSES back inside -> not a real breakout
    assert find_signal(day([mk(9, 50, 99, 103, 98.5, 99.0, 300)]), ATR, **P) is None


def test_reject_indecisive_body():
    # closes just beyond but the body is tiny (doji) -> skip
    assert find_signal(day([mk(9, 50, 100, 103, 99.5, 100.2, 300)]), ATR, **P) is None


def test_reject_no_breakout():
    # never reaches the trigger -> no trade today
    post = [mk(9, 50 + i, 95, 99, 91, 95, 300) for i in range(5)]
    assert find_signal(day(post), ATR, **P) is None


def test_reject_dead_range():
    # OR range 1 pt < min_or_points 8 -> structurally dead, skip before any breakout
    d = day([mk(9, 50, 100, 103, 99.5, 102.5, 300)], hi=100.0, lo=99.0)
    assert find_signal(d, ATR, **P) is None


def test_reject_exhausted_range():
    # OR range 10 with ATR 12 -> frac 0.83 > 0.75 (already exhausted) -> skip
    assert find_signal(day([mk(9, 50, 100, 103, 99.5, 102.5, 300)]), 12.0, **P) is None


def test_ignore_after_cutoff():
    # a valid breakout AFTER 11:00 ET is ignored (no chasing late)
    assert find_signal(day([mk(11, 30, 100, 103, 99.5, 102.5, 300)]), ATR, **P) is None


def test_volumeless_index_feed_skips_volume_gate():
    # cash-index bars carry no volume -> volume gate auto-disabled; body+close still required
    sig = find_signal(day([mk(9, 50, 100, 103, 99.5, 102.5, 0)], orv=0), ATR, **P)
    assert sig and sig.side == "long"


def test_first_qualifying_bar_wins_after_a_reject():
    # an early wick fails, a later decisive bar qualifies -> takes the later one
    post = [mk(9, 50, 99, 101, 98, 99, 300),          # wick above, closes inside -> reject
            mk(9, 55, 100, 104, 100, 103.5, 300)]     # decisive close beyond -> LONG
    sig = find_signal(day(post), ATR, **P)
    assert sig and sig.side == "long"


def test_daily_true_range():
    assert daily_true_range(or_bars(hi=100, lo=90), prev_close=None) == 10
    assert daily_true_range(or_bars(hi=100, lo=90), prev_close=85) == 15
