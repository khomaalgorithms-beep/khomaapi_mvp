"""KhomaVolume multi-strategy autopilot: ORB + prior-day break (resting stop entry at 09:35) and, in 'both' mode,
the 10:30 momentum rule as a second, independent leg on the same account.

Both legs trade the same contract, so the broker nets their positions. Each leg is therefore tracked by its OWN
order ids (entry, protective stop, take-profit) — never by the account's net position — and the 15:59 flatten
works on the whole instrument (inherited from Autopilot). Validated rules: lucid_research/ORB_PREREGISTRATION.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app import orb_momentum as MOM
from app.orb_live import (Autopilot, INSTRUMENTS, _DayState, _IDLE, _DONE, _ambiguous, round_tick,
                          VERIFY_SEC, TOKEN_RETRY_MIN)

PENDING, ARMED, LIVE, DONE = "PENDING", "ARMED", "LIVE", "DONE"
_FINAL_BAD = ("rejected", "canceled", "cancelled", "expired")


@dataclass
class Leg:
    name: str                       # "orb" | "mom"
    side: str                       # "long" | "short"
    qty: int
    entry_px: float                 # resting stop price (orb) / reference price (mom)
    stop_pts: float
    sl: float
    tp: Optional[float]
    status: str = PENDING
    entry_oid: object = None
    stop_oid: object = None
    tp_oid: object = None
    fill: Optional[float] = None
    be_done: bool = False
    verify_until: Optional[datetime] = None
    sent_at: Optional[datetime] = None

    @property
    def sgn(self) -> int:
        return 1 if self.side == "long" else -1


class MultiAutopilot(Autopilot):
    """strategy 'orb' (ORB leg only) or 'both' (ORB leg + 10:30 momentum leg)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.legs: Dict[str, List[Leg]] = {}          # root -> legs today
        self._orb_done: Dict[str, str] = {}           # root -> trading date the ORB decision was settled
        self._mom_done: Dict[str, str] = {}

    # ---- day helpers -------------------------------------------------------------------------------
    def _today_legs(self, root: str, st: _DayState) -> List[Leg]:
        if getattr(st, "_legs_date", None) != st.trading_date:
            st._legs_date = st.trading_date
            self.legs[root] = []
        return self.legs.setdefault(root, [])

    def _tp_price(self, token, ref: float, side: int, qty: int, stop_pts: float, spec) -> (Optional[float], Optional[float]):
        tp_usd = self.eval_take_profit_usd(token)
        if tp_usd:
            return round_tick(ref + side * tp_usd / (qty * spec.dollars_per_point), spec.tick), tp_usd
        if self.cfg.tp_r:
            return round_tick(ref + side * float(self.cfg.tp_r) * stop_pts, spec.tick), None
        return None, None

    def _qty(self, stop_pts: float, spec) -> int:
        q = MOM.micros_for_risk(self.cfg.risk_usd, stop_pts, spec.dollars_per_point, self.cfg.max_micros)
        if q <= 0 and self.cfg.min_micros > 0 and self.cfg.risk_usd > 0:
            q = min(int(self.cfg.min_micros), int(self.cfg.max_micros))
        return q

    def _risk_lock(self) -> str:
        if self.cfg.risk_check is None:
            return ""
        try:
            return self.cfg.risk_check() or ""
        except Exception:
            return "Risk state unavailable - entry blocked (fail-closed)."

    # ---- feed callback -----------------------------------------------------------------------------
    def on_bar(self, root, bar):
        if root not in self.cfg.instruments:
            return None
        st = self._day(root, bar)
        st.bars.append(bar)
        legs = self._today_legs(root, st)
        out = []
        r = self._orb_step(root, st, bar, legs)
        if r:
            out.append(r)
        if self.cfg.strategy == "both":
            r = self._mom_step(root, st, bar, legs)
            if r:
                out.append(r)
        r = self._breakeven(root, bar, legs)
        if r:
            out.append(r)
        if not out:
            return None
        return out[0] if len(out) == 1 else {**out[0], "also": out[1:]}

    def _orb_step(self, root, st, bar, legs) -> Optional[dict]:
        m = bar.minute_of_day; td = st.trading_date
        if self._orb_done.get(root) == td:
            # entry window management: first break of the INVALID side ends the day; 10:35 cancels unfilled entries
            pend = [l for l in legs if l.name == "orb" and l.status == PENDING]
            if not pend:
                return None
            lv = getattr(st, "_orb_lv", None)
            broke = lv and self._invalid_break(bar, lv, pend)
            if broke or m >= MOM.ORB_LAST_MIN - 1:          # the 10:34 bar is the last one an entry may fill in
                why = "other_side_broke_first" if broke else "no_break"
                self._cancel_legs(pend)
                return {"root": root, "action": "skip", "strategy": "orb", "reason": why}
            return None
        if m < MOM.ORB_END_MIN - 1:
            return None
        self._orb_done[root] = td
        if MOM.short_session(td):
            return {"root": root, "action": "skip", "strategy": "orb", "reason": "holiday_or_half_day"}
        if m > MOM.ORB_END_MIN - 1:
            return {"root": root, "action": "skip", "strategy": "orb", "reason": "late"}     # never place late
        ctx = self._context(root, td)
        spec = INSTRUMENTS[root]
        lv = MOM.orb_levels(st.bars, ctx, spec.tick)
        if lv is None:
            return {"root": root, "action": "skip", "strategy": "orb", "reason": "no_context"}
        hi, lo, vlong, vshort = lv
        st._orb_lv = (hi, lo, vlong, vshort)
        if not (vlong or vshort):
            return {"root": root, "action": "skip", "strategy": "orb", "reason": "not_beyond_prior_day"}
        stop_pts = round(MOM.STOP_ATR * ctx.atr, 2)
        qty = self._qty(stop_pts, spec)
        info = {"root": root, "strategy": "orb", "qty": qty, "stop_pts": stop_pts, "hi": hi, "lo": lo,
                "long": vlong, "short": vshort}
        if self.cfg.observe.get(root):
            return {**info, "action": "observed"}
        if qty <= 0:
            return {**info, "action": "skip", "reason": "qty<=0"}
        lock = self._risk_lock()
        if lock:
            return {**info, "action": "skip", "reason": "risk_locked", "detail": lock}
        token = self._get_token()
        if not token:
            return {**info, "action": "error", "reason": "no token"}
        placed = []
        for side, ok, px in ((1, vlong, round_tick(hi + spec.tick, spec.tick)), (-1, vshort, round_tick(lo - spec.tick, spec.tick))):
            if not ok:
                continue
            sl = round_tick(px - side * stop_pts, spec.tick)
            tp, _ = self._tp_price(token, px, side, qty, stop_pts, spec)
            resp = self._broker.place_stop_entry_bracket(
                self.cfg.env, token, self.cfg.account_spec, self.cfg.account_id, "buy" if side > 0 else "sell",
                self.cfg.instruments[root], qty, px, sl, tp)
            leg = Leg("orb", "long" if side > 0 else "short", qty, px, stop_pts, sl, tp, sent_at=self._utcnow())
            oid = resp.get("orderId") if isinstance(resp, dict) else None
            if oid is not None:
                leg.entry_oid = oid
                leg.stop_oid = resp.get("oso1Id")
                leg.tp_oid = resp.get("oso2Id") if tp is not None else None
                legs.append(leg)
                placed.append({"side": leg.side, "entry": px, "sl": sl, "tp": tp, "order_id": oid})
            elif _ambiguous(resp):
                leg.status = ARMED                   # outcome unknown: verify against broker orders, never re-send
                leg.verify_until = self._utcnow() + timedelta(seconds=VERIFY_SEC)
                legs.append(leg)
                placed.append({"side": leg.side, "unconfirmed": True, "resp": resp})
            else:
                placed.append({"side": leg.side, "rejected": True, "resp": resp})
        if not any(p.get("order_id") or p.get("unconfirmed") for p in placed):
            return {**info, "action": "reject", "orders": placed}
        return {**info, "action": "placed", "orders": placed}

    @staticmethod
    def _invalid_break(bar, lv, pend) -> bool:
        """The research rule: the FIRST break of either side decides the day. If the side we did not (could not)
        place breaks first, the resting order on the other side must be cancelled."""
        hi, lo, vlong, vshort = lv
        sides = {l.side for l in pend}
        broke_up = bar.h >= hi + 0.25
        broke_dn = bar.l <= lo - 0.25
        return (broke_up and "long" not in sides) or (broke_dn and "short" not in sides)

    def _mom_step(self, root, st, bar, legs) -> Optional[dict]:
        td = st.trading_date
        if self._mom_done.get(root) == td or bar.minute_of_day < MOM.DECIDE_MIN:
            return None
        ctx = self._context(root, td)
        status, res = MOM.decision(st.bars, ctx)
        if status == "wait":
            return None
        if status == "skip":
            self._mom_done[root] = td
            return {"root": root, "action": "skip", "strategy": "mom", "reason": res}
        sig = res; spec = INSTRUMENTS[root]; side = 1 if sig.side == "long" else -1
        qty = self._qty(sig.stop_points, spec)
        info = {"root": root, "strategy": "mom", "side": sig.side, "qty": qty, "stop_pts": sig.stop_points, "ref": sig.ref_price}
        if self.cfg.observe.get(root) or qty <= 0:
            self._mom_done[root] = td
            return {**info, "action": "observed" if self.cfg.observe.get(root) else "skip", "reason": "qty<=0"}
        lock = self._risk_lock()
        if lock:
            self._mom_done[root] = td
            return {**info, "action": "skip", "reason": "risk_locked", "detail": lock}
        token = self._get_token()
        if not token:
            if bar.minute_of_day < MOM.DECIDE_MIN + TOKEN_RETRY_MIN:
                return {**info, "action": "error", "reason": "no token (will retry next minute)"}
            self._mom_done[root] = td
            return {**info, "action": "error", "reason": "no token"}
        self._mom_done[root] = td
        sl = round_tick(sig.ref_price - side * sig.stop_points, spec.tick)
        tp, _ = self._tp_price(token, sig.ref_price, side, qty, sig.stop_points, spec)
        resp = self._broker.place_bracket_order(self.cfg.env, token, self.cfg.account_spec, self.cfg.account_id,
                                                "buy" if side > 0 else "sell", self.cfg.instruments[root], qty,
                                                stop_price=sl, limit_price=tp)
        leg = Leg("mom", sig.side, qty, sig.ref_price, sig.stop_points, sl, tp, status=ARMED, sent_at=self._utcnow())
        oid = resp.get("orderId") if isinstance(resp, dict) else None
        if oid is not None:
            leg.entry_oid = oid; leg.stop_oid = resp.get("oso1Id"); leg.tp_oid = resp.get("oso2Id") if tp is not None else None
            leg.verify_until = self._utcnow() + timedelta(seconds=60)
            legs.append(leg)
            return {**info, "action": "placed", "order_id": oid, "sl": sl, "tp": tp}
        if _ambiguous(resp):
            leg.verify_until = self._utcnow() + timedelta(seconds=VERIFY_SEC)
            legs.append(leg)
            return {**info, "action": "unconfirmed", "resp": resp, "sl": sl, "tp": tp}
        return {**info, "action": "reject", "resp": resp, "sl": sl, "tp": tp}

    # ---- breakeven (ORB leg): stop to the fill once a COMPLETED bar has reached +1R ------------------
    def _breakeven(self, root, bar, legs) -> Optional[dict]:
        for l in legs:
            if l.name != "orb" or l.status != LIVE or l.be_done or l.fill is None:
                continue
            fav = (bar.h - l.fill) if l.sgn > 0 else (l.fill - bar.l)
            if fav < MOM.ORB_BE_R * l.stop_pts:
                continue
            token = self._get_token()
            if not token:
                return {"root": root, "action": "error", "strategy": "orb", "reason": "no token for breakeven"}
            sid = l.stop_oid or self._find_order_id(root, token, "stop", l.sl)
            if sid is None:
                return {"root": root, "action": "error", "strategy": "orb", "reason": "breakeven: stop order not found"}
            new = round_tick(l.fill, INSTRUMENTS[root].tick)
            resp = self._broker.modify_stop_price(self.cfg.env, token, sid, new, qty=l.qty)   # Tradovate requires orderQty
            if not isinstance(resp, dict) or resp.get("failureReason") or resp.get("error"):
                return {"root": root, "action": "error", "strategy": "orb", "reason": "breakeven modify failed", "resp": resp}
            l.stop_oid = sid; l.sl = new; l.be_done = True
            return {"root": root, "action": "breakeven", "strategy": "orb", "stop": new}
        return None

    # ---- broker truth ------------------------------------------------------------------------------
    def _orders(self, token) -> Dict[str, dict]:
        try:
            return {str(o.get("id")): o for o in (self._broker.get_orders(self.cfg.env, token) or [])
                    if str(o.get("accountId", self.cfg.account_id)) == str(self.cfg.account_id)}
        except Exception:
            return {}

    @staticmethod
    def _status(orders, oid) -> str:
        o = orders.get(str(oid)) if oid is not None else None
        return str((o or {}).get("ordStatus") or (o or {}).get("status") or "").lower()

    def _find_order_id(self, root, token, kind, price):
        """Locate a working bracket order of this instrument by type and price (when placeOSO did not return ids)."""
        try:
            orders = self._broker.get_orders(self.cfg.env, token) or []
            versions = self._broker.get_order_versions(self.cfg.env, token) or []
            for w in self._broker.working_orders_for(orders, versions, self.cfg.account_id):
                t = str(w.get("orderType") or "").lower()
                p = w.get("stopPrice") if kind == "stop" else w.get("price")
                if t == kind and p is not None and abs(float(p) - float(price)) < 1e-6:
                    name = self._contract_name(token, w.get("contractId"))
                    if name and name.upper().startswith(INSTRUMENTS[root].root):
                        return w.get("id")
        except Exception:
            pass
        return None

    def _fill_price(self, token, oid, fallback) -> float:
        try:
            fills = [f for f in (self._broker.get_fills(self.cfg.env, token) or []) if str(f.get("orderId")) == str(oid)]
            q = sum(abs(int(f.get("qty") or 0)) for f in fills)
            if q:
                return sum(float(f["price"]) * abs(int(f.get("qty") or 0)) for f in fills) / q
        except Exception:
            pass
        return float(fallback)

    def _cancel_legs(self, legs: List[Leg]) -> None:
        token = self._get_token()
        for l in legs:
            if token and l.entry_oid is not None:
                try:
                    self._broker.cancel_order(self.cfg.env, token, l.entry_oid)
                except Exception:
                    pass
            l.status = DONE

    # ---- periodic reconcile ------------------------------------------------------------------------
    def poll(self) -> List[dict]:
        out: List[dict] = []
        for root, st in list(self._state.items()):
            if self._past_flatten():
                if not st.eod_done:
                    res = self._eod_flatten(root, st)
                    if res:
                        out.append({"root": root, **res})
                    if st.eod_done:
                        for l in self.legs.get(root, []):
                            l.status = DONE
                continue
            legs = [l for l in self.legs.get(root, []) if l.status in (PENDING, ARMED, LIVE)]
            if not legs:
                continue
            token = self._get_token()
            if not token:
                out.append({"root": root, "action": "error", "reason": "no token"})
                continue
            orders = self._orders(token)
            for l in legs:
                if l.status in (PENDING, ARMED):
                    if l.entry_oid is None:                       # unknown outcome: look for our order at the broker
                        l.entry_oid = self._adopt_unconfirmed(orders, l)
                        if l.entry_oid is None:
                            if l.verify_until and self._utcnow() >= l.verify_until:
                                l.status = DONE
                                out.append({"root": root, "action": "reject", "strategy": l.name, "reason": "entry not confirmed by broker"})
                            continue
                    s = self._status(orders, l.entry_oid)
                    if s == "filled":
                        l.status = LIVE
                        l.fill = self._fill_price(token, l.entry_oid, l.entry_px)
                        if l.stop_oid is None:
                            l.stop_oid = self._find_order_id(root, token, "stop", l.sl)
                        if l.tp is not None and l.tp_oid is None:
                            l.tp_oid = self._find_order_id(root, token, "limit", l.tp)
                        out.append({"root": root, "action": "filled", "strategy": l.name, "side": l.side, "qty": l.qty,
                                    "entry": l.fill, "plan": {"sl": l.sl, "tp": l.tp, "qty": l.qty, "side": l.side}})
                        if l.name == "orb":                       # first fill wins: cancel the other side's resting entry
                            sib = [x for x in self.legs.get(root, []) if x is not l and x.name == "orb" and x.status == PENDING]
                            if sib:
                                self._cancel_legs(sib)
                    elif s in _FINAL_BAD:
                        l.status = DONE
                        out.append({"root": root, "action": "reject", "strategy": l.name, "reason": f"entry order {s} by broker"})
                elif l.status == LIVE:
                    ss, ts = self._status(orders, l.stop_oid), self._status(orders, l.tp_oid)
                    if ss == "filled" or ts == "filled":
                        l.status = DONE
                        out.append({"root": root, "action": "closed", "strategy": l.name,
                                    "exit": "stop" if ss == "filled" else "target"})
        return out

    def _adopt_unconfirmed(self, orders, leg) -> Optional[object]:
        """After an ambiguous send: an order of the same direction created after the send that no leg owns."""
        owned = {str(x.entry_oid) for ls in self.legs.values() for x in ls if x.entry_oid is not None}
        want = "buy" if leg.sgn > 0 else "sell"
        for oid, o in orders.items():
            if oid in owned or str(o.get("action") or "").lower() != want:
                continue
            ts = o.get("timestamp")
            try:
                t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None) if ts else None
            except Exception:
                t = None
            if t is not None and leg.sent_at is not None and t >= leg.sent_at - timedelta(seconds=5):
                return o.get("id")
        return None

    # ---- restart safety ----------------------------------------------------------------------------
    def backfill(self, root, bars):
        bars = list(bars or [])
        return self._backfill(root, bars)

    def _backfill(self, root, bars):
        """Restart mid-session: rebuild today's bars; never place a late entry. If the ORB entry window has started,
        resting entries placed before the restart are unknown to this process -> cancel this instrument's working
        orders when FLAT (stale entries); when a position exists its bracket protects it and 15:59 flattens it."""
        if root not in self.cfg.instruments or not bars:
            return {"root": root, "action": "backfill_skip", "reason": "no bars"}
        st = self._day(root, bars[-1])
        have = {b.dt.replace(second=0, microsecond=0, tzinfo=None) for b in st.bars}
        for b in bars:
            if b.dt.strftime("%Y-%m-%d") == st.trading_date and b.dt.replace(second=0, microsecond=0, tzinfo=None) not in have:
                st.bars.append(b)
        st.bars.sort(key=lambda b: b.dt)
        last = max((b.minute_of_day for b in bars if b.dt.strftime("%Y-%m-%d") == st.trading_date), default=0)
        res = {"root": root, "action": "backfill_ready", "bars": len(st.bars)}
        if last >= MOM.ORB_END_MIN - 1:
            self._orb_done[root] = st.trading_date
            res["orb"] = "missed"
            token = self._get_token()
            if token and last < self.cfg.flatten_min:
                net, _, _ = self._net_position(root, token)
                if net == 0:
                    n = self._cancel_working(root, token)
                    res["cancelled_stale"] = n
        if last >= MOM.DECIDE_MIN:
            self._mom_done[root] = st.trading_date
            res["mom"] = "missed"
        if res.get("orb") and (self.cfg.strategy != "both" or res.get("mom")):
            res["action"] = "backfill_missed"
        return res
