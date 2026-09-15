"""Live execution engine (autopilot) — pure sizing/order-math + the full trade lifecycle
driven against a fake broker. No network, no real orders. Proves: sizing matches the approved
Prop Engine 1:1; brackets are fill-relative, tick-rounded, slippage-capped, stop-market SL;
one trade/instrument/day; fill/close/EOD reconciliation via broker truth."""
from datetime import datetime

from app.orb_selective import Bar, Signal
from app.orb_live import (
    INSTRUMENTS, DEFAULT_PARAMS, SignalParams, AccountConfig, Autopilot, DailyATR,
    build_entry_plan, round_tick, split_micros, live_cash_micros, plan_total_micros,
    EVAL, FUNDED,
)


# ======================= sizing (must match prop_engine.html) =======================
def test_eval_funded_sizing_matches_artifact():
    assert plan_total_micros("eval", "25K", "norm") == 6
    assert plan_total_micros("eval", "100K", "aggr") == 30
    assert plan_total_micros("funded", "50K", "norm") == 8
    assert plan_total_micros("funded", "150K", "aggr") == 30
    assert EVAL["25K"]["cons"][0] == 4 and FUNDED["25K"]["cons"] == 2


def test_split_micros():
    assert split_micros(6, "both") == {"MNQ": 3, "M2K": 3}
    assert split_micros(5, "both") == {"MNQ": 3, "M2K": 2}     # MNQ gets rounded half
    assert split_micros(10, "nq") == {"MNQ": 10, "M2K": 0}
    assert split_micros(10, "rty") == {"MNQ": 0, "M2K": 10}


def test_live_cash_sizing_matches_artifact_formula():
    # balance 50k, dd 10% -> budget 5000 -> mx = floor(5000/720) = 6
    # norm: max(2, round(6*.75)) = max(2, 5) = 5 ; cons: max(2, round(3)) = 3 ; aggr: 6
    assert live_cash_micros(50000, 10, "norm", "both") == 5
    assert live_cash_micros(50000, 10, "cons", "both") == 3
    assert live_cash_micros(50000, 10, "aggr", "both") == 6
    # budget 500 (5000 @ 10%) -> mx = floor(500/720) = 0 -> too small to trade -> 0
    assert live_cash_micros(5000, 10, "norm", "both") == 0


def test_live_cash_zero_when_budget_below_one_micro():
    assert live_cash_micros(1000, 5, "norm", "both") == 0      # budget 50 -> mx 0


# ======================= order math =======================
def test_build_entry_plan_long_fill_relative():
    spec = INSTRUMENTS["MNQ"]; params = DEFAULT_PARAMS["MNQ"]
    sig = Signal("long", trigger=20000.5, stop_points=30, target_points=75,
                 or_high=20000.0, or_low=19950.0, or_range=50.0)
    plan = build_entry_plan(sig, ref_close=20005.0, spec=spec, params=params, slip_cap_ticks=8)
    assert plan.entry_action == "buy" and plan.exit_action == "sell"
    assert plan.entry_stop == 20000.5                         # trigger (already crossed)
    assert plan.entry_limit == 20007.0                        # close 20005 + 8 ticks(2.0)
    assert plan.sl_price == 19975.0                           # close - 30 (stop-market)
    assert plan.tp_price == 20080.0                           # close + 75
    assert plan.risk_points == 30 and plan.reward_points == 75


def test_build_entry_plan_short_mirrors():
    spec = INSTRUMENTS["MNQ"]; params = DEFAULT_PARAMS["MNQ"]
    sig = Signal("short", trigger=19949.5, stop_points=30, target_points=75,
                 or_high=20000.0, or_low=19950.0, or_range=50.0)
    plan = build_entry_plan(sig, ref_close=19945.0, spec=spec, params=params, slip_cap_ticks=8)
    assert plan.entry_action == "sell" and plan.exit_action == "buy"
    assert plan.entry_limit == 19943.0                        # close - 2.0
    assert plan.sl_price == 19975.0                           # close + 30 (above, stop-market)
    assert plan.tp_price == 19870.0                           # close - 75


def test_round_tick():
    assert round_tick(20000.13, 0.25) == 20000.25
    assert round_tick(20000.12, 0.25) == 20000.0


