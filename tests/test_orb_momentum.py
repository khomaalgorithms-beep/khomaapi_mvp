"""Validated KhomaVolume NQ momentum rule: signal, sizing, executor path, engine sizing/config/copy/dashboard."""
from datetime import datetime

import pytest

from app import orb_momentum as MOM
from app.orb_selective import Bar
from app.orb_live import Autopilot, AccountConfig
from tests.test_orb_live import FakeBroker

D = "2026-10-13"


def bar(hh, mm, o, h, l, c, day=13):
    return Bar(datetime(2026, 10, day, hh, mm), o, h, l, c, 100)


def session(open_px=20000.0, px_1029=20040.0, upto=(10, 29), day=13):
    """09:30 .. upto bars; the 09:30 bar opens at open_px and the 10:29 bar closes at px_1029."""
    out = []
    h, m = 9, 30
    while (h, m) <= upto:
        if (h, m) == (9, 30):
            out.append(bar(h, m, open_px, open_px + 2, open_px - 2, open_px + 1, day))
        elif (h, m) == (10, 29):
            out.append(bar(h, m, px_1029 - 1, px_1029 + 1, px_1029 - 2, px_1029, day))
        else:
            out.append(bar(h, m, open_px, open_px + 1, open_px - 1, open_px, day))
        m += 1
        if m == 60:
            h, m = h + 1, 0
    return out


CTX_UP = MOM.DayContext(D, 300.0, 1)
CTX_DN = MOM.DayContext(D, 300.0, -1)


# ---------------------------------------------------------------- pure signal
def test_context_from_prior_sessions():
    ctx = MOM.context_from([100.0] * 13 + [240.0], list(range(100, 120)) + [130], D)
    assert ctx.atr == pytest.approx((100 * 13 + 240) / 14)
    assert ctx.trend == 1 and ctx.ok
    assert not MOM.context_from([100.0] * 5, [1.0] * 25, D).ok          # too few ranges -> no ATR


def test_waits_until_1029_bar():
    st, res = MOM.decision(session(upto=(10, 28)), CTX_UP)
    assert st == "wait" and res is None


def test_long_with_uptrend():
    st, sig = MOM.decision(session(20000, 20040), CTX_UP)
    assert st == "signal" and sig.side == "long" and sig.stop_points == 60.0 and sig.ref_price == 20040


def test_against_trend_sits_out():
    st, why = MOM.decision(session(20000, 20040), CTX_DN)
    assert st == "skip" and why == "against_trend"


def test_short_with_downtrend():
    st, sig = MOM.decision(session(20000, 19950), CTX_DN)
    assert st == "signal" and sig.side == "short"


def test_no_context_fails_closed():
    assert MOM.decision(session(), None) == ("skip", "no_context")
    assert MOM.decision(session(), MOM.DayContext(D, None, 1)) == ("skip", "no_context")


def test_late_without_1029_bar_skips():
    bars = [b for b in session(upto=(10, 45)) if not (10 * 60 + 29 <= b.minute_of_day < 10 * 60 + 40)]
    assert MOM.decision(bars, CTX_UP) == ("skip", "late")


def test_micros_for_risk():
    assert MOM.micros_for_risk(600, 60, 2.0, 40) == 5            # $120/micro -> 5
    assert MOM.micros_for_risk(100, 60, 2.0, 40) == 1            # $120 <= 1.5x $100 -> 1 micro allowed
    assert MOM.micros_for_risk(50, 60, 2.0, 40) == 0             # too expensive -> no trade
    assert MOM.micros_for_risk(100000, 60, 2.0, 40) == 40        # capped at account max


# ---------------------------------------------------------------- executor
def cfg(mode="eval", risk=600.0, tp=1440, start=50000, target=3000):
    return AccountConfig(env="demo", account_spec="LFE1", account_id=42, account_name="LFE1",
                         instruments={"MNQ": "MNQZ6"}, qty={"MNQ": 0}, strategy="momentum", mode=mode,
                         risk_usd=risk, max_micros=40, acct_start=start, acct_target=target,
                         day_tp_usd=(tp if mode == "eval" else None))


class Broker(FakeBroker):
    def __init__(self, balance=None):
        super().__init__()
        self.balance = balance

    def get_cash_snapshot(self, env, token, acct):
        return {"totalCashValue": self.balance} if self.balance is not None else {}


def run_day(ap, bars):
    out = []
    for b in bars:
        r = ap.on_bar("MNQ", b)
        if r:
            out.append(r)
    return out


