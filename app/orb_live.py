"""KhomaVolume live execution engine — the autopilot that turns server-side signals into
Tradovate orders. Add-only and version-independent: it talks ONLY to the stable tradovate_oauth
(`tvo`) REST layer and the signal brain (app.orb_selective) + feed (app.orb_feed). It does NOT
touch KhomaAPI's existing execution method or its ORB state machine.

Trade lifecycle per (account, instrument, day), mirroring the validated backtest but with NO
lookahead — conservative by design:
  1. Completed 1-min bars stream in (feed -> on_bar). We build the 09:30-09:45 ET opening range.
  2. The FIRST bar that CLOSES beyond the range with real volume + a decisive body, inside the
     09:45-11:00 window, is a confirmed signal (find_signal; thresholds hidden server-side).
  3. We place ONE atomic OSO stop-limit bracket: a StopLimit entry (slippage-capped so it can
     never chase) with a protective stop-MARKET + take-profit LIMIT that arm AT THE EXCHANGE on
     fill — zero naked window. Brackets are anchored to the confirmed close (fill-relative).
  4. poll() reconciles against the broker (fills/positions = truth): confirms the fill, detects
     the SL/TP close, and force-flattens at 15:55 ET. One trade per instrument per day.

Sizing mirrors the approved Prop Engine configurator 1:1, so what the operator picks in the UI
is exactly what the engine trades. Everything except the thin broker calls is pure + unit-tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date as _date, timedelta
from typing import Callable, Dict, List, Optional

from app.orb_selective import Bar, Signal, find_signal, daily_true_range, opening_range
from app import orb_momentum as MOM

# ---------------------------------------------------------------------------
# Instrument specs — MICRO futures only (what the Prop Engine sizes in).
# stop/target/min_or are POINTS, per instrument (scaled by the caller). MNQ is validated
# live-ready (backtest 30/75/8). M2K carries the same validated points but MUST be
# reconfirmed against the RTY backtest before live RTY trading (see orb-honest-performance).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InstrumentSpec:
    root: str            # micro root used for Tradovate symbol resolution
    tick: float          # minimum price increment
    dollars_per_point: float


@dataclass(frozen=True)
class SignalParams:
    stop_points: float
    target_points: float
    min_or_points: float


INSTRUMENTS: Dict[str, InstrumentSpec] = {
    "MNQ": InstrumentSpec("MNQ", 0.25, 2.0),   # micro Nasdaq-100
    "M2K": InstrumentSpec("M2K", 0.10, 5.0),   # micro Russell 2000
}
DEFAULT_PARAMS: Dict[str, SignalParams] = {
    # NQ: validated 30/75 pts (PF 1.72, 43% win, 2019-26). ~14%/35% of a typical NQ day.
    "MNQ": SignalParams(30.0, 75.0, 8.0),
    # RTY: fitted on real RTY 1-min 2019-26 (FirstRate), same 2.5:1 structure scaled to RTY's
    # ~32pt day. Center of a broad 7-10pt plateau: PF 1.64, 47% win, OOS holds (1.72->1.60),
    # survives 6x slippage (PF 1.35), 9/9 robustness. NOT the old 30/75 (that was ~a full RTY day).
    "M2K": SignalParams(8.0, 20.0, 1.5),
}

# ===========================================================================
# SIZING — mirrors scratchpad/prop_engine.html (the approved configurator) 1:1.
# ===========================================================================
PROP = {
    "25K":  {"target": 1250, "maxLoss": 1000, "fee": 70,  "cap": 1000},
    "50K":  {"target": 3000, "maxLoss": 2000, "fee": 115, "cap": 2000},
    "100K": {"target": 6000, "maxLoss": 3000, "fee": 180, "cap": 2500},
    "150K": {"target": 9000, "maxLoss": 4500, "fee": 245, "cap": 3000},
}
# EVAL[acct][preset] = [total_micros, pass_pct, ~days]
EVAL = {
    "25K":  {"cons": [4, 87, 20],  "norm": [6, 77, 11],  "aggr": [10, 60, 5]},
    "50K":  {"cons": [8, 86, 23],  "norm": [12, 75, 13], "aggr": [20, 65, 7]},
    "100K": {"cons": [12, 85, 32], "norm": [18, 74, 18], "aggr": [30, 62, 9]},
    "150K": {"cons": [20, 83, 27], "norm": [30, 72, 16], "aggr": [50, 62, 9]},
}
FUNDED = {
    "25K":  {"cons": 2, "norm": 4, "aggr": 6},
    "50K":  {"cons": 4, "norm": 8, "aggr": 12},
    "100K": {"cons": 6, "norm": 12, "aggr": 18},
    "150K": {"cons": 10, "norm": 20, "aggr": 30},
}
PER_MICRO_MONTH = 276
DD_PER_MICRO = 720
RISKFRAC = {"cons": 0.5, "norm": 0.75, "aggr": 1.0}


def _round_half_up(x: float) -> int:
    """JavaScript Math.round semantics (ties go to +inf), so sizing matches the configurator's
    numbers exactly — Python's built-in round() uses banker's rounding and would diverge on .5."""
    import math
    return int(math.floor(x + 0.5))


def split_micros(total: int, asset: str) -> Dict[str, int]:
    """Split a portfolio micro count across instruments exactly as the configurator does:
    nq-only -> all MNQ; rty-only -> all M2K; both -> half each (MNQ gets the rounded-up half)."""
    if asset == "nq":
        return {"MNQ": total, "M2K": 0}
    if asset == "rty":
        return {"MNQ": 0, "M2K": total}
    q = _round_half_up(total / 2)
    return {"MNQ": q, "M2K": total - q}


def live_cash_micros(balance: float, dd_pct: float, preset: str, asset: str) -> int:
    """Live-cash sizing: size to the drawdown budget, scaled by the preset's risk fraction,
    never below the 1/2-micro floor. mx = floor(budget / $720 per micro)."""
    mx = max(0, int((balance * dd_pct / 100.0) // DD_PER_MICRO))
    if mx == 0:
        return 0
    floor = 2 if asset == "both" else 1
    return max(floor, _round_half_up(mx * RISKFRAC[preset]))


def plan_total_micros(mode: str, acct: str, preset: str,
                      balance: float = 0.0, dd_pct: float = 0.0, asset: str = "both") -> int:
    """Total portfolio micros for a configuration (mode = 'eval' | 'funded' | 'cash')."""
    if mode == "eval":
        return EVAL[acct][preset][0]
    if mode == "funded":
        return FUNDED[acct][preset]
    if mode == "cash":
        return live_cash_micros(balance, dd_pct, preset, asset)
    raise ValueError(f"unknown mode {mode!r}")


# ===========================================================================
# ORDER MATH (pure) — fill-relative bracket anchored to the confirmed close.
# ===========================================================================
def round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)


@dataclass
class EntryPlan:
    """A fully-specified OSO bracket ready for tvo.place_stoplimit_bracket. The entry is a
    StopLimit whose limit caps slippage (so it never chases a runaway breakout); SL is
    stop-MARKET (prop tail safety), TP is a limit. Prices are fill-relative to `ref` (the
    confirmed breakout close) and tick-rounded."""
    side: str            # "long" | "short"
    entry_action: str    # "buy" | "sell"
    exit_action: str     # opposite — the closing side
    entry_stop: float    # StopLimit trigger
    entry_limit: float   # StopLimit price cap
    sl_price: float      # protective stop (stop-market)
    tp_price: float      # take-profit (limit)
    risk_points: float
    reward_points: float


def build_entry_plan(signal: Signal, ref_close: float, spec: InstrumentSpec,
                     params: SignalParams, slip_cap_ticks: float = 8.0) -> EntryPlan:
    """Turn a confirmed Signal + the breakout bar's close into a placeable OSO bracket.
    Anchored to `ref_close` (best estimate of the fill at placement), which is where the
    position actually opens when we act on the closed breakout bar."""
    tick = spec.tick
    ref = round_tick(ref_close, tick)
    cap = slip_cap_ticks * tick
    if signal.side == "long":
        entry_action, exit_action = "buy", "sell"
        entry_stop = round_tick(signal.trigger, tick)
        entry_limit = round_tick(ref + cap, tick)            # fill up to close + cap, no chase
        sl_price = round_tick(ref - params.stop_points, tick)
        tp_price = round_tick(ref + params.target_points, tick)
    else:
        entry_action, exit_action = "sell", "buy"
        entry_stop = round_tick(signal.trigger, tick)
        entry_limit = round_tick(ref - cap, tick)
        sl_price = round_tick(ref + params.stop_points, tick)
        tp_price = round_tick(ref - params.target_points, tick)
    return EntryPlan(signal.side, entry_action, exit_action, entry_stop, entry_limit,
                     sl_price, tp_price, params.stop_points, params.target_points)


# ===========================================================================
# ROLLING ATR (pure) — the selectivity "is the range alive but not exhausted?" gate.
# ===========================================================================
@dataclass
class DailyATR:
    """Simple moving average of prior sessions' true ranges. Today is NEVER included — the
    value() the autopilot reads reflects only completed prior days, so there is no lookahead.
    Seed it from historical daily ranges at startup; roll it at each session close."""
    period: int = 14
    _ranges: List[float] = field(default_factory=list)

    def add_session_range(self, tr: float) -> None:
        if tr is not None and tr > 0:
            self._ranges.append(float(tr))

    def value(self) -> Optional[float]:
        if not self._ranges:
            return None
        window = self._ranges[-self.period:]
        return sum(window) / len(window)


# ===========================================================================
# AUTOPILOT — per-account live state machine. Broker I/O is injected (defaults to tvo) so
# every decision path is unit-testable with a fake broker.
# ===========================================================================
@dataclass
class AccountConfig:
    env: str                         # "live" | "demo"
    account_spec: str                # Tradovate accountSpec (name)
    account_id: int
    account_name: str
    instruments: Dict[str, str]      # {"MNQ": "MNQZ5", "M2K": "M2KZ5"} resolved front contracts
    qty: Dict[str, int]              # {"MNQ": 3, "M2K": 3} micros per instrument
    params: Dict[str, SignalParams] = field(default_factory=lambda: dict(DEFAULT_PARAMS))
    observe: Dict[str, bool] = field(default_factory=dict)  # {"M2K": True} = log, don't trade
    flatten_min: int = 959           # 15:59 ET force-flat (validated rule exits at the close)
    cutoff_min: int = 660            # (legacy selective-ORB setting; unused by the momentum rule)
    slip_cap_ticks: float = 8.0      # entry limit slippage allowance
    # ---- validated momentum rule (orb_momentum) -------------------------------------------
    strategy: str = "momentum"       # "momentum" (validated, live) | "selective" (legacy)
    mode: str = "eval"               # eval | funded | cash
    risk_usd: float = 0.0            # $ risked per trade (stop-out cost) for this account/preset
    max_micros: int = 40             # account max size (eval: 20/40/60/100; funded: tier-0)
    acct_start: float = 0.0          # eval/funded starting balance (for the eval pass take-profit)
    acct_target: float = 0.0         # eval profit target
    day_tp_usd: Optional[float] = None   # eval daily take-profit cap (48% of target); None = hold to close
    tp_r: Optional[float] = None         # funded Safe: take-profit at tp_r x the stop distance (None = hold to close)
    min_micros: int = 0                  # funded: trade at least this many micros (validated sizing floor)
    risk_check: Optional[Callable[[], str]] = None   # -> lock reason ('' = OK); KhomaAPI Risk Engine lock


# ---------------------------------------------------------------------------
# MNQ news-day skip rule (data-validated: skipping these days lifts the 50K eval pass
# rate ~45%->51% and raises net P&L, because MNQ trades on them are net losers; M2K is
# UNAFFECTED and keeps trading). MNQ sits out: (1) FOMC decision days, (2) the 1st trading
# day of each month (ISM Manufacturing). Fail-open: any uncertainty -> trade normally.
# ---------------------------------------------------------------------------
# FOMC rate-decision days (ET). UPDATE ANNUALLY — if a year is missing, MNQ simply trades
# those days (safe fail-open), it just won't skip them until added.
_FOMC_DATES = frozenset({
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
})


def _is_month_start_holiday(dt: "_date") -> bool:
    """True for the only US market holidays that can fall at a month's start: New Year's Day
    (Jan 1, + its Monday observance when Jan 1 is a Sunday) and Labor Day (1st Monday of Sep)."""
    if dt.month == 1:
        if dt.day == 1:
            return True
        if dt.day == 2 and dt.replace(day=1).weekday() == 6:   # Jan 1 was Sunday -> Jan 2 observed
            return True
    if dt.month == 9 and dt.weekday() == 0 and dt.day <= 7:    # Labor Day = 1st Monday of September
        return True
    return False


def _is_first_trading_day_of_month(dt: "_date") -> bool:
    probe = dt.replace(day=1)
    while probe.weekday() >= 5 or _is_month_start_holiday(probe):   # skip weekends + month-start holidays
        probe += timedelta(days=1)
    return probe == dt


def mnq_skip_day(date_str: str) -> bool:
    """Should MNQ SIT OUT this ET date? (FOMC day OR 1st trading day of the month.) M2K never skips."""
    if not date_str:
        return False
    if date_str in _FOMC_DATES:
        return True
    try:
        dt = _date(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]))
    except Exception:
        return False                                            # fail-open: trade normally
    return _is_first_trading_day_of_month(dt)


# per-instrument day state
_IDLE, _ARMED, _LIVE, _DONE = "IDLE", "ARMED", "LIVE", "DONE"
VERIFY_SEC = 120          # an entry with an unknown outcome is checked against broker positions this long
ACCEPTED_CHECK_SEC = 60   # an accepted entry that is still flat after this long is settled from its order status
TOKEN_RETRY_MIN = 3       # a failed token at 10:30 is retried on each new bar for this many minutes


def _ambiguous(resp) -> bool:
    """True when an order request may have reached the broker without a usable answer (never re-send)."""
    if not isinstance(resp, dict):
        return True
    if resp.get("failureReason") or resp.get("failureText"):
        return False                                   # explicit broker rejection
    err = str(resp.get("error") or "")
    return err in ("timeout after send", "request failed") or err.startswith("http 5")


@dataclass
class _DayState:
    trading_date: str
    bars: List[Bar] = field(default_factory=list)
    status: str = _IDLE
    signal: Optional[Signal] = None
    plan: Optional[object] = None
    entry_order_id: Optional[object] = None
    oso_ids: List[object] = field(default_factory=list)
    was_filled: bool = False
    verify_until: Optional[datetime] = None   # entry sent but outcome unknown -> check broker truth until then
    eod_done: bool = False                    # 15:59 flatten verified (flat) for this day
    eod_tries: int = 0


class Autopilot:
    """One per connected account. Feed -> on_bar(); a periodic loop -> poll()."""

    def __init__(self, cfg: AccountConfig, get_token: Callable[[], Optional[str]],
                 broker=None, atr: Optional[Dict[str, DailyATR]] = None,
                 now_et: Callable[[], datetime] = None,
                 get_context: Optional[Callable[[str, str], Optional["MOM.DayContext"]]] = None):
        self.cfg = cfg
        self._get_context = get_context          # (root, date) -> DayContext (prior-session ATR + trend)
        self._get_token = get_token
        self._broker = broker
        if self._broker is None:                      # default to the real REST layer
            from app import tradovate_oauth as tvo
            self._broker = tvo
        self.atr = atr or {r: DailyATR() for r in cfg.instruments}
        self._now_et = now_et
        self._state: Dict[str, _DayState] = {}
        self._name_cache: Dict[object, str] = {}   # contractId -> contract name

    # ---- helpers -----------------------------------------------------------
    def _trading_date(self, bar: Optional[Bar]) -> str:
        if bar is not None:
            return bar.dt.strftime("%Y-%m-%d")
        if self._now_et is not None:
            return self._now_et().strftime("%Y-%m-%d")
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _day(self, root: str, bar: Optional[Bar]) -> _DayState:
        td = self._trading_date(bar)
        st = self._state.get(root)
        if st is None or st.trading_date != td:
            # new session: roll the just-finished day's true range into the ATR, then reset
            if st is not None and st.bars:
                tr = daily_true_range(st.bars, None)
                self.atr[root].add_session_range(tr)
            st = _DayState(trading_date=td)
            self._state[root] = st
        return st

    # ---- feed callback: a 1-min bar just CLOSED ----------------------------
    def on_bar(self, root: str, bar: Bar) -> Optional[dict]:
        """Accumulate the day's bars and, on the first confirmed breakout, place the bracket.
        Returns a dict describing the action taken (or None if nothing happened)."""
        if root not in self.cfg.instruments or bar.dt.weekday() >= 5:     # Sunday-evening Globex bars are not a session
            return None
        st = self._day(root, bar)
        st.bars.append(bar)
        if st.status != _IDLE:
            return None                          # already armed / in a trade / done today

        if self.cfg.strategy == "momentum":
            return self._momentum(root, st, bar)

        # MNQ sits out FOMC days + the 1st trading day of each month; lock it out for the day.
        # M2K (and any non-MNQ root) is unaffected and trades normally.
        if root == "MNQ" and mnq_skip_day(st.trading_date):
            st.status = _DONE
            return {"root": root, "action": "skip", "reason": "news_skip_day", "date": st.trading_date}

        params = self.cfg.params.get(root, DEFAULT_PARAMS.get(root))
        spec = INSTRUMENTS[root]
        sig = find_signal(st.bars, self.atr[root].value(),
                          stop_points=params.stop_points, target_points=params.target_points,
                          min_or_points=params.min_or_points, tick=spec.tick,
                          cutoff_min=self.cfg.cutoff_min)
        if sig is None:
            return None
        return self._place_bracket(root, st, sig, bar)

    # ---- validated momentum rule ----------------------------------------------------------
    def _context(self, root: str, date: str):
        if self._get_context is None:
            return None
        try:
            return self._get_context(root, date)
        except Exception:
            return None

    def _momentum(self, root: str, st: _DayState, bar: Bar) -> Optional[dict]:
        """10:30 decision: direction of 09:30 open -> 10:29 close, only with the 20-day trend."""
        if bar.minute_of_day < MOM.DECIDE_MIN:
            return None                                    # cheap early exit before the decision bar
        ctx = self._context(root, st.trading_date)
        status, res = MOM.decision(st.bars, ctx)
        if status == "wait":
            return None
        if status == "skip":
            st.status = _DONE
            return {"root": root, "action": "skip", "reason": res, "date": st.trading_date,
                    "trend": (ctx.trend if ctx else None), "atr": (ctx.atr if ctx else None)}
        return self._place_momentum(root, st, res, ctx)

    def eval_take_profit_usd(self, token: Optional[str]) -> Optional[float]:
        """Eval accounts: close the day's trade at the smaller of (a) the $ still needed to reach the
        target and (b) the daily cap that keeps the best day under the 50% consistency limit.
        Funded / cash accounts hold to the close (None). Balance read fail-safe -> cap only."""
        if self.cfg.mode != "eval" or not self.cfg.day_tp_usd:
            return None
        tp = float(self.cfg.day_tp_usd)
        if token and self.cfg.acct_start and self.cfg.acct_target:
            try:
                snap = self._broker.get_cash_snapshot(self.cfg.env, token, self.cfg.account_id) or {}
                bal = snap.get("totalCashValue")
                if bal is None:
                    bal = snap.get("netLiq")
                if bal is not None:
                    need = self.cfg.acct_start + self.cfg.acct_target - float(bal)
                    if need > 50:
                        tp = min(tp, need)
            except Exception:
                pass
        return tp

    def _place_momentum(self, root: str, st: _DayState, sig, ctx) -> dict:
        spec = INSTRUMENTS[root]
        qty = MOM.micros_for_risk(self.cfg.risk_usd, sig.stop_points, spec.dollars_per_point, self.cfg.max_micros)
        if qty <= 0 and self.cfg.min_micros > 0 and self.cfg.risk_usd > 0:
            qty = min(int(self.cfg.min_micros), int(self.cfg.max_micros))
        info = {"root": root, "side": sig.side, "qty": qty, "stop_pts": sig.stop_points,
                "ref": sig.ref_price, "move": sig.move, "trend": ctx.trend, "atr": round(ctx.atr, 2)}
        if self.cfg.observe.get(root):
            st.status = _DONE
            return {**info, "action": "observed"}
        if qty <= 0:
            st.status = _DONE
            return {**info, "action": "skip", "reason": "qty<=0"}
        if self.cfg.risk_check is not None:            # Risk Engine lock (daily loss / drawdown) blocks new entries
            try:
                lock = self.cfg.risk_check() or ""
            except Exception:
                lock = "Risk state unavailable - entry blocked (fail-closed)."
            if lock:
                st.status = _DONE
                return {**info, "action": "skip", "reason": "risk_locked", "detail": lock}
        token = self._get_token()
        if not token:
            # transient token failure: stay IDLE so the next bar (up to TOKEN_RETRY_MIN) retries the entry
            if self._bar_minute(st) < MOM.DECIDE_MIN + TOKEN_RETRY_MIN:
                return {**info, "action": "error", "reason": "no token (will retry next minute)"}
            st.status = _DONE
            return {**info, "action": "error", "reason": "no token"}
        long_ = sig.side == "long"
        sl = round_tick(sig.ref_price - sig.stop_points if long_ else sig.ref_price + sig.stop_points, spec.tick)
        tp_usd = self.eval_take_profit_usd(token)
        tp = None
        if tp_usd:
            pts = tp_usd / (qty * spec.dollars_per_point)
            tp = round_tick(sig.ref_price + pts if long_ else sig.ref_price - pts, spec.tick)
        elif self.cfg.tp_r:
            pts = float(self.cfg.tp_r) * sig.stop_points
            tp = round_tick(sig.ref_price + pts if long_ else sig.ref_price - pts, spec.tick)
        resp = self._broker.place_bracket_order(
            self.cfg.env, token, self.cfg.account_spec, self.cfg.account_id,
            "buy" if long_ else "sell", self.cfg.instruments[root], qty, stop_price=sl, limit_price=tp)
        oid = resp.get("orderId") if isinstance(resp, dict) else None
        if oid is None:
            if _ambiguous(resp):
                # the request may have reached the broker (timeout after send / transport error): never
                # re-send (duplicate risk) and never write it off -> verify against broker positions
                st.status = _ARMED
                st.entry_order_id = None
                st.verify_until = self._utcnow() + timedelta(seconds=VERIFY_SEC)
                st.plan = {"sl": sl, "tp": tp, "qty": qty, "side": sig.side}

                class _U:
                    side = sig.side
                st.signal = _U()
                return {**info, "action": "unconfirmed", "resp": resp, "sl": sl, "tp": tp}
            st.status = _DONE                              # broker explicitly rejected: one shot per day
            return {**info, "action": "reject", "resp": resp, "sl": sl, "tp": tp}
        st.status = _ARMED
        st.entry_order_id = oid
        st.verify_until = self._utcnow() + timedelta(seconds=ACCEPTED_CHECK_SEC)
        st.plan = {"sl": sl, "tp": tp, "qty": qty, "side": sig.side}

        class _S:  # minimal signal record for poll()'s "filled" event
            side = sig.side
        st.signal = _S()
        return {**info, "action": "placed", "order_id": oid, "sl": sl, "tp": tp, "tp_usd": tp_usd}

    def _place_bracket(self, root: str, st: _DayState, sig: Signal, bar: Bar) -> dict:
        spec = INSTRUMENTS[root]
        params = self.cfg.params.get(root, DEFAULT_PARAMS.get(root))
        qty = int(self.cfg.qty.get(root, 0))
        plan = build_entry_plan(sig, bar.c, spec, params, self.cfg.slip_cap_ticks)
        # observe-only: record the signal the engine WOULD have taken, place nothing. Used for an
        # instrument still being validated (e.g. RTY) so the demo exercises it without real risk.
        if self.cfg.observe.get(root):
            st.status = _DONE
            st.signal = sig
            return {"root": root, "action": "observed", "side": sig.side, "qty": qty,
                    "ref_close": bar.c, "plan": plan}
        if qty <= 0:
            st.status = _DONE
            return {"root": root, "action": "skip", "reason": "qty<=0"}
        token = self._get_token()
        if not token:
            return {"root": root, "action": "error", "reason": "no token"}
        # MARKET entry + attached OSO bracket (stop-market SL + limit TP). The breakout is already
        # CONFIRMED — the bar CLOSED beyond the level — so we enter at market immediately. A resting
        # STOP entry at the breakout level would sit on the WRONG side of the market by the time it
        # is placed (price has already passed it), and the broker rejects it. Market fills at the
        # breakout and the bracket protects the fill the instant it opens (no naked window).
        resp = self._broker.place_bracket_order(
            self.cfg.env, token, self.cfg.account_spec, self.cfg.account_id,
            plan.entry_action, self.cfg.instruments[root], qty,
            stop_price=plan.sl_price, limit_price=plan.tp_price)
        oid = resp.get("orderId") if isinstance(resp, dict) else None
        if oid is None:
            st.status = _DONE                    # don't retry a rejected entry; one shot/day
            return {"root": root, "action": "reject", "resp": resp, "plan": plan}
        st.status = _ARMED
        st.signal = sig
        st.plan = plan
        st.entry_order_id = oid
        return {"root": root, "action": "placed", "side": sig.side, "qty": qty,
                "order_id": oid, "plan": plan}

    # ---- ATR seed (so the OR-vs-ATR gate matches the backtest from day one) -
    def seed_atr(self, root: str, ranges: List[float]) -> None:
        """Seed the rolling ATR from prior sessions' true ranges. Idempotent — only seeds an
        empty ATR, so it runs once at startup and daily rolls take over afterward."""
        a = self.atr.get(root)
        if a is None or a.value() is not None:
            return
        for tr in ranges:
            a.add_session_range(tr)

    # ---- restart-proof opening-range backfill ------------------------------
    def backfill(self, root: str, bars: List[Bar]) -> dict:
        """Seed today's already-elapsed 1-min bars (from REST) so the opening range is rebuilt
        after a restart — WITHOUT entering on a breakout that already fired (no stale/late entry).
        If a qualifying signal already fired in the backfill, mark the day DONE; otherwise stay
        IDLE so a LIVE breakout going forward is still taken. Idempotent (skips if bars already)."""
        bars = [b for b in (bars or []) if b.dt.weekday() < 5]
        if root not in self.cfg.instruments or not bars:
            return {"root": root, "action": "backfill_skip", "reason": "no bars"}
        st = self._day(root, bars[-1])
        if self.cfg.strategy == "momentum":
            # MERGE history with any live bars that already arrived (a live bar can beat the backfill after a
            # restart); dedupe by minute so the 09:30 open is always present for the 10:30 decision.
            if st.status != _IDLE:
                return {"root": root, "action": "backfill_skip", "reason": "already active"}
            have = {b.dt.replace(second=0, microsecond=0, tzinfo=None) for b in st.bars}
            for b in bars:
                if b.dt.strftime("%Y-%m-%d") == st.trading_date and b.dt.replace(second=0, microsecond=0, tzinfo=None) not in have:
                    st.bars.append(b)
            st.bars.sort(key=lambda b: b.dt)
            last_hist = max((b.minute_of_day for b in bars), default=0)
            if last_hist >= MOM.DECIDE_MIN:          # the 10:29 decision bar already closed -> never enter late
                st.status = _DONE
                return {"root": root, "action": "backfill_missed", "bars": len(st.bars)}
            return {"root": root, "action": "backfill_ready", "bars": len(st.bars),
                    "has_open": any(MOM.OPEN_MIN <= b.minute_of_day < MOM.OPEN_MIN + 10 for b in st.bars)}
        if st.status != _IDLE or st.bars:
            return {"root": root, "action": "backfill_skip", "reason": "already active"}
        st.bars.extend(sorted(bars, key=lambda b: b.dt))    # elapsed bars -> reconstruct the OR
        if self.cfg.strategy == "momentum":
            # restart after the 10:29 decision bar already closed -> never enter late
            if any(b.minute_of_day >= MOM.DECIDE_MIN for b in st.bars):
                st.status = _DONE
                return {"root": root, "action": "backfill_missed", "bars": len(st.bars)}
            return {"root": root, "action": "backfill_ready", "bars": len(st.bars)}
        params = self.cfg.params.get(root, DEFAULT_PARAMS.get(root))
        sig = find_signal(st.bars, self.atr[root].value(),
                          stop_points=params.stop_points, target_points=params.target_points,
                          min_or_points=params.min_or_points, tick=INSTRUMENTS[root].tick,
                          cutoff_min=self.cfg.cutoff_min)
        if sig is not None:                                 # breakout already fired -> don't chase
            st.status = _DONE
            st.signal = sig
            return {"root": root, "action": "backfill_missed", "side": sig.side, "bars": len(st.bars)}
        return {"root": root, "action": "backfill_ready", "bars": len(st.bars),
                "has_or": opening_range(st.bars) is not None}

    # ---- periodic reconcile (seconds cadence) ------------------------------
    def poll(self) -> List[dict]:
        """Reconcile each armed/live instrument against broker truth: detect the entry fill,
        detect the SL/TP close, and force-flatten at EOD. Safe to call repeatedly."""
        out = []
        for root, st in self._state.items():
            if self._past_flatten():
                # 15:59 sweep for EVERY state (incl. DONE / restarted / unconfirmed) until the account is
                # verified flat in this instrument; retried every poll, never silently given up
                if not st.eod_done:
                    res = self._eod_flatten(root, st)
                    if res:
                        out.append({"root": root, **res})
                continue
            if st.status in (_IDLE, _DONE):
                continue
            token = self._get_token()
            if not token:
                out.append({"root": root, "action": "error", "reason": "no token"})
                continue
            net, price, _ = self._net_position(root, token)
            if net is None:                              # broker read failed -> try again next poll
                continue
            if net != 0:
                st.was_filled = True
                if st.status == _ARMED:
                    st.status = _LIVE
                    out.append({"root": root, "action": "filled", "net": net,
                                "side": (st.signal.side if st.signal else ""),
                                "qty": abs(net), "entry": price, "plan": st.plan})
            else:
                if st.status == _LIVE and st.was_filled:
                    # we were in a position and now we're flat -> SL or TP closed it
                    self._cancel_working(root, token)     # sweep the other OSO leg if it lingers
                    st.status = _DONE
                    out.append({"root": root, "action": "closed"})
                elif st.status == _ARMED and st.entry_order_id is None and st.verify_until is not None \
                        and self._utcnow() >= st.verify_until:
                    # unconfirmed entry never produced a position -> it did not execute
                    self._cancel_working(root, token)
                    st.status = _DONE
                    out.append({"root": root, "action": "reject", "reason": "entry not confirmed by broker"})
                elif st.status == _ARMED and st.entry_order_id is not None and st.verify_until is not None \
                        and self._utcnow() >= st.verify_until:
                    # accepted entry, still flat a minute later: rejected after acceptance, or filled AND
                    # already closed between two polls -> settle it from the order's final status
                    stat = self._order_status(token, st.entry_order_id)
                    if stat in ("rejected", "canceled", "cancelled", "expired"):
                        self._cancel_working(root, token)
                        st.status = _DONE
                        out.append({"root": root, "action": "reject", "reason": f"entry order {stat} by broker"})
                    elif stat == "filled":
                        self._cancel_working(root, token)
                        st.status = _DONE; st.was_filled = True
                        out.append({"root": root, "action": "filled", "net": 0, "side": (st.signal.side if st.signal else ""),
                                    "qty": (st.plan or {}).get("qty") if isinstance(st.plan, dict) else None,
                                    "entry": None, "plan": st.plan, "note": "filled and closed between checks"})
                        out.append({"root": root, "action": "closed"})
                    else:
                        st.verify_until = self._utcnow() + timedelta(seconds=ACCEPTED_CHECK_SEC)   # still working
        return out

    # ---- broker-truth helpers (thin) ---------------------------------------
    def _contract_name(self, token: str, cid) -> str:
        """Cached contractId -> contract name (positions/orders carry only the id)."""
        if cid is None:
            return ""
        if cid in self._name_cache:
            return self._name_cache[cid]
        name = ""
        try:
            c = self._broker.get_contract(self.cfg.env, token, cid)
            if isinstance(c, dict):
                name = str(c.get("name") or "")
        except Exception:
            name = ""
        if name:                                # don't cache a failed lookup; retry it next time
            self._name_cache[cid] = name
        return name

    def _net_position(self, root: str, token: str):
        """(signed net, avg price, contractId) for `root` on the account; (0, None, None) if flat.
        Positions carry only a contractId, so we resolve it to a name to match the root."""
        spec = INSTRUMENTS[root]
        net, price, matched_cid = 0, None, None
        try:
            positions = self._broker.get_positions(self.cfg.env, token)
        except Exception:
            positions = None
        if positions is None:
            return None, None, None
        for p in positions:
            if str(p.get("accountId")) != str(self.cfg.account_id):
                continue
            n = int(p.get("netPos") or 0)
            if n == 0:
                continue
            cid = p.get("contractId")
            cname = self._contract_name(token, cid)
            if not cname or not cname.upper().startswith(spec.root):
                continue                        # unknown or other instrument: never ours to manage
            net += n
            matched_cid = cid
            if p.get("netPrice") is not None:
                price = float(p["netPrice"])
        return net, price, matched_cid

    def _order_status(self, token: str, order_id) -> str:
        try:
            for o in (self._broker.get_orders(self.cfg.env, token) or []):
                if str(o.get("id")) == str(order_id):
                    return str(o.get("ordStatus") or o.get("status") or "").lower()
        except Exception:
            pass
        return ""

    def _working_ids(self, root: str, token: str) -> List[object]:
        spec = INSTRUMENTS[root]
        orders = self._broker.get_orders(self.cfg.env, token) or []
        try:
            versions = self._broker.get_order_versions(self.cfg.env, token) or []
            working = self._broker.working_orders_for(orders, versions, self.cfg.account_id)
        except Exception:
            working = []
        ids = []
        for w in working:
            cname = self._contract_name(token, w.get("contractId")) if w.get("contractId") is not None else ""
            if cname and cname.upper().startswith(spec.root):      # only this instrument's orders
                ids.append(w.get("id"))
        return [i for i in ids if i is not None]

    def _cancel_working(self, root: str, token: str) -> int:
        n = 0
        for oid in self._working_ids(root, token):
            try:
                if self._broker.cancel_order(self.cfg.env, token, oid) is not None:
                    n += 1
            except Exception:
                pass
        return n

    def _eod_flatten(self, root: str, st: _DayState) -> Optional[dict]:
        """Cancel this instrument's working orders and liquidate any position; done only once a fresh
        broker read confirms flat. Returns an event dict (or None when there was nothing to do)."""
        st.eod_tries += 1
        token = self._get_token()
        if not token:
            return {"action": "eod_error", "reason": "no token", "tries": st.eod_tries}
        self._cancel_working(root, token)
        net, _, cid = self._net_position(root, token)
        if net is None:
            return {"action": "eod_error", "reason": "positions unavailable", "tries": st.eod_tries}
        if net != 0 and cid is not None:
            try:   # liquidatePosition cancels resting orders AND closes the position atomically
                self._broker.liquidate_position(self.cfg.env, token, self.cfg.account_id, cid)
            except Exception:
                pass
            net2, _, _ = self._net_position(root, token)
            if net2 != 0:
                return {"action": "eod_error", "reason": "still open after liquidate", "net": net2, "tries": st.eod_tries}
            st.status = _DONE; st.eod_done = True
            return {"action": "eod", "flattened": net}
        st.status = _DONE; st.eod_done = True
        return {"action": "eod", "flattened": 0}            # verified flat (logged once per day)

    def _utcnow(self) -> datetime:
        return datetime.utcnow()

    def _bar_minute(self, st: _DayState) -> int:
        return st.bars[-1].minute_of_day if st.bars else 0

    def _past_flatten(self) -> bool:
        if self._now_et is None:
            return False
        now = self._now_et()
        return (now.hour * 60 + now.minute) >= self.cfg.flatten_min
