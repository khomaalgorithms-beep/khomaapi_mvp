"""MultiAutopilot: ORB + prior-day break (resting stop entries) and 'both' (ORB + 10:30 momentum) on one account."""
from datetime import datetime, timedelta

from app import orb_momentum as MOM
from app.orb_selective import Bar
from app.orb_live import AccountConfig
from app.orb_multi import MultiAutopilot, PENDING, LIVE, DONE
from tests.test_orb_live import FakeBroker

D = "2026-10-13"   # a normal Tuesday


def bar(h, m, o, hi, lo, c, day=13):
    return Bar(datetime(2026, 10, day, h, m), o, hi, lo, c, 100.0)


class MB(FakeBroker):
    """Fake broker with resting stop entries, order statuses, fills and stop modification."""
    def __init__(self, balance=None):
        super().__init__()
        self.balance = balance
        self.stop_entries = []
        self.mods = []
        self.fills = []
        self.contracts = {901: {"name": "MNQZ6"}}

    def get_cash_snapshot(self, env, token, acct):
        return {"totalCashValue": self.balance} if self.balance is not None else {}

    def place_stop_entry_bracket(self, env, token, spec, acct, action, symbol, qty, entry_stop, stop_price, limit_price=None):
        oid = self.next_order_id; self.next_order_id += 3
        self.stop_entries.append(dict(action=action, qty=qty, entry=entry_stop, sl=stop_price, tp=limit_price, oid=oid))
        self.orders.append({"id": oid, "accountId": 42, "ordStatus": "Working", "action": action.capitalize(), "contractId": 901})
        return {"orderId": oid, "oso1Id": oid + 1, "oso2Id": (oid + 2) if limit_price is not None else None}

    def place_bracket_order(self, env, token, spec, acct, action, symbol, qty, stop_price=None, limit_price=None):
        r = super().place_bracket_order(env, token, spec, acct, action, symbol, qty, stop_price, limit_price)
        oid = r["orderId"]
        self.orders.append({"id": oid, "accountId": 42, "ordStatus": "Working", "action": action.capitalize(), "contractId": 901})
        return {"orderId": oid, "oso1Id": oid + 1000, "oso2Id": (oid + 2000) if limit_price is not None else None}

    def fill(self, oid, price):
        for o in self.orders:
            if o["id"] == oid:
                o["ordStatus"] = "Filled"
        self.fills.append({"orderId": oid, "price": price, "qty": 1})

    def set_status(self, oid, status):
        found = False
        for o in self.orders:
            if o["id"] == oid:
                o["ordStatus"] = status; found = True
        if not found:
            self.orders.append({"id": oid, "accountId": 42, "ordStatus": status, "contractId": 901})

    def get_fills(self, env, token):
        return self.fills

    def modify_stop_price(self, env, token, oid, new_stop, qty=None):
        if not qty:                     # real Tradovate rejects a modify without orderQty
            return {"failureReason": "orderQty required"}
        self.mods.append((oid, new_stop)); return {"ok": True}


def cfg(strategy="orb", mode="funded", risk=240.0, tp_r=None, day_tp=None):
    return AccountConfig(env="demo", account_spec="LFE1", account_id=42, account_name="LFE1",
                         instruments={"MNQ": "MNQZ6"}, qty={"MNQ": 0}, strategy=strategy, mode=mode,
                         risk_usd=risk, max_micros=40, acct_start=50000, acct_target=(3000 if mode == "eval" else 0),
                         day_tp_usd=day_tp, tp_r=tp_r, min_micros=1)


def ctx(pdh=20010.0, pdl=19900.0, trend=1):
    return MOM.DayContext(D, 300.0, trend, pdh, pdl)


def opening(hi=20020.0, lo=19990.0):
    """09:30-09:34 bars spanning [lo, hi]."""
    out = [bar(9, 30, 20000, hi, 19995, 20005)]
    for m in range(31, 35):
        out.append(bar(9, m, 20005, 20010, lo, 20000))
    return out


def run(ap, bars):
    out = []
    for b in bars:
        r = ap.on_bar("MNQ", b)
        if r:
            out.append(r)
    return out


def mk(strategy="orb", **k):
    fb = MB(balance=50000)
    c = k.pop("context", ctx())
    ap = MultiAutopilot(cfg(strategy, **k), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: c)
    return ap, fb