def test_eval_places_market_bracket_with_stop_and_daily_cap_tp():
    fb = Broker(balance=50000)
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    acts = run_day(ap, session(20000, 20040))
    assert len(fb.place_calls) == 1
    c = fb.place_calls[0]
    assert c["action"] == "buy" and c["qty"] == 5 and c["sl"] == 19980.0          # 20040 - 60
    assert c["tp"] == pytest.approx(20040 + 1440 / (5 * 2.0), abs=0.25)           # $1,440 cap
    assert acts[-1]["action"] == "placed" and ap._state["MNQ"].status == "ARMED"


def test_eval_tp_uses_dollars_still_needed_when_smaller():
    fb = Broker(balance=52600)                                                   # $400 left to the target
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    assert fb.place_calls[0]["tp"] == pytest.approx(20040 + 400 / 10.0, abs=0.25)


def test_funded_holds_to_close_without_tp():
    fb = Broker()
    ap = Autopilot(cfg(mode="funded", risk=240), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    assert fb.place_calls[0]["tp"] is None and fb.place_calls[0]["qty"] == 2


def test_short_bracket_prices():
    fb = Broker(balance=50000)
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_DN)
    run_day(ap, session(20000, 19950))
    c = fb.place_calls[0]
    assert c["action"] == "sell" and c["sl"] == 20010.0 and c["tp"] < 19950


def test_against_trend_places_nothing_and_is_done_for_the_day():
    fb = Broker()
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_DN)
    acts = run_day(ap, session(20000, 20040) + [bar(10, 31, 20100, 20101, 20099, 20100)])
    assert fb.place_calls == [] and acts[-1]["reason"] == "against_trend" and ap._state["MNQ"].status == "DONE"


def test_missing_context_sits_out():
    fb = Broker()
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: None)
    acts = run_day(ap, session())
    assert fb.place_calls == [] and acts[-1]["reason"] == "no_context"


def test_one_trade_per_day_only():
    fb = Broker(balance=50000)
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040) + [bar(10, 30 + i, 20040, 20041, 20039, 20040) for i in range(1, 9)])
    assert len(fb.place_calls) == 1


def test_rejected_entry_is_not_retried():
    class Rej(Broker):
        def place_bracket_order(self, *a, **k):
            self.place_calls.append(k)
            return {"errorText": "rejected"}
    fb = Rej(balance=50000)
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    acts = run_day(ap, session(20000, 20040) + [bar(10, 31, 20040, 20041, 20039, 20040)])
    assert len(fb.place_calls) == 1 and acts[-1]["action"] == "reject" and ap._state["MNQ"].status == "DONE"


def test_restart_after_decision_does_not_enter_late():
    fb = Broker()
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    res = ap.backfill("MNQ", session(upto=(10, 35)))
    assert res["action"] == "backfill_missed" and ap._state["MNQ"].status == "DONE"
    ap.on_bar("MNQ", bar(10, 36, 20040, 20041, 20039, 20040))
    assert fb.place_calls == []


def test_restart_before_decision_then_trades_live():
    fb = Broker(balance=50000)
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    assert ap.backfill("MNQ", session(upto=(10, 10)))["action"] == "backfill_ready"
    run_day(ap, [b for b in session(20000, 20040) if b.minute_of_day > 10 * 60 + 10])
    assert len(fb.place_calls) == 1


def test_eod_flatten_at_1559():
    fb = Broker(balance=50000)
    clock = [datetime(2026, 10, 13, 10, 31)]
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, now_et=lambda: clock[0], get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    fb.positions = [{"accountId": 42, "netPos": 5, "contractId": 901, "netPrice": 20040.0}]
    fb.contracts = {901: {"name": "MNQZ6"}}
    assert any(a["action"] == "filled" for a in ap.poll())
    clock[0] = datetime(2026, 10, 13, 15, 58)
    ap.poll()
    assert fb.liquidations == []
    clock[0] = datetime(2026, 10, 13, 15, 59)
    ap.poll()
    assert fb.liquidations == [901] and ap._state["MNQ"].status == "DONE"


# ---------------------------------------------------------------- engine glue (no DB)
def test_risk_plan_matches_validated_tables():
    from app import orb_engine as E
    from app.orb_validated import DATA
    for size in ("25K", "50K", "100K", "150K"):
        for dd in ("eod", "intraday", "static"):
            for pr in ("cons", "norm", "aggr"):
                p = E.risk_plan("eval", size, pr, dd)
                assert p["risk_usd"] == DATA["presets"][size][dd][pr]["risk"] > 0
                assert p["day_tp_usd"] == round(0.48 * E.ACCT[size]["target"])
            f = E.risk_plan("funded", size, "norm", dd)
            assert f["day_tp_usd"] is None and f["risk_usd"] == DATA["funded"][size][dd]["risk"]
    assert E.risk_plan("cash", "50K", "norm", "eod", 40000, 10)["risk_usd"] == 1400


