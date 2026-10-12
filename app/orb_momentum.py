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


def context_from(rth_ranges: Sequence[float], closes: Sequence[float], date: str) -> DayContext:
    """Build today's context from PRIOR sessions only: rth_ranges / closes oldest-first, excluding today."""
    rr = [float(x) for x in rth_ranges if x and x > 0]
    atr = sum(rr[-ATR_DAYS:]) / len(rr[-ATR_DAYS:]) if len(rr) >= ATR_DAYS else None
    cl = [float(x) for x in closes if x]
    trend = 0
    if len(cl) >= TREND_DAYS:
        sma = sum(cl[-TREND_DAYS:]) / TREND_DAYS
        trend = 1 if cl[-1] > sma else (-1 if cl[-1] < sma else 0)
    return DayContext(date=date, atr=atr, trend=trend)


def _minute(b) -> int:
    return b.dt.hour * 60 + b.dt.minute


def decision(day_bars: List, ctx: Optional[DayContext]):
    """Evaluate today's bars-so-far. Returns (status, signal|None):
       ("wait", None)    - the 10:29 bar has not completed yet
       ("signal", sig)   - trade now
       ("skip", reason)  - no trade today (reason: no_open / no_context / flat / against_trend / late)"""
    bars = sorted(day_bars, key=lambda b: b.dt)
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