def test_places_only_the_side_beyond_the_prior_day_level():
    ap, fb = mk()                                   # range 19990..20020, PDH 20010 -> long valid; PDL 19900 -> short not
    acts = run(ap, opening())
    assert acts[-1]["action"] == "placed" and [e["action"] for e in fb.stop_entries] == ["buy"]
    e = fb.stop_entries[0]
    assert e["entry"] == 20020.25 and e["sl"] == 20020.25 - 60.0 and e["tp"] is None   # stop 0.2 x ATR(300)


def test_no_trade_when_neither_side_is_beyond_the_prior_day():
    ap, fb = mk(context=ctx(pdh=20100.0, pdl=19800.0))
    acts = run(ap, opening())
    assert acts[-1]["reason"] == "not_beyond_prior_day" and fb.stop_entries == []


def test_both_sides_valid_places_two_and_first_fill_cancels_the_other():
    ap, fb = mk(context=ctx(pdh=20000.0, pdl=20000.0))
    run(ap, opening())
    assert [e["action"] for e in fb.stop_entries] == ["buy", "sell"]
    long_oid, short_oid = fb.stop_entries[0]["oid"], fb.stop_entries[1]["oid"]
    fb.fill(long_oid, 20020.5)
    out = ap.poll()
    assert out[0]["action"] == "filled" and out[0]["entry"] == 20020.5 and short_oid in fb.cancels
    assert [l.status for l in ap.legs["MNQ"]] == [LIVE, DONE]


def test_breakeven_moves_the_stop_to_the_fill_after_plus_one_r():
    ap, fb = mk()
    run(ap, opening())
    oid = fb.stop_entries[0]["oid"]
    fb.fill(oid, 20020.5); ap.poll()
    ap.on_bar("MNQ", bar(9, 40, 20030, 20070, 20025, 20060))       # +49.5 < 1R (60)
    assert fb.mods == []
    r = ap.on_bar("MNQ", bar(9, 41, 20060, 20081, 20055, 20075))    # +60.5 >= 1R
    assert r["action"] == "breakeven" and fb.mods == [(oid + 1, 20020.5)]
    ap.on_bar("MNQ", bar(9, 42, 20075, 20100, 20070, 20090))
    assert len(fb.mods) == 1                                         # only once


def test_close_detected_from_the_legs_own_stop_order():
    ap, fb = mk()
    run(ap, opening())
    oid = fb.stop_entries[0]["oid"]
    fb.fill(oid, 20020.5); ap.poll()
    fb.set_status(oid + 1, "Filled")
    out = ap.poll()
    assert out[-1]["action"] == "closed" and out[-1]["exit"] == "stop" and ap.legs["MNQ"][0].status == DONE


def test_other_side_breaking_first_cancels_the_resting_entry():
    ap, fb = mk()                                   # only the long is valid
    run(ap, opening())
    r = ap.on_bar("MNQ", bar(9, 36, 19995, 19998, 19985, 19988))    # breaks the LOW first
    assert r["reason"] == "other_side_broke_first" and fb.stop_entries[0]["oid"] in fb.cancels


def test_unfilled_entry_is_cancelled_after_the_1034_bar():
    ap, fb = mk()
    bars = opening() + [bar(9, m, 20000, 20010, 19995, 20005) for m in range(35, 60)] + \
        [bar(10, m, 20000, 20010, 19995, 20005) for m in range(0, 35)]
    acts = run(ap, bars)
    assert acts[-1]["reason"] == "no_break" and fb.stop_entries[0]["oid"] in fb.cancels


def test_holiday_is_skipped():
    fb = MB(balance=50000)
    ap = MultiAutopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: ctx())
    acts = run(ap, [Bar(datetime(2026, 11, 27, 9, m), 20000, 20020, 19990, 20000, 1.0) for m in range(30, 35)])
    assert acts[-1]["reason"] == "holiday_or_half_day" and fb.stop_entries == []


def test_eval_take_profit_on_the_orb_leg_uses_the_dollars_still_needed():
    fb = MB(balance=52600)                          # $400 left
    ap = MultiAutopilot(cfg(mode="eval", risk=600, day_tp=1440), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: ctx())
    run(ap, opening())
    e = fb.stop_entries[0]
    assert e["qty"] == 5 and abs(e["tp"] - (20020.25 + 400 / 10.0)) < 0.26


def test_funded_safe_target_is_two_times_the_stop():
    ap, fb = mk(tp_r=2.0)
    run(ap, opening())
    assert fb.stop_entries[0]["tp"] == 20020.25 + 120.0