def test_every_asset_value_trades_mnq_only():
    from app import orb_engine as E
    assert E._ASSET_ROOTS["both"] == ["MNQ"] and E._ASSET_ROOTS["rty"] == ["MNQ"]


def test_copy_rows_inherit_drawdown_type_and_preset():
    from app import orb_engine as E
    rows = E._copy_config_rows(
        [{"account_pk": 9, "user_id": 1, "account_name": "C1", "env": "live", "account_id": "77"}],
        {1: {"mode": "eval", "acct_size": "50K", "preset": "cons", "asset": "nq", "dd_type": "intraday",
             "cash_bal": 25000, "cash_dd": 15}})
    assert rows[0]["dd_type"] == "intraday" and rows[0]["preset"] == "cons" and rows[0]["source"] == "copy"


def test_build_account_config_sets_validated_rule(monkeypatch):
    from app import orb_engine as E
    monkeypatch.setattr(E.tvo, "resolve_contract", lambda env, tok, root: "MNQZ6")
    row = {"env": "demo", "asset": "both", "mode": "eval", "acct_size": "50K", "preset": "norm",
           "dd_type": "eod", "account_name": "LFE1", "account_id": "42"}
    c = E.build_account_config(row, "tok")
    assert c.strategy == "momentum" and c.instruments == {"MNQ": "MNQZ6"} and c.flatten_min == 959
    assert c.risk_usd == E.risk_plan("eval", "50K", "norm", "eod")["risk_usd"] and c.day_tp_usd == 1440


def test_dashboard_renders_with_validated_data():
    from app import orb_engine as E
    html = E._render_dashboard([{"account_id": "42", "account_name": "LFE1", "env": "demo"}],
                               {"42": {"enabled": 1, "mode": "funded", "acct_size": "100K", "preset": "aggr",
                                       "dd_type": "static", "cash_bal": 30000, "cash_dd": 12}})
    assert "__DATA__" not in html and "__ACCOUNTS__" not in html and '"presets"' in html
    assert '"dd_type": "static"' in html and "Drawdown type" in html


def test_save_config_rejects_bad_values():
    from app import orb_engine as E
    with pytest.raises(AssertionError):
        E.save_config(1, 1, "1", enabled=True, mode="eval", acct_size="50K", preset="norm", asset="nq", dd_type="weekly")


def test_save_config_roundtrip_sqlite(tmp_path, monkeypatch):
    """Real DB path: ensure_table adds the new columns, save + read back dd_type/cash, and the legacy fallback
    keeps Start working on a table without the new columns."""
    import sqlite3
    from app import orb_engine as E
    dbf = tmp_path / "t.db"

    def mkcon():
        c = sqlite3.connect(dbf)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(E, "_db", mkcon)
    E.ensure_table()
    E.save_config(1, 5, "77", enabled=True, mode="funded", acct_size="100K", preset="cons", asset="nq",
                  dd_type="intraday", cash_bal=30000, cash_dd=12)
    row = E.get_config(1, "77")
    assert row["dd_type"] == "intraday" and row["mode"] == "funded" and row["enabled"] == 1
    # legacy table (no new columns) -> fallback path still saves
    dbf2 = tmp_path / "legacy.db"
    c = sqlite3.connect(dbf2)
    c.execute("""CREATE TABLE orb_autopilot(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                 account_pk INTEGER NOT NULL, account_id TEXT NOT NULL, enabled INTEGER DEFAULT 0, mode TEXT,
                 acct_size TEXT, preset TEXT, asset TEXT, updated_at TEXT, UNIQUE(user_id, account_id))""")
    c.commit(); c.close()

    def mk2():
        c2 = sqlite3.connect(dbf2); c2.row_factory = sqlite3.Row; return c2
    monkeypatch.setattr(E, "_db", mk2)
    E.save_config(1, 5, "88", enabled=True, mode="eval", acct_size="50K", preset="norm", asset="nq", dd_type="eod")
    assert E.get_config(1, "88")["enabled"] == 1


def test_trade_email_shows_momentum_plan():
    from app import orb_engine as E
    subj, head, html = E._trade_email("LFE1", {"root": "MNQ", "side": "long", "qty": 5, "entry": 20040.25,
                                              "plan": {"sl": 19978.75, "tp": None, "qty": 5, "side": "long"}})
    assert "19,978.75" in html and "Held to the close" in html and "None" not in html
    assert "LONG MNQ" in subj
    _, _, html2 = E._trade_email("LFE1", {"root": "MNQ", "side": "short", "qty": 4, "entry": 20000,
                                          "plan": {"sl": 20060, "tp": 19820, "qty": 4, "side": "short"}})
    assert "19,820" in html2 and "20,060" in html2


