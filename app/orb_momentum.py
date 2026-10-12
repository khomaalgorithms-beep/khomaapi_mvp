"""KhomaVolume NQ — the validated live signal (server-side only).

Rule (validated 2008-2026 on FirstRate 1-min data, pre-registered holdout 2024-26, max-level stress battery;
see lucid_research/RESULTS_2026-10-11.md):
  * At 10:30 ET, take the direction of the move from the 09:30 open to the 10:29 bar's close
    (i.e. decided the moment the 10:29 bar completes; entry at market at ~10:30:00).
  * Only if that direction AGREES with the 20-day trend (prior session close vs the mean of the last
    20 session closes). Otherwise sit out — one decision per day.
  * Protective stop = STOP_ATR x ATR14, where ATR14 = mean of the prior 14 sessions' RTH (09:30-16:00) ranges.
  * Hold to the close: flat at 15:59 ET. (Eval accounts may also carry a take-profit, set by the executor.)
STOP_ATR is the adaptive rule's current choice (expanding-window quarterly re-fit; 0.2 since 2020-04).

Everything here is pure + deterministic so it is unit-tested without a broker or a socket.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, timedelta as _td
from typing import List, Optional, Sequence

STOP_ATR = 0.2            # stop distance as a fraction of ATR14 (adaptive rule, current setting)
ATR_DAYS = 14
TREND_DAYS = 20
OPEN_MIN = 9 * 60 + 30    # 09:30 ET
DECIDE_MIN = 10 * 60 + 29  # the 10:29 bar's CLOSE decides (entry ~10:30:00)
LAST_DECIDE_MIN = 10 * 60 + 40   # if the 10:29 bar is missing, a bar up to 10:39 may stand in; later = no trade
RTH_END_MIN = 16 * 60


@dataclass
class DayContext:
    """Prior-sessions context for one trading date (never includes today)."""
    date: str
    atr: Optional[float]     # mean RTH range of the prior ATR_DAYS sessions (points)
    trend: int               # +1 up / -1 down / 0 unknown
    pdh: Optional[float] = None   # prior session's RTH high (ORB filter)
    pdl: Optional[float] = None   # prior session's RTH low

    @property
    def ok(self) -> bool:
        return bool(self.atr) and self.atr > 0 and self.trend in (1, -1)


@dataclass
class MomentumSignal:
    side: str                # "long" | "short"
    stop_points: float
    ref_price: float         # the deciding bar's close (≈ the market fill)
    move: float              # 10:29 close - 09:30 open (points)
    open_price: float


def context_from(rth_ranges: Sequence[float], closes: Sequence[float], date: str, levels=None) -> DayContext:
    """Build today's context from PRIOR sessions only: rth_ranges / closes oldest-first, excluding today."""
    rr = [float(x) for x in rth_ranges if x and x > 0]
    atr = sum(rr[-ATR_DAYS:]) / len(rr[-ATR_DAYS:]) if len(rr) >= ATR_DAYS else None
    cl = [float(x) for x in closes if x]
    trend = 0
    if len(cl) >= TREND_DAYS:
        sma = sum(cl[-TREND_DAYS:]) / TREND_DAYS
        trend = 1 if cl[-1] > sma else (-1 if cl[-1] < sma else 0)
    pdh, pdl = (levels if levels else (None, None))
    return DayContext(date=date, atr=atr, trend=trend, pdh=pdh, pdl=pdl)


def _easter(y: int):
    a = y % 19; b = y // 100; c = y % 100; d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451; mo = (h + l - 7 * m + 114) // 31; dy = ((h + l - 7 * m + 114) % 31) + 1
    return _date(y, mo, dy)


def _nth_weekday(y, mo, wd, n):
    d = _date(y, mo, 1); off = (wd - d.weekday()) % 7
    return d + _td(days=off + 7 * (n - 1))


def _last_weekday(y, mo, wd):
    d = _date(y, mo + 1, 1) - _td(days=1) if mo < 12 else _date(y, 12, 31)
    return d - _td(days=(d.weekday() - wd) % 7)


def _observed(d):
    return d - _td(days=1) if d.weekday() == 5 else (d + _td(days=1) if d.weekday() == 6 else d)


def short_session(date_str: str) -> bool:
    """US equity-index futures holiday or early-close day (the research excludes these sessions)."""
    try:
        y, mo, dy = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]); d = _date(y, mo, dy)
    except Exception:
        return False
    hol = {_observed(_date(y, 1, 1)), _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3), _easter(y) - _td(days=2),
           _last_weekday(y, 5, 0), _observed(_date(y, 7, 4)), _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4),
           _observed(_date(y, 12, 25))}
    if y >= 2022:
        hol.add(_observed(_date(y, 6, 19)))
    half = {_nth_weekday(y, 11, 3, 4) + _td(days=1), _date(y, 12, 24), _date(y, 7, 3)}
    return d in hol or d in half


def _minute(b) -> int:
    return b.dt.hour * 60 + b.dt.minute