def test_both_mode_runs_the_1030_leg_independently():
    ap, fb = mk(strategy="both", context=ctx(trend=1))
    bars = opening() + [bar(9, m, 20000, 20010, 19995, 20005) for m in range(35, 60)] + \
        [bar(10, m, 20000, 20010, 19995, 20005) for m in range(0, 29)] + [bar(10, 29, 20030, 20041, 20029, 20040)]
    acts = run(ap, bars)
    assert len(fb.place_calls) == 1 and fb.place_calls[0]["action"] == "buy"          # momentum market entry
    names = [l.name for l in ap.legs["MNQ"]]
    assert names.count("orb") == 1 and names.count("mom") == 1
    mom = [l for l in ap.legs["MNQ"] if l.name == "mom"][0]
    fb.fill(mom.entry_oid, 20040.25)
    out = ap.poll()
    assert any(o["action"] == "filled" and o["strategy"] == "mom" for o in out)
    orb = [l for l in ap.legs["MNQ"] if l.name == "orb"][0]
    assert orb.status in (PENDING, DONE)                                             # untouched by the mom fill


def test_restart_after_0935_never_places_late_and_cancels_stale_entries_when_flat():
    ap, fb = mk()
    fb.orders = [{"id": 77, "accountId": 42, "ordStatus": "Working", "contractId": 901}]
    res = ap.backfill("MNQ", opening() + [bar(9, 40, 20000, 20010, 19995, 20005)])
    assert res["orb"] == "missed" and 77 in fb.cancels
    ap.on_bar("MNQ", bar(9, 41, 20000, 20030, 19995, 20025))
    assert fb.stop_entries == []


def test_unknown_outcome_is_adopted_from_the_broker_order_list():
    class Amb(MB):
        def place_stop_entry_bracket(self, *a, **k):
            r = super().place_stop_entry_bracket(*a, **k)
            self.orders[-1]["timestamp"] = (datetime.utcnow()).isoformat()
            return {"error": "timeout after send"}
    fb = Amb(balance=50000)
    ap = MultiAutopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: ctx())
    run(ap, opening())
    leg = ap.legs["MNQ"][0]
    assert leg.entry_oid is None
    ap.poll()
    assert leg.entry_oid == fb.stop_entries[0]["oid"]


def test_eod_flattens_and_closes_every_leg():
    fb = MB(balance=50000)
    clock = [datetime(2026, 10, 13, 9, 35)]
    ap = MultiAutopilot(cfg(), get_token=lambda: "tok", broker=fb, now_et=lambda: clock[0], get_context=lambda r, d: ctx())
    run(ap, opening())
    fb.fill(fb.stop_entries[0]["oid"], 20020.5); ap.poll()
    fb.positions = [{"accountId": 42, "netPos": 4, "contractId": 901, "netPrice": 20020.5}]
    clock[0] = datetime(2026, 10, 13, 15, 59)
    ap.poll()
    assert fb.liquidations == [901] and all(l.status == DONE for l in ap.legs["MNQ"])


def test_risk_lock_blocks_the_orb_entry():
    fb = MB(balance=50000)
    c = cfg(); c.risk_check = lambda: "daily loss limit hit"
    ap = MultiAutopilot(c, get_token=lambda: "tok", broker=fb, get_context=lambda r, d: ctx())
    acts = run(ap, opening())
    assert acts[-1]["reason"] == "risk_locked" and fb.stop_entries == []


def test_risk_plan_uses_the_validated_tables_for_each_strategy():
    from app import orb_engine as E
    from app.orb_validated import DATA
    for size in ("25K", "50K", "100K", "150K"):
        for dd in ("eod", "intraday", "static"):
            for strat, key in (("orb", "orb"), ("both", "both")):
                for pr in ("cons", "norm", "aggr"):
                    assert E.risk_plan("eval", size, pr, dd, strat=strat)["risk_usd"] == DATA["setups"][key]["presets"][size][dd][pr]["risk"]
                f = DATA["setups"][key]["funded"][size][dd]
                assert E.risk_plan("funded", size, "safe", dd, strat=strat)["tp_r"] == 2.0
                assert E.risk_plan("funded", size, "safe2", dd, strat=strat)["risk_usd"] == f["risk_after"]
                assert E.risk_plan("funded", size, "norm", dd, strat=strat)["tp_r"] is None
    # live cash on ORB: drawdown budget / ORB's worst drawdown since 2008
    assert E.risk_plan("cash", "50K", "cons", "eod", 50000, 25, strat="orb")["risk_usd"] == round(12500 / DATA["setups"]["orb"]["cash"]["full"]["ddR"], 2)