def test_risk_engine_lock_blocks_new_entry():
    fb = Broker(balance=50000)
    c = cfg(); c.risk_check = lambda: "Daily loss limit hit"
    ap = Autopilot(c, get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    acts = run_day(ap, session(20000, 20040))
    assert fb.place_calls == [] and acts[-1]["reason"] == "risk_locked"


def test_risk_check_error_fails_closed():
    fb = Broker(balance=50000)
    c = cfg()
    def boom():
        raise RuntimeError("db down")
    c.risk_check = boom
    ap = Autopilot(c, get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    assert fb.place_calls == []


def test_risk_check_ok_trades():
    fb = Broker(balance=50000)
    c = cfg(); c.risk_check = lambda: ""
    ap = Autopilot(c, get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    assert len(fb.place_calls) == 1


def test_backfill_merges_when_a_live_bar_arrived_first():
    """Restart at 10:03: a live 10:03 bar reaches the autopilot before the REST backfill. The backfill must still
    load the 09:30 open so the 10:30 decision can trade."""
    fb = Broker(balance=50000)
    ap = Autopilot(cfg(), get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    ap.on_bar("MNQ", bar(10, 3, 20010, 20011, 20009, 20010))            # live bar first
    res = ap.backfill("MNQ", session(20000, 20040, upto=(10, 2)))        # history 09:30..10:02
    assert res["action"] == "backfill_ready" and res["has_open"] is True
    run_day(ap, [b for b in session(20000, 20040) if b.minute_of_day >= 10 * 60 + 4])
    assert len(fb.place_calls) == 1 and fb.place_calls[0]["action"] == "buy"


# ---- Funded SAFE mode ---------------------------------------------------------------------------
def test_funded_safe_places_take_profit_at_two_times_the_stop():
    fb = Broker()
    c = cfg(mode="funded", risk=240); c.tp_r = 2.0; c.min_micros = 1
    ap = Autopilot(c, get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    call = fb.place_calls[0]
    assert call["qty"] == 2 and call["sl"] == 19980.0 and call["tp"] == pytest.approx(20040 + 120, abs=0.25)


def test_funded_min_micros_floor_trades_one_micro_when_risk_is_below_one_stop():
    fb = Broker()
    c = cfg(mode="funded", risk=60); c.tp_r = 2.0; c.min_micros = 1      # 25K Safe after first payout
    ap = Autopilot(c, get_token=lambda: "tok", broker=fb, get_context=lambda r, d: CTX_UP)
    run_day(ap, session(20000, 20040))
    assert fb.place_calls[0]["qty"] == 1


def test_risk_plan_funded_safe_and_after_first_payout():
    from app import orb_engine as E
    from app.orb_validated import DATA
    for size, mll in (("25K", 1000), ("50K", 2000), ("100K", 3000), ("150K", 4500)):
        for dd in ("eod", "intraday", "static"):
            s1 = E.risk_plan("funded", size, "safe", dd); s2 = E.risk_plan("funded", size, "safe2", dd)
            assert s1["risk_usd"] == round(0.12 * mll) and s2["risk_usd"] == round(0.06 * mll)
            assert s1["tp_r"] == 2.0 == s2["tp_r"] and s1["day_tp_usd"] is None
            assert E.risk_plan("funded", size, "norm", dd)["tp_r"] is None             # Standard still holds to close
            assert DATA["funded"][size][dd]["safe"]["paid_full"] > DATA["funded"][size][dd]["p1_full"]


def test_build_account_config_funded_safe(monkeypatch):
    from app import orb_engine as E
    monkeypatch.setattr(E.tvo, "resolve_contract", lambda env, tok, root: "MNQZ6")
    row = {"env": "demo", "asset": "nq", "mode": "funded", "acct_size": "50K", "preset": "safe2",
           "dd_type": "eod", "account_name": "LFF1", "account_id": "43"}
    c = E.build_account_config(row, "tok")
    assert c.mode == "funded" and c.risk_usd == 120 and c.tp_r == 2.0 and c.min_micros == 1 and c.day_tp_usd is None


def test_safe_presets_only_valid_in_funded_mode():
    from app import orb_engine as E
    with pytest.raises(AssertionError):
        E.save_config(1, 1, "1", enabled=True, mode="eval", acct_size="50K", preset="safe", asset="nq", dd_type="eod")


def test_dashboard_renders_funded_safe():
    from app import orb_engine as E
    html = E._render_dashboard([{"account_id": "43", "account_name": "LFF1", "env": "demo"}],
                               {"43": {"enabled": 1, "mode": "funded", "acct_size": "50K", "preset": "safe",
                                       "dd_type": "eod", "cash_bal": 25000, "cash_dd": 15}})
    assert '"safe": {' in html and "Safe (recommended)" in html and "First payout" in html