# ======================= ATR roller =======================
def test_daily_atr_excludes_today_and_averages():
    atr = DailyATR(period=3)
    assert atr.value() is None                 # nothing yet -> signal skips ATR gate
    for tr in (30, 60, 90):
        atr.add_session_range(tr)
    assert atr.value() == 60                    # mean of last 3
    atr.add_session_range(120)
    assert atr.value() == (60 + 90 + 120) / 3   # rolling window of 3


# ======================= autopilot lifecycle =======================
class FakeBroker:
    """Records calls; returns programmable broker state."""
    def __init__(self):
        self.place_calls = []
        self.cancels = []
        self.liquidations = []
        self.next_order_id = 555
        self.positions = []                 # list of position dicts
        self.orders = []
        self.versions = []
        self.contracts = {901: {"name": "MNQZ5"}}

    def place_stoplimit_bracket(self, env, token, spec, acct, action, symbol, qty,
                                es, el, sl, tp):
        self.place_calls.append(dict(action=action, symbol=symbol, qty=qty,
                                     entry_stop=es, entry_limit=el, sl=sl, tp=tp))
        oid = self.next_order_id; self.next_order_id += 1
        return {"orderId": oid}

    def get_positions(self, env, token):
        return self.positions

    def get_orders(self, env, token):
        return self.orders

    def get_order_versions(self, env, token):
        return self.versions

    def working_orders_for(self, orders, versions, account_id, **kw):
        return [{"id": o.get("id")} for o in orders]

    def cancel_order(self, env, token, oid):
        self.cancels.append(oid); return {"ok": True}

    def get_contract(self, env, token, cid):
        return self.contracts.get(cid, {"name": ""})

    def liquidate_position(self, env, token, account_id, cid):
        self.liquidations.append(cid); return {"ok": True}


def cfg_for(qty_mnq=3, flatten_min=955):
    return AccountConfig(env="demo", account_spec="DEMO1", account_id=42,
                         account_name="DEMO1", instruments={"MNQ": "MNQZ5"},
                         qty={"MNQ": qty_mnq}, flatten_min=flatten_min)


def mk(hh, mm, o, h, l, c, v=100):
    return Bar(datetime(2026, 1, 2, hh, mm), o, h, l, c, v)


def breakout_day():
    """15 OR bars (09:30-09:44, range 19950-20000) then a decisive long breakout bar."""
    orb = [mk(9, 30 + i, 19975, 20000, 19950, 19975, 100) for i in range(15)]
    brk = mk(9, 50, 20000, 20030, 19995, 20025, 300)      # closes above, 3x vol, full body
    return orb + brk_as_list(brk)


def brk_as_list(b):
    return [b]


def feed(ap, bars, root="MNQ"):
    res = [ap.on_bar(root, b) for b in bars]
    return [r for r in res if r]


def test_on_bar_places_bracket_on_confirmed_breakout():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    actions = feed(ap, breakout_day())
    assert len(fb.place_calls) == 1
    call = fb.place_calls[0]
    assert call["action"] == "buy" and call["qty"] == 3
    # SL is a protective stop BELOW the close (stop-market), TP above, both fill-relative
    assert call["sl"] == 20025 - 30 and call["tp"] == 20025 + 75
    assert actions[-1]["action"] == "placed" and ap._state["MNQ"].status == "ARMED"


def test_only_one_trade_per_day():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    bars = breakout_day()
    # append ANOTHER decisive breakout bar later — must NOT place a second order
    bars.append(mk(10, 10, 20030, 20060, 20025, 20055, 400))
    feed(ap, bars)
    assert len(fb.place_calls) == 1


def test_no_order_without_signal():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    # flat chop, never closes beyond the OR -> no signal
    orb = [mk(9, 30 + i, 19975, 20000, 19950, 19975, 100) for i in range(15)]
    chop = [mk(9, 50 + i, 19975, 19999, 19951, 19975, 80) for i in range(5)]
    feed(ap, orb + chop)
    assert fb.place_calls == []


def test_zero_qty_skips():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(qty_mnq=0), get_token=lambda: "tok", broker=fb)
    feed(ap, breakout_day())
    assert fb.place_calls == [] and ap._state["MNQ"].status == "DONE"


def test_rejected_entry_is_not_retried():
    fb = FakeBroker()
    fb.place_stoplimit_bracket = lambda *a, **k: {"error": "rejected"}
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    bars = breakout_day() + [mk(10, 10, 20030, 20060, 20025, 20055, 400)]
    feed(ap, bars)
    assert ap._state["MNQ"].status == "DONE"


