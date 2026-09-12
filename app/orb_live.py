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
from datetime import datetime
from typing import Callable, Dict, List, Optional

from app.orb_selective import Bar, Signal, find_signal, daily_true_range, opening_range

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
    flatten_min: int = 955           # 15:55 ET force-flat
    cutoff_min: int = 660            # 11:00 ET no new entries
    slip_cap_ticks: float = 8.0      # entry limit slippage allowance


# per-instrument day state
_IDLE, _ARMED, _LIVE, _DONE = "IDLE", "ARMED", "LIVE", "DONE"


@dataclass
class _DayState:
    trading_date: str
    bars: List[Bar] = field(default_factory=list)
    status: str = _IDLE
    signal: Optional[Signal] = None
    entry_order_id: Optional[object] = None
    oso_ids: List[object] = field(default_factory=list)
    was_filled: bool = False


class Autopilot:
    """One per connected account. Feed -> on_bar(); a periodic loop -> poll()."""

    def __init__(self, cfg: AccountConfig, get_token: Callable[[], Optional[str]],
                 broker=None, atr: Optional[Dict[str, DailyATR]] = None,
                 now_et: Callable[[], datetime] = None):
        self.cfg = cfg
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
        if root not in self.cfg.instruments:
            return None
        st = self._day(root, bar)
        st.bars.append(bar)
        if st.status != _IDLE:
            return None                          # already armed / in a trade / done today

        params = self.cfg.params.get(root, DEFAULT_PARAMS.get(root))
        spec = INSTRUMENTS[root]
        sig = find_signal(st.bars, self.atr[root].value(),
                          stop_points=params.stop_points, target_points=params.target_points,
                          min_or_points=params.min_or_points, tick=spec.tick,
                          cutoff_min=self.cfg.cutoff_min)
        if sig is None:
            return None
        return self._place_bracket(root, st, sig, bar)

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
        resp = self._broker.place_stoplimit_bracket(
            self.cfg.env, token, self.cfg.account_spec, self.cfg.account_id,
            plan.entry_action, self.cfg.instruments[root], qty,
            plan.entry_stop, plan.entry_limit, plan.sl_price, plan.tp_price)
        oid = resp.get("orderId") if isinstance(resp, dict) else None
        if oid is None:
            st.status = _DONE                    # don't retry a rejected entry; one shot/day
            return {"root": root, "action": "reject", "resp": resp, "plan": plan}
        st.status = _ARMED
        st.signal = sig
        st.entry_order_id = oid
        return {"root": root, "action": "placed", "side": sig.side, "qty": qty,
                "order_id": oid, "plan": plan}

    # ---- periodic reconcile (seconds cadence) ------------------------------
    def poll(self) -> List[dict]:
        """Reconcile each armed/live instrument against broker truth: detect the entry fill,
        detect the SL/TP close, and force-flatten at EOD. Safe to call repeatedly."""
        out = []
        for root, st in self._state.items():
            if st.status in (_IDLE, _DONE):
                # EOD sweep even for IDLE (cancels any stray same-symbol working order we own)
                if self._past_flatten() and st.status != _DONE:
                    self._eod_flatten(root, st)
                    out.append({"root": root, "action": "eod", "from": _IDLE})
                continue
            token = self._get_token()
            if not token:
                out.append({"root": root, "action": "error", "reason": "no token"})
                continue
            if self._past_flatten():
                self._eod_flatten(root, st)
                out.append({"root": root, "action": "eod"})
                continue
            net, _, _ = self._net_position(root, token)
            if net != 0:
                st.was_filled = True
                if st.status == _ARMED:
                    st.status = _LIVE
                    out.append({"root": root, "action": "filled", "net": net})
            else:
                if st.status == _LIVE and st.was_filled:
                    # we were in a position and now we're flat -> SL or TP closed it
                    self._cancel_working(root, token)     # sweep the other OSO leg if it lingers
                    st.status = _DONE
                    out.append({"root": root, "action": "closed"})
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
        self._name_cache[cid] = name
        return name

    def _net_position(self, root: str, token: str):
        """(signed net, avg price, contractId) for `root` on the account; (0, None, None) if flat.
        Positions carry only a contractId, so we resolve it to a name to match the root."""
        spec = INSTRUMENTS[root]
        net, price, matched_cid = 0, None, None
        for p in (self._broker.get_positions(self.cfg.env, token) or []):
            if str(p.get("accountId")) != str(self.cfg.account_id):
                continue
            n = int(p.get("netPos") or 0)
            if n == 0:
                continue
            cid = p.get("contractId")
            cname = self._contract_name(token, cid)
            if cname and not cname.upper().startswith(spec.root):
                continue
            net += n
            matched_cid = cid
            if p.get("netPrice") is not None:
                price = float(p["netPrice"])
        return net, price, matched_cid

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

    def _eod_flatten(self, root: str, st: _DayState) -> None:
        token = self._get_token()
        if not token:
            st.status = _DONE
            return
        self._cancel_working(root, token)
        net, _, cid = self._net_position(root, token)
        if net != 0 and cid is not None:
            try:   # liquidatePosition cancels resting orders AND closes the position atomically
                self._broker.liquidate_position(self.cfg.env, token, self.cfg.account_id, cid)
            except Exception:
                pass
        st.status = _DONE

    def _past_flatten(self) -> bool:
        if self._now_et is None:
            return False
        now = self._now_et()
        return (now.hour * 60 + now.minute) >= self.cfg.flatten_min
