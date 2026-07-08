"""Permanent per-trade logging that feeds the live 'Verified via Tradovate'
section: each closed trade is saved once (deduped) and read back for the feed."""

import os
import tempfile

os.environ["KHOMA_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ["KHOMA_DISABLE_WATCHDOG"] = "1"

from app import main as appmod  # noqa: E402

_n = [0]


def _user():
    _n[0] += 1
    con = appmod.db()
    cur = con.cursor()
    uid = appmod.dbmod.insert_returning_id(
        cur, "INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at) VALUES(?,?,?,?,?)",
        (f"tl{_n[0]}@t.com", "x", f"tlk{_n[0]}", f"tls{_n[0]}", "2026-01-01T00:00:00+00:00"))
    con.commit()
    con.close()
    return uid


def _connect(uid, name, aid=None):
    """Register a CONNECTED broker account so realized P&L for `name` is in scope — the
    journal/ledger now show only accounts still connected to KhomaAPI (disconnected ones
    drop out). Name is what the scoping keys on (stable across reconnects)."""
    con = appmod.db()
    con.execute("INSERT INTO broker_accounts(user_id,broker,env,account_id,account_name,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (uid, "tradovate", "demo", str(aid if aid is not None else name), name, "connected", "x", "x"))
    con.commit(); con.close()


def test_ledger_persist_and_merge_unions_history():
    # The fix: realized P&L survives Tradovate's short fill window because every
    # trip is persisted and account_trade_history returns ledger ∪ live fills.
    uid = _user()
    _connect(uid, "A")
    old = {"account": "A", "_account_id": 44, "symbol": "MNQ", "side": "long", "qty": 2,
           "entry_price": 100, "exit_price": 90, "pnl": -40.0,
           "opened_at": "2026-06-01T10:00:00Z", "closed_at": "2026-06-01T10:30:00Z"}
    appmod._ledger_persist_trips(uid, [old])
    appmod._ledger_persist_trips(uid, [old])          # idempotent
    # A fresh fill Tradovate still returns today:
    new = {"account": "A", "_account_id": 44, "symbol": "MNQ", "side": "short", "qty": 2,
           "entry_price": 120, "exit_price": 110, "pnl": 40.0,
           "opened_at": "2026-06-24T10:00:00Z", "closed_at": "2026-06-24T10:30:00Z"}
    merged = appmod._ledger_merge(uid, [new])
    assert len(merged) == 2                            # old (ledger) + new (live), no dupes
    assert round(sum(t["pnl"] for t in merged), 2) == 0.0
    # Account scoping holds.
    assert len(appmod._ledger_merge(uid, [], only_account_id=44)) == 1
    assert len(appmod._ledger_merge(uid, [], only_account_id=999)) == 0


def test_name_drift_does_not_duplicate_or_double_count():
    # The real bug: trip_key includes the account NAME, which drifts to empty as a
    # trip round-trips through the ledger. Persisting the "same" trade once with a
    # name and once without must NOT create a 2nd row or double the P&L.
    uid = _user()
    _connect(uid, "DEMO856420")
    aid = 990044
    base = {"_account_id": aid, "symbol": "MNQU6", "side": "long", "qty": 2,
            "entry_price": 29986.25, "exit_price": 29852.25, "pnl": -536.0,
            "opened_at": "2026-06-23T14:30:00Z", "closed_at": "2026-06-23T15:00:01Z"}
    appmod._ledger_persist_trips(uid, [{**base, "account": "DEMO856420"}])  # with name
    appmod._ledger_persist_trips(uid, [{**base, "account": ""}])            # name drifted
    appmod._ledger_persist_trips(uid, [{**base, "account": None}])          # name None
    # Exactly one row; calendar + trade list + merge all show a single -536.
    assert len(appmod._track_live_trades([aid])) == 1
    assert appmod.public_daily_map([aid])[appmod._et_day(base["closed_at"])] == -536.0
    assert len(appmod._ledger_merge(uid, [], only_account_id=aid)) == 1
    assert appmod._trade_stats(appmod._track_live_trades([aid]))["net"] == -536.0


def test_public_daily_map_built_from_trade_ledger():
    # Regression: the verified calendar showed $0 on a real trade day because it read
    # the daily_equity snapshots (which were $0). It must be built from the trade
    # ledger so the calendar ALWAYS matches the trade history. Keyed by ET date.
    uid = _user()
    aid = 990045
    appmod._ledger_persist_trips(uid, [{
        "account": "DEMO", "_account_id": aid, "symbol": "MNQ", "side": "long", "qty": 2,
        "entry_price": 29986.25, "exit_price": 29852.25, "pnl": -536.0,
        "opened_at": "2026-06-23T14:00:00Z", "closed_at": "2026-06-23T14:30:00Z"}])
    m = appmod.public_daily_map([aid])
    day = appmod._et_day("2026-06-23T14:30:00Z")      # ET trade-close date
    assert m.get(day) == -536.0                        # ledger drives the calendar
    assert appmod.public_daily_map([999]) == {}        # scoped to the chosen account


def test_et_day_converts_to_eastern():
    # 02:30 UTC is still the previous evening in ET.
    assert appmod._et_day("2026-06-24T02:30:00Z") == "2026-06-23"
    assert appmod._et_day("2026-06-23T14:30:00Z") == "2026-06-23"
    assert appmod._et_day("") == "" and appmod._et_day("garbage")[:4] == "garb"


def test_ledger_merge_dedups_across_reconnect_account_id_change():
    # Reconnect rotates broker_accounts.id (44 -> 55) but the Tradovate account NAME
    # is stable. A trade still in the live fills window after a reconnect must NOT be
    # double-counted just because its row id changed.
    uid = _user()
    _connect(uid, "DEMO856420")
    appmod._ledger_persist_trips(uid, [{
        "account": "DEMO856420", "_account_id": 44, "symbol": "MNQ", "side": "long", "qty": 2,
        "entry_price": 100, "exit_price": 90, "pnl": -40.0,
        "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:00:00Z"}])
    live = [{   # same physical trade, re-read live after reconnect: NEW id, SAME name
        "account": "DEMO856420", "_account_id": 55, "symbol": "MNQ", "side": "long", "qty": 2,
        "entry_price": 100, "exit_price": 90, "pnl": -40.0,
        "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:00:00Z"}]
    merged = appmod._ledger_merge(uid, live)
    assert len(merged) == 1                              # not double-counted
    assert round(sum(t["pnl"] for t in merged), 2) == -40.0


def test_ledger_merge_keeps_distinct_accounts_separate():
    # Copy trading: the SAME signal on TWO different accounts (different names) must
    # stay as two separate trades — never collapsed by the dedup.
    uid = _user()
    _connect(uid, "ACCT-A")
    _connect(uid, "ACCT-B")
    live = [
        {"account": "ACCT-A", "_account_id": 70, "symbol": "MNQ", "side": "long", "qty": 1,
         "entry_price": 100, "exit_price": 110, "pnl": 10.0,
         "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:00:00Z"},
        {"account": "ACCT-B", "_account_id": 71, "symbol": "MNQ", "side": "long", "qty": 1,
         "entry_price": 100, "exit_price": 110, "pnl": 10.0,
         "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:00:00Z"},
    ]
    merged = appmod._ledger_merge(uid, live)
    assert len(merged) == 2                              # distinct accounts not collapsed
    assert round(sum(t["pnl"] for t in merged), 2) == 20.0


def test_trade_stats_math():
    s = appmod._trade_stats([{"pnl": 100}, {"pnl": -40}, {"pnl": 60}])
    assert s["net"] == 120 and s["trades"] == 3 and s["wins"] == 2 and s["losses"] == 1
    assert s["pf_disp"] == "4.00"            # (100+60) / 40


def test_account_history_falls_back_to_ledger_when_broker_read_throws(monkeypatch):
    # The journal/dashboard $0 bug: a broker hiccup made account_trade_history raise,
    # the caller swallowed it to [], and the journal went blank even though the
    # permanent ledger held the trade. Now a broker failure must STILL return the
    # ledger so realized P&L never disappears.
    uid = _user()
    aid = 990077
    appmod._ledger_persist_trips(uid, [{
        "account": "DEMO", "_account_id": aid, "symbol": "MNQU6", "side": "long", "qty": 2,
        "entry_price": 29986.25, "exit_price": 29852.25, "pnl": -536.0,
        "opened_at": "2026-06-23T14:00:00Z", "closed_at": "2026-06-23T15:00:01Z"}])

    # Simulate a connected account whose live read explodes.
    monkeypatch.setattr(appmod, "get_broker_accounts",
                        lambda user_id, connected_only=False: [
                            {"id": aid, "env": "demo", "account_id": "48440214",
                             "account_name": "DEMO", "access_token_enc": "x"}])
    def boom(_a):
        raise RuntimeError("token renew failed / broker down")
    monkeypatch.setattr(appmod, "ensure_fresh_token", boom)

    trips, _open = appmod.account_trade_history(uid, only_account_id=aid)
    assert len(trips) == 1                         # ledger preserved, not blanked
    assert trips[0]["pnl"] == -536.0
    s = appmod.journal_analytics(trips)
    assert s["net"] == -536.0 and s["gross_loss"] == 536.0   # analytics populated


def test_persist_track_trades_dedup(monkeypatch):
    uid = _user()
    monkeypatch.setattr(appmod, "_public_track_user", lambda: {"id": uid})
    monkeypatch.setattr(appmod, "_public_connected_ids", lambda u: [777])
    trips = [
        {"account": "A", "symbol": "MNQ", "side": "Long", "qty": 2, "entry_price": 100,
         "exit_price": 110, "pnl": 20, "opened_at": "2026-06-01T10:00", "closed_at": "2026-06-01T10:30"},
        {"account": "A", "symbol": "MNQ", "side": "Short", "qty": 2, "entry_price": 120,
         "exit_price": 115, "pnl": 10, "opened_at": "2026-06-02T10:00", "closed_at": "2026-06-02T10:30"},
    ]
    monkeypatch.setattr(appmod, "account_trade_history", lambda u, only_account_id=None: (trips, []))

    appmod.persist_track_trades()
    appmod.persist_track_trades()          # run twice → must NOT duplicate

    rows = appmod._track_live_trades([777])
    assert len(rows) == 2                   # deduped by trip_key
    s = appmod._trade_stats(rows)
    assert s["net"] == 30 and s["trades"] == 2 and s["wins"] == 2

    # A new trade later → appended, old ones untouched.
    trips.append({"account": "A", "symbol": "MNQ", "side": "Long", "qty": 2, "entry_price": 130,
                  "exit_price": 125, "pnl": -10, "opened_at": "2026-06-03T10:00", "closed_at": "2026-06-03T10:30"})
    appmod.persist_track_trades()
    rows = appmod._track_live_trades([777])
    assert len(rows) == 3


def test_persist_all_account_trades_covers_every_connected_user(monkeypatch):
    # The client-$0 fix: a server-side sweep must capture EVERY connected user's trips
    # into the ledger (not just the public-track account), so each client sees their own
    # realized P&L even with no dashboard open. account_trade_history persists as a side
    # effect; here we assert the sweep drives it for every distinct connected user_id.
    u1, u2 = _user(), _user()
    a1, a2 = 51001, 51002
    con = appmod.db()
    for uid, aid in ((u1, a1), (u2, a2)):
        con.execute("INSERT INTO broker_accounts(user_id,broker,env,account_id,account_name,status,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (uid, "tradovate", "demo", str(aid), f"ACC{aid}", "connected", "x", "x"))
    con.commit(); con.close()

    real = appmod.account_trade_history
    def fake(uid, only_account_id=None):
        # Each user has one closed trip; persist it the way the real function does.
        aid = a1 if uid == u1 else a2
        trip = {"account": f"ACC{aid}", "_account_id": aid, "symbol": "MNQ", "side": "long",
                "qty": 2, "entry_price": 100, "exit_price": 95, "pnl": -20.0,
                "opened_at": "2026-06-23T14:00:00Z", "closed_at": "2026-06-23T15:00:00Z"}
        appmod._ledger_persist_trips(uid, [trip])
        return [trip], []
    monkeypatch.setattr(appmod, "account_trade_history", fake)

    appmod.persist_all_account_trades()
    appmod.persist_all_account_trades()           # idempotent

    assert len(appmod._ledger_merge(u1, [], only_account_id=a1)) == 1
    assert len(appmod._ledger_merge(u2, [], only_account_id=a2)) == 1
    # Strict per-user scoping: u1's sweep never wrote into u2's ledger.
    assert appmod._ledger_merge(u1, [], only_account_id=a2) == []


def test_journal_shows_only_connected_accounts():
    # The journal must show ONLY accounts currently connected to KhomaAPI. Disconnect an
    # account (its broker_accounts row is DELETED) and its numbers vanish immediately.
    uid = _user()
    _connect(uid, "ACC-A")
    _connect(uid, "ACC-B")
    tA = {"account": "ACC-A", "_account_id": 61, "symbol": "MNQ", "side": "long", "qty": 1,
          "entry_price": 100, "exit_price": 110, "pnl": 10.0,
          "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:00:00Z"}
    tB = {"account": "ACC-B", "_account_id": 62, "symbol": "MNQ", "side": "long", "qty": 1,
          "entry_price": 100, "exit_price": 90, "pnl": -10.0,
          "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:05:00Z"}
    appmod._ledger_persist_trips(uid, [tA, tB])
    assert len(appmod._ledger_merge(uid, [])) == 2                       # both connected -> both show
    # Disconnect B (delete its broker row, as /broker/disconnect does).
    con = appmod.db(); con.execute("DELETE FROM broker_accounts WHERE user_id=? AND account_name=?", (uid, "ACC-B")); con.commit(); con.close()
    merged = appmod._ledger_merge(uid, [])
    assert len(merged) == 1 and merged[0]["account"] == "ACC-A"          # B's numbers gone
    assert round(sum(t["pnl"] for t in merged), 2) == 10.0
    assert round(sum(appmod.ledger_daily_map(uid).values()), 2) == 10.0  # calendar scoped too


def test_journal_scoping_fails_safe_on_lookup_error(monkeypatch):
    # A DB hiccup while reading the connected set must NOT blank the journal — fail safe = show all.
    uid = _user()
    appmod._ledger_persist_trips(uid, [{
        "account": "X", "_account_id": 63, "symbol": "MNQ", "side": "long", "qty": 1,
        "entry_price": 100, "exit_price": 110, "pnl": 10.0,
        "opened_at": "2026-06-24T14:00:00Z", "closed_at": "2026-06-24T15:00:00Z"}])
    assert appmod._ledger_merge(uid, []) == []                           # not connected -> hidden
    def boom(user_id, connected_only=False):
        raise RuntimeError("db down")
    monkeypatch.setattr(appmod, "get_broker_accounts", boom)
    assert len(appmod._ledger_merge(uid, [])) == 1                       # error -> show all, never blank
