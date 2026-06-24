"""Tests for persisted daily PnL — the foundation that fixes the $0 reports /
journal / equity curve by saving day_pnl instead of re-pulling Tradovate fills."""

import os
import tempfile

os.environ["KHOMA_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ["KHOMA_DISABLE_WATCHDOG"] = "1"

from app import main as appmod  # noqa: E402

_n = [0]


def _new_user():
    _n[0] += 1
    con = appmod.db()
    cur = con.cursor()
    uid = appmod.dbmod.insert_returning_id(
        cur,
        "INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at) VALUES(?,?,?,?,?)",
        (f"p{_n[0]}@t.com", "x", f"k{_n[0]}", f"s{_n[0]}", "2026-01-01T00:00:00+00:00"),
    )
    con.commit()
    con.close()
    return uid


def _today_et():
    return appmod.datetime.now(appmod.timezone.utc).astimezone(
        appmod.ZoneInfo(appmod._ET)).strftime("%Y-%m-%d")


def test_record_daily_equity_and_upsert():
    uid = _new_user()
    acct = {"id": 50_001, "user_id": uid, "account_name": "DEMO1"}
    appmod._LAST_EQUITY_SNAP.clear()
    appmod.record_daily_equity(acct, {"ok": True, "equity": 50500.0, "day_pnl": 500.0})
    today = _today_et()
    assert appmod.daily_pnl_map(uid).get(today) == 500.0
    # Same day again (throttle bypassed) → updates in place, not a duplicate row.
    appmod._LAST_EQUITY_SNAP.clear()
    appmod.record_daily_equity(acct, {"ok": True, "equity": 50800.0, "day_pnl": 800.0})
    m = appmod.daily_pnl_map(uid)
    assert m.get(today) == 800.0
    assert len(m) == 1  # one day, one (upserted) row


def test_record_ignores_bad_state():
    uid = _new_user()
    acct = {"id": 50_002, "user_id": uid, "account_name": "DEMO2"}
    appmod._LAST_EQUITY_SNAP.clear()
    appmod.record_daily_equity(acct, {"ok": False})           # broker unreachable
    appmod.record_daily_equity(acct, {"ok": True})            # no numbers
    assert appmod.daily_pnl_map(uid) == {}


def test_period_stats_multi_day_and_multi_account():
    uid = _new_user()
    con = appmod.db()
    for d, p in [("2026-05-01", 100.0), ("2026-05-02", -40.0), ("2026-05-03", 250.0)]:
        con.execute(
            "INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,day_pnl,updated_at) "
            "VALUES(?,?,?,?,?,?)", (uid, 60_001, "A", d, p, "x"))
    con.commit()
    con.close()
    s = appmod.period_pnl_stats(uid, "2026-05-01", "2026-05-03")
    assert s["net"] == 310.0
    assert s["green_days"] == 2 and s["red_days"] == 1
    assert s["best_day"] == ("2026-05-03", 250.0)
    assert s["worst_day"] == ("2026-05-02", -40.0)
    assert s["active_days"] == 3

    # A second account on the same day → daily totals SUM across accounts.
    con = appmod.db()
    con.execute(
        "INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,day_pnl,updated_at) "
        "VALUES(?,?,?,?,?,?)", (uid, 60_002, "B", "2026-05-03", 50.0, "x"))
    con.commit()
    con.close()
    assert appmod.daily_pnl_map(uid, "2026-05-03", "2026-05-03")["2026-05-03"] == 300.0


def test_period_stats_empty_is_zero_not_crash():
    uid = _new_user()
    s = appmod.period_pnl_stats(uid, "2026-05-01", "2026-05-31")
    assert s["net"] == 0 and s["green_days"] == 0 and s["best_day"] is None


def test_apply_persisted_pnl_overrides_journal():
    # The journal calendar/net are driven by the REALIZED trade ledger (trade_log),
    # NOT the daily_equity snapshots (which include unrealized open-position P&L).
    uid = _new_user()
    appmod._ledger_persist_trips(uid, [
        {"account": "A", "_account_id": 80_001, "symbol": "MNQ", "side": "long", "qty": 1,
         "entry_price": 100, "exit_price": 200, "pnl": 100.0,
         "opened_at": "2026-05-01T14:00:00Z", "closed_at": "2026-05-01T15:00:00Z"},
        {"account": "A", "_account_id": 80_001, "symbol": "MNQ", "side": "long", "qty": 1,
         "entry_price": 100, "exit_price": 70, "pnl": -30.0,
         "opened_at": "2026-05-02T14:00:00Z", "closed_at": "2026-05-02T15:00:00Z"},
    ])
    d1 = appmod._et_day("2026-05-01T15:00:00Z")
    d2 = appmod._et_day("2026-05-02T15:00:00Z")
    s = appmod.journal_analytics([])           # empty fills → net 0, daily {}
    assert s["net"] == 0
    s = appmod.apply_persisted_pnl(s, uid, "2026-05-01", "2026-05-31")
    assert s["net"] == 70.0
    assert s["daily"] == {d1: 100.0, d2: -30.0}
    assert s["green_days"] == 1 and s["red_days"] == 1
    assert s["best_day"] == (d1, 100.0)
    assert s["equity"] == [100.0, 70.0]        # cumulative daily equity curve


def test_persisted_zero_does_not_override_live_pnl():
    # Regression: an idle/demo account's persisted $0 must NOT replace the real
    # P&L from live broker fills for a day they both cover.
    uid = _new_user()
    con = appmod.db()
    con.execute("INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,day_pnl,updated_at) "
                "VALUES(?,?,?,?,?,?)", (uid, 90_001, "A", "2026-05-10", 0.0, "x"))
    con.commit()
    con.close()
    s = {"daily": {"2026-05-10": 250.0}, "net": 250.0, "green_days": 1, "red_days": 0,
         "best_day": ("2026-05-10", 250.0), "worst_day": ("2026-05-10", 250.0)}
    s = appmod.apply_persisted_pnl(s, uid, "2026-05-01", "2026-05-31")
    assert s["daily"]["2026-05-10"] == 250.0   # live wins over persisted $0
    assert s["net"] == 250.0


def test_persisted_fills_gaps_not_covered_by_live():
    # The ledger backfills OLDER days the current live fills no longer cover (the
    # fills aged out of Tradovate's window), while live trips win for days they cover.
    uid = _new_user()
    appmod._ledger_persist_trips(uid, [
        {"account": "A", "_account_id": 90_002, "symbol": "MNQ", "side": "long", "qty": 1,
         "entry_price": 100, "exit_price": 280, "pnl": 180.0,
         "opened_at": "2026-04-01T14:00:00Z", "closed_at": "2026-04-01T15:00:00Z"},
    ])
    d_old = appmod._et_day("2026-04-01T15:00:00Z")
    s = {"daily": {"2026-05-10": 250.0}, "net": 250.0, "green_days": 1, "red_days": 0,
         "best_day": ("2026-05-10", 250.0), "worst_day": ("2026-05-10", 250.0)}
    s = appmod.apply_persisted_pnl(s, uid, "2026-04-01", "2026-05-31")
    assert s["daily"][d_old] == 180.0          # ledger fills the gap
    assert s["daily"]["2026-05-10"] == 250.0   # live kept
    assert s["net"] == 430.0


def test_ledger_wins_over_stale_equity_snapshot_in_journal():
    # One-source-of-truth: a real trade in the permanent ledger must drive the journal
    # calendar even when a stale/wrong daily_equity snapshot exists for that day and no
    # live trips were passed (e.g. the fill aged out of Tradovate's window).
    uid = _new_user()
    aid = 123_450
    con = appmod.db()
    con.execute("INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,day_pnl,updated_at) "
                "VALUES(?,?,?,?,?,?)", (uid, aid, "A", "2026-06-23", 0.0, "x"))  # stale $0 snapshot
    con.commit(); con.close()
    appmod._ledger_persist_trips(uid, [{
        "account": "A", "_account_id": aid, "symbol": "MNQ", "side": "long", "qty": 2,
        "entry_price": 29986.25, "exit_price": 29852.25, "pnl": -536.0,
        "opened_at": "2026-06-23T14:00:00Z", "closed_at": "2026-06-23T15:00:01Z"}])
    s = appmod.journal_analytics([])                       # no live trips passed
    s = appmod.apply_persisted_pnl(s, uid, "2026-06-01", "2026-06-30")
    day = appmod._et_day("2026-06-23T15:00:01Z")
    assert s["daily"][day] == -536.0                       # ledger beats the stale $0
    assert s["net"] == -536.0


def test_apply_persisted_pnl_noop_when_no_snapshots():
    uid = _new_user()
    s = appmod.journal_analytics([])
    s2 = appmod.apply_persisted_pnl(s, uid, None, None)
    assert s2["net"] == 0 and s2["daily"] == {}


def test_daily_pnl_map_scoped_to_account():
    uid = _new_user()
    con = appmod.db()
    con.execute("INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,day_pnl,updated_at) VALUES(?,?,?,?,?,?)",
                (uid, 70_001, "A", "2026-05-10", 200.0, "x"))
    con.execute("INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,day_pnl,updated_at) VALUES(?,?,?,?,?,?)",
                (uid, 70_002, "B", "2026-05-10", 75.0, "x"))
    con.commit()
    con.close()
    assert appmod.daily_pnl_map(uid)["2026-05-10"] == 275.0
    assert appmod.daily_pnl_map(uid, only_account_id=70_001)["2026-05-10"] == 200.0