def decision(day_bars: List, ctx: Optional[DayContext]):
    """Evaluate today's bars-so-far. Returns (status, signal|None):
       ("wait", None)    - the 10:29 bar has not completed yet
       ("signal", sig)   - trade now
       ("skip", reason)  - no trade today (reason: no_open / no_context / flat / against_trend / late)"""
    bars = sorted(day_bars, key=lambda b: b.dt)
    if bars and short_session(bars[0].dt.strftime("%Y-%m-%d")):
        return "skip", "holiday_or_half_day"
    deciding = next((b for b in bars if DECIDE_MIN <= _minute(b) < LAST_DECIDE_MIN), None)
    if deciding is None:
        if bars and _minute(bars[-1]) >= LAST_DECIDE_MIN:
            return "skip", "late"
        return "wait", None
    opening = next((b for b in bars if OPEN_MIN <= _minute(b) < OPEN_MIN + 10), None)
    if opening is None:
        return "skip", "no_open"
    if ctx is None or not ctx.ok:
        return "skip", "no_context"
    move = deciding.c - opening.o
    side = 1 if move > 0 else (-1 if move < 0 else 0)
    if side == 0:
        return "skip", "flat"
    if side != ctx.trend:
        return "skip", "against_trend"
    return "signal", MomentumSignal(side="long" if side > 0 else "short",
                                    stop_points=round(STOP_ATR * ctx.atr, 2),
                                    ref_price=float(deciding.c), move=round(move, 2),
                                    open_price=float(opening.o))


def micros_for_risk(risk_usd: float, stop_points: float, dollars_per_point: float = 2.0,
                    max_micros: int = 40) -> int:
    """Contracts so that a stop-out costs ~risk_usd (floor), allowing 1 micro when one micro's risk is
    within 1.5x the budget (matches the validated sizing). Capped at the account's max."""
    per = stop_points * dollars_per_point
    if per <= 0 or risk_usd <= 0:
        return 0
    q = int(risk_usd // per)
    if q == 0 and per <= 1.5 * risk_usd:
        q = 1
    return max(0, min(q, int(max_micros)))


# ---------------------------------------------------------------------------------------------------------------
# ORB + prior-day break (validated 2008-2026, lucid_research/ORB_PREREGISTRATION.md, close-entry variant):
#   * opening range = 09:30-09:34 bars (high/low of the first 5 minutes)
#   * from the 09:35 bar until the 10:34 bar: the FIRST 1-min bar that CLOSES beyond the range decides the side
#   * trade it only if that close is also beyond the prior session's RTH high (long) / low (short); else no trade
#   * stop = STOP_ATR x ATR14; stop moves to breakeven once the trade is +1R (judged on completed bars); flat 15:59
# ---------------------------------------------------------------------------------------------------------------
ORB_END_MIN = OPEN_MIN + 5            # 09:35 — first bar that can break the range
ORB_LAST_MIN = ORB_END_MIN + 60       # entries only on closes of 09:35 .. 10:34 bars
ORB_BE_R = 1.0


def orb_decision(day_bars: List, ctx: Optional[DayContext]):
    """("wait", None) | ("signal", MomentumSignal) | ("skip", reason). Pure; same semantics as the research rule."""
    bars = sorted(day_bars, key=lambda b: b.dt)
    if bars and short_session(bars[0].dt.strftime("%Y-%m-%d")):
        return "skip", "holiday_or_half_day"
    rng = [b for b in bars if OPEN_MIN <= _minute(b) < ORB_END_MIN]
    after = [b for b in bars if ORB_END_MIN <= _minute(b) < ORB_LAST_MIN]
    if not rng:
        if bars and _minute(bars[-1]) >= ORB_END_MIN:
            return "skip", "no_open"
        return "wait", None
    if not after:
        if bars and _minute(bars[-1]) >= ORB_LAST_MIN:
            return "skip", "late"
        return "wait", None
    hi = max(b.h for b in rng); lo = min(b.l for b in rng)
    for b in after:
        side = 1 if b.c > hi else (-1 if b.c < lo else 0)
        if side == 0:
            continue
        if ctx is None or not ctx.atr or ctx.atr <= 0 or ctx.pdh is None or ctx.pdl is None:
            return "skip", "no_context"
        if (side > 0 and not b.c > ctx.pdh) or (side < 0 and not b.c < ctx.pdl):
            return "skip", "not_beyond_prior_day"
        return "signal", MomentumSignal(side="long" if side > 0 else "short", stop_points=round(STOP_ATR * ctx.atr, 2),
                                        ref_price=float(b.c), move=round(b.c - (hi if side > 0 else lo), 2), open_price=float(rng[0].o))
    if _minute(after[-1]) >= ORB_LAST_MIN - 1:
        return "skip", "no_break"
    return "wait", None


def orb_levels(day_bars: List, ctx: Optional[DayContext], tick: float = 0.25):
    """At the end of the 09:30-09:34 range: (hi, lo, valid_long, valid_short) for the RESTING-order ORB, or None.
    A side is valid only if its entry price (1 tick beyond the range) is also beyond the prior-day high/low —
    the same condition the research applies to the breakout fill."""
    rng = [b for b in day_bars if OPEN_MIN <= _minute(b) < ORB_END_MIN]
    if not rng or ctx is None or not ctx.atr or ctx.pdh is None or ctx.pdl is None:
        return None
    hi = max(b.h for b in rng); lo = min(b.l for b in rng)
    return hi, lo, (hi + tick) > ctx.pdh, (lo - tick) < ctx.pdl
