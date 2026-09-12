"""KhomaVolume selective-ORB signal engine — the server-side "brain".

PROPRIETARY / SERVER-SIDE ONLY. The entry thresholds in this module are the strategy's
edge and MUST NEVER be sent to a chart, a client, or any client-visible payload. The live
engine calls find_signal() with recent 1-minute bars; the caller (data feed + executor)
never needs to know these numbers.

Ported 1:1 from the validated backtest (orb/backtest, 2019-2026 walk-forward, PF 1.66 OOS):
build the 09:30-09:45 ET opening range, then take ONLY a decisive breakout — a bar that
CLOSES beyond the range, with real volume and a decisive body, out of a range that is alive
but not exhausted. Stop/target are POINTS from the ACTUAL FILL (fill-relative), matching the
backtest exactly — the executor arms them on the broker fill, never from the OR level.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

# ---- proprietary thresholds (server-side only) -----------------------------
_OR_START_MIN = 9 * 60 + 30          # 09:30 ET
_OR_MINUTES = 15                     # opening range length -> 09:45 ET
_OR_END_MIN = _OR_START_MIN + _OR_MINUTES
_DEFAULT_CUTOFF_MIN = 11 * 60        # 11:00 ET — no new entries after
_ATR_MIN_FRAC = 0.08                 # OR range must be >= 8% of daily ATR (not dead)
_ATR_MAX_FRAC = 0.75                 # ... and <= 75% (not already exhausted)
_MIN_BODY_FRAC = 0.35                # decisive candle: |c-o| / (h-l)
_VOL_MULT = 1.20                     # breakout bar volume >= 1.2x avg OR per-bar volume


@dataclass
class Bar:
    """One 1-minute bar, ET-aware open time."""
    dt: object
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

    @property
    def minute_of_day(self) -> int:
        return self.dt.hour * 60 + self.dt.minute


@dataclass
class Signal:
    """A confirmed selective breakout. `trigger` is the entry stop level (OR edge + buffer);
    the executor places a stop-limit there and, on the actual fill, arms stop/target as
    fill ∓/± these POINTS (fill-relative — never from the OR level)."""
    side: str                 # "long" | "short"
    trigger: float            # entry stop trigger price
    stop_points: float        # protective stop distance from fill (fill - this, for a long)
    target_points: float      # take-profit distance from fill (fill + this, for a long)
    or_high: float
    or_low: float
    or_range: float
    reason: str = "selective_breakout"


def opening_range(day_bars: List[Bar]):
    """(high, low, total_volume, bar_count) of 09:30-09:45 ET, or None if that window is
    missing from the bars."""
    win = [b for b in day_bars if _OR_START_MIN <= b.minute_of_day < _OR_END_MIN]
    if not win:
        return None
    hi = max(b.h for b in win)
    lo = min(b.l for b in win)
    vol = sum(b.v for b in win)
    return hi, lo, vol, len(win)


def daily_true_range(day_bars: List[Bar], prev_close: Optional[float]) -> Optional[float]:
    """RTH true range for one day — the caller averages these across prior sessions to
    maintain the rolling ATR that find_signal() reads (today never sees itself)."""
    if not day_bars:
        return None
    hi = max(b.h for b in day_bars)
    lo = min(b.l for b in day_bars)
    if prev_close is None:
        return hi - lo
    return max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))


def _breakout_ok(bar: Bar, or_high: float, or_low: float, avg_bar_vol: float, side: str) -> bool:
    """The three per-bar gates that separate a decisive breakout from a fakeout."""
    rng = bar.h - bar.l
    if rng <= 0:
        return False
    # decisive body
    if (abs(bar.c - bar.o) / rng) < _MIN_BODY_FRAC:
        return False
    # must CLOSE beyond the broken level, in the breakout direction (not just a wick)
    if side == "long" and bar.c < or_high:
        return False
    if side == "short" and bar.c > or_low:
        return False
    # participation: breakout volume vs the OR's own average bar volume
    # (auto-skipped when the feed carries no volume, e.g. cash-index bars)
    if avg_bar_vol > 0 and bar.v < _VOL_MULT * avg_bar_vol:
        return False
    return True


def find_signal(day_bars: List[Bar], prev_atr: Optional[float], *,
                stop_points: float, target_points: float, min_or_points: float,
                tick: float = 0.25, buffer_ticks: float = 2.0,
                cutoff_min: int = _DEFAULT_CUTOFF_MIN,
                allow_long: bool = True, allow_short: bool = True) -> Optional[Signal]:
    """Evaluate the day's bars-so-far and return the first CONFIRMED selective breakout, or
    None. Deterministic and side-effect free — safe to call every time a new 1-min bar closes.
    `prev_atr` is the rolling ATR from PRIOR sessions (never today's). `stop_points`,
    `target_points`, `min_or_points` are per-instrument (scaled to price level by the caller)."""
    orr = opening_range(day_bars)
    if orr is None:
        return None
    or_high, or_low, or_vol, or_count = orr
    or_range = or_high - or_low

    # ---- day-level gates: skip a structurally weak day entirely ----
    if or_range < min_or_points:
        return None
    if prev_atr is not None and prev_atr > 0:
        frac = or_range / prev_atr
        if frac < _ATR_MIN_FRAC or frac > _ATR_MAX_FRAC:
            return None

    avg_bar_vol = (or_vol / or_count) if or_count else 0.0
    long_trig = or_high + buffer_ticks * tick
    short_trig = or_low - buffer_ticks * tick

    # ---- first decisive breakout after the OR, before the cutoff, wins ----
    for b in day_bars:
        m = b.minute_of_day
        if m < _OR_END_MIN or m >= cutoff_min:
            continue
        if allow_long and b.h >= long_trig and _breakout_ok(b, or_high, or_low, avg_bar_vol, "long"):
            return Signal("long", long_trig, stop_points, target_points, or_high, or_low, or_range)
        if allow_short and b.l <= short_trig and _breakout_ok(b, or_high, or_low, avg_bar_vol, "short"):
            return Signal("short", short_trig, stop_points, target_points, or_high, or_low, or_range)
    return None
