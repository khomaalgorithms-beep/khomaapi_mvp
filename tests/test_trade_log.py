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


def test_trade_stats_math():
    s = appmod._trade_stats([{"pnl": 100}, {"pnl": -40}, {"pnl": 60}])
    assert s["net"] == 120 and s["trades"] == 3 and s["wins"] == 2 and s["losses"] == 1
    assert s["pf_disp"] == "4.00"            # (100+60) / 40


def test_persist_track_trades_dedup(monkeypatch):
    uid = _user()
    monkeypatch.setattr(appmod, "_public_track_user", lambda: {"id": uid})
    monkeypatch.setattr(appmod, "_public_track_account_ids", lambda u: [777])
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