def test_strategy_mapping_and_autopilot_class():
    from app import orb_engine as E
    from app.orb_live import Autopilot
    assert E.strategy_of("nq") == "momentum" and E.strategy_of("both") == "momentum"   # legacy NQ+RTY value
    assert E.strategy_of("orb") == "orb" and E.strategy_of("combo") == "both"
    c = cfg("both")
    assert isinstance(E._make_autopilot(c, lambda: "tok"), MultiAutopilot)
    c.strategy = "momentum"
    ap = E._make_autopilot(c, lambda: "tok")
    assert type(ap) is Autopilot


def test_keepalive_renews_every_armed_token_and_alerts_a_dead_one(monkeypatch):
    from datetime import datetime as _dt
    from app import orb_engine as E
    calls, alerts = [], []
    good = MultiAutopilot(cfg(), get_token=lambda: calls.append("a") or "good", broker=MB(), get_context=lambda r, d: ctx())
    dead = MultiAutopilot(cfg(), get_token=lambda: calls.append("b") or "expired", broker=MB(), get_context=lambda r, d: ctx())
    m = E.EngineManager.__new__(E.EngineManager)
    m._active = {1: {"autopilot": good}, 2: {"autopilot": dead}}
    monkeypatch.setattr(E, "_MANAGER", m)
    monkeypatch.setattr(E.tvo, "_get", lambda env, tok, path: [{"id": 1}] if tok == "good" else {"errorText": "Expired Access Token"})
    monkeypatch.setattr(E, "_owner_email", lambda aid: "c@x.com")
    monkeypatch.setattr(E, "_send_alert", lambda subj, html, to=None: alerts.append((subj, to)))
    E._KEEPALIVE_LAST[0] = 0.0; E._TOKEN_ALERTED.clear()
    res = E.keepalive(_dt(2026, 10, 13, 2, 0))
    assert calls == ["a", "b"] and [r["ok"] for r in res] == [True, False]
    assert [a[1] for a in alerts] == ["c@x.com", None]                       # client + operator, once
    assert E.keepalive(_dt(2026, 10, 13, 2, 1)) == []                        # throttled to every 5 min
    E._KEEPALIVE_LAST[0] = 0.0
    E.keepalive(_dt(2026, 10, 13, 2, 6))
    assert len(alerts) == 2                                                  # same day: no repeat email


def test_keepalive_also_alerts_armed_accounts_that_could_not_start(monkeypatch):
    from datetime import datetime as _dt
    from app import orb_engine as E
    alerts = []
    m = E.EngineManager.__new__(E.EngineManager); m._active = {}
    monkeypatch.setattr(E, "_MANAGER", m)
    monkeypatch.setattr(E, "_owner_email", lambda aid: None)
    monkeypatch.setattr(E, "_send_alert", lambda subj, html, to=None: alerts.append(subj))
    E._KEEPALIVE_LAST[0] = 0.0; E._TOKEN_ALERTED.clear(); E._UNBUILT.clear(); E._UNBUILT["48440214"] = "broker connection not working"
    res = E.keepalive(_dt(2026, 10, 13, 12, 0))
    assert res == [{"account_id": "48440214", "ok": False}] and len(alerts) == 1
    E._UNBUILT.clear()


def test_keepalive_uses_the_long_runway_renewal_and_preflight_skips_weekends(monkeypatch):
    from datetime import datetime as _dt
    from app import orb_engine as E
    used = []
    ap = MultiAutopilot(cfg(), get_token=lambda: used.append("normal") or "good", broker=MB(), get_context=lambda r, d: ctx())
    m = E.EngineManager.__new__(E.EngineManager)
    m._active = {1: {"autopilot": ap, "keep": lambda: used.append("keep") or "good"}}
    monkeypatch.setattr(E, "_MANAGER", m)
    monkeypatch.setattr(E.tvo, "_get", lambda env, tok, path: [{"id": 1}])
    E._KEEPALIVE_LAST[0] = 0.0; E._UNBUILT.clear()
    assert E.keepalive(_dt(2026, 10, 16, 18, 0)) == [{"account_id": 1, "ok": True}] and used == ["keep"]
    E._PREFLIGHT_DONE.discard("2026-10-17")
    assert E.preflight(_dt(2026, 10, 17, 9, 15)) == []                         # Saturday: no pre-flight, no emails