def test_poll_detects_fill_then_close():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    feed(ap, breakout_day())
    # broker now shows a long position on MNQ (contractId 901 -> name MNQZ5)
    fb.positions = [{"accountId": 42, "netPos": 3, "netPrice": 20025, "contractId": 901}]
    out = ap.poll()
    assert any(a["action"] == "filled" for a in out) and ap._state["MNQ"].status == "LIVE"
    # TP hits -> flat again
    fb.positions = []
    fb.orders = [{"id": 777}]                      # a lingering OSO leg to sweep
    out = ap.poll()
    assert any(a["action"] == "closed" for a in out)
    assert ap._state["MNQ"].status == "DONE" and 777 in fb.cancels


def test_poll_eod_flattens_open_position():
    fb = FakeBroker()
    clock = [datetime(2026, 1, 2, 15, 56)]         # past 15:55 flatten
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb, now_et=lambda: clock[0])
    feed(ap, breakout_day())
    fb.positions = [{"accountId": 42, "netPos": 3, "netPrice": 20025, "contractId": 901}]
    fb.orders = [{"id": 888}]
    out = ap.poll()
    assert any(a["action"] == "eod" for a in out)
    assert 901 in fb.liquidations and 888 in fb.cancels
    assert ap._state["MNQ"].status == "DONE"


def test_position_root_mismatch_is_ignored():
    fb = FakeBroker()
    fb.contracts = {901: {"name": "ESZ5"}}         # a DIFFERENT instrument's position
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    feed(ap, breakout_day())
    fb.positions = [{"accountId": 42, "netPos": 5, "netPrice": 5000, "contractId": 901}]
    out = ap.poll()
    # ES position must NOT be read as our MNQ fill
    assert not any(a.get("action") == "filled" for a in out)


def test_day_rollover_rolls_atr_and_resets():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    feed(ap, breakout_day())                       # day 2026-01-02, range 80 pts (19950..20030)
    assert ap.atr["MNQ"].value() is None           # today never counts
    # a bar on the NEXT day rolls 2026-01-02's true range into the ATR and resets state
    ap.on_bar("MNQ", Bar(datetime(2026, 1, 3, 9, 30), 20000, 20000, 20000, 20000, 1))
    assert ap.atr["MNQ"].value() == 80.0
    assert ap._state["MNQ"].trading_date == "2026-01-03" and ap._state["MNQ"].status == "IDLE"


# ---- restart-proof opening-range backfill ----
def test_backfill_marks_done_if_breakout_already_fired():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    res = ap.backfill("MNQ", breakout_day())        # OR + decisive breakout already elapsed
    assert res["action"] == "backfill_missed"
    assert ap._state["MNQ"].status == "DONE"        # won't enter late on a past breakout
    assert fb.place_calls == []                     # backfill places NO order


def test_backfill_ready_then_live_breakout_trades():
    fb = FakeBroker()
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=fb)
    orb = [mk(9, 30 + i, 19975, 20000, 19950, 19975, 100) for i in range(15)]  # OR only, no breakout
    res = ap.backfill("MNQ", orb)
    assert res["action"] == "backfill_ready" and res["has_or"] is True
    assert ap._state["MNQ"].status == "IDLE"        # ready to catch a LIVE breakout
    ap.on_bar("MNQ", mk(9, 50, 20000, 20030, 19995, 20025, 300))   # live breakout after restart
    assert len(fb.place_calls) == 1                 # trades the live breakout normally


def test_backfill_empty_is_safe():
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=FakeBroker())
    assert ap.backfill("MNQ", [])["action"] == "backfill_skip"


# ---- ATR seeding from historical daily ranges ----
def test_seed_atr_fills_empty_atr():
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=FakeBroker())
    assert ap.atr["MNQ"].value() is None
    ap.seed_atr("MNQ", [400.0, 420.0, 380.0])
    assert ap.atr["MNQ"].value() == (400 + 420 + 380) / 3


def test_seed_atr_is_idempotent_and_never_overwrites():
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=FakeBroker())
    ap.seed_atr("MNQ", [400.0])
    ap.seed_atr("MNQ", [999.0])                     # second seed is a no-op (ATR already primed)
    assert ap.atr["MNQ"].value() == 400.0


def test_seed_atr_unknown_root_is_safe():
    ap = Autopilot(cfg_for(), get_token=lambda: "tok", broker=FakeBroker())
    ap.seed_atr("ZZZ", [1.0])                        # no such instrument -> no crash
