"""Recurring news lockout: a rule ('never trade CPI') + the live calendar must
auto-materialize an enforceable lockout window every time the event occurs."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["KHOMA_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ["KHOMA_DISABLE_WATCHDOG"] = "1"

from app import main as appmod  # noqa: E402
from app.risk_engine import evaluate_order, Decision  # noqa: E402

_n = [0]


def _user():
    _n[0] += 1
    con = appmod.db()
    cur = con.cursor()
    uid = appmod.dbmod.insert_returning_id(
        cur, "INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at) VALUES(?,?,?,?,?)",
        (f"news{_n[0]}@t.com", "x", f"news_k{_n[0]}", f"news_s{_n[0]}", "2026-01-01T00:00:00+00:00"))
    con.commit()
    con.close()
    return uid


def _add_rule(uid, kw, before=15, after=15):
    con = appmod.db()
    con.execute("INSERT INTO news_rules(user_id,account_id,keyword,minutes_before,minutes_after,enabled,created_at) "
                "VALUES(?,?,?,?,?,1,?)", (uid, None, kw, before, after, "x"))
    con.commit()
    con.close()


def _mock_calendar(monkeypatch, events):
    monkeypatch.setattr(appmod, "fetch_calendar_events", lambda w, n: (events, False))


def test_recurring_rule_materializes_window(monkeypatch):
    uid = _user()
    _add_rule(uid, "CPI")
    evt = datetime.now(timezone.utc) + timedelta(hours=1)
    _mock_calendar(monkeypatch, [{"dt": evt, "title": "CPI m/m", "impact": "high", "currency": "USD"}])
    appmod.sync_recurring_news_locks()
    wins = appmod.news_windows_for(uid, 999)
    assert len(wins) == 1
    s, e = wins[0]
    assert s <= evt <= e


def test_non_matching_event_not_blocked(monkeypatch):
    uid = _user()
    _add_rule(uid, "CPI")
    evt = datetime.now(timezone.utc) + timedelta(hours=1)
    _mock_calendar(monkeypatch, [{"dt": evt, "title": "Retail Sales m/m", "impact": "high", "currency": "USD"}])
    appmod.sync_recurring_news_locks()
    assert appmod.news_windows_for(uid, 999) == []


def test_sync_is_idempotent(monkeypatch):
    uid = _user()
    _add_rule(uid, "FOMC", 10, 10)
    evt = datetime.now(timezone.utc) + timedelta(hours=2)
    _mock_calendar(monkeypatch, [{"dt": evt, "title": "FOMC Statement", "impact": "high", "currency": "USD"}])
    appmod.sync_recurring_news_locks()
    appmod.sync_recurring_news_locks()  # run twice → still one window
    assert len(appmod.news_windows_for(uid, 999)) == 1


def test_order_blocked_during_auto_window(monkeypatch):
    uid = _user()
    _add_rule(uid, "CPI")
    now = datetime.now(timezone.utc)
    _mock_calendar(monkeypatch, [{"dt": now, "title": "CPI y/y", "impact": "high", "currency": "USD"}])
    appmod.sync_recurring_news_locks()
    wins = appmod.news_windows_for(uid, 1)
    d = evaluate_order({}, {"news_windows": wins}, {"side": "buy", "qty": 1}, now_utc=now)
    assert d.action == Decision.REJECT and "news" in d.reason.lower()
