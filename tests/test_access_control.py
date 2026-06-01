"""End-to-end access-control tests against the real FastAPI app.

Runs with ENFORCE_SUBSCRIPTIONS=1 + ALLOW_MANUAL_PLAN=1 on a throwaway SQLite DB,
so we exercise the live middleware, gate(), and webhook checks — the things that
must never let a non-payer trade or lock out a paying customer.
"""

import os
import tempfile

# Configure env BEFORE importing the app (these are read at import time).
os.environ["KHOMA_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ["ENFORCE_SUBSCRIPTIONS"] = "1"
os.environ["ALLOW_MANUAL_PLAN"] = "1"
os.environ["COOKIE_SECURE"] = "0"
os.environ["KHOMA_DISABLE_WATCHDOG"] = "1"

import pytest
from fastapi.testclient import TestClient

from app import main as appmod

client = TestClient(appmod.app)

_counter = [0]


def make_user(plan=None, status=None, period_end=None, membership=None, plan_id=None):
    """Create a user row with the given subscription state and return its id."""
    _counter[0] += 1
    n = _counter[0]
    email = f"u{n}@test.com"
    con = appmod.db()
    cur = con.cursor()
    uid = appmod.dbmod.insert_returning_id(
        cur,
        "INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at,allowed_symbols) "
        "VALUES(?,?,?,?,?,?)",
        (email, appmod.hash_password("pw"), f"k_{n}", f"secret_{n}",
         "2026-01-01T00:00:00+00:00", "*"),
    )
    con.execute(
        "UPDATE users SET manual_plan=?, subscription_status=?, current_period_end=?, "
        "whop_membership_id=?, whop_plan_id=? WHERE id=?",
        (plan, status, period_end, membership, plan_id, uid),
    )
    con.commit()
    con.close()
    return uid, email


def cookies_for(uid):
    return {"khoma_session": appmod.create_session(uid)}


# --------------------------------------------------------------------------
# No subscription → blocked entirely
# --------------------------------------------------------------------------

def test_no_subscription_blocks_dashboard():
    uid, _ = make_user(plan=None)  # logged in, but no plan
    r = client.get("/dashboard", cookies=cookies_for(uid), follow_redirects=False)
    body = r.text.lower()
    assert "execution dashboard" not in body            # real dashboard NOT shown
    assert "subscription" in body or r.status_code in (302, 402)


def test_no_subscription_blocks_api_402():
    uid, _ = make_user(plan=None)
    r = client.get("/api/trades", cookies=cookies_for(uid))
    assert r.status_code == 402
    assert r.json()["error"] == "no active subscription"


def test_canceled_and_expired_blocked_entirely():
    # canceled with the paid period already elapsed → revoked
    uid, _ = make_user(plan=None, status="canceled",
                       period_end="2020-01-01T00:00:00+00:00",
                       membership="mem_x", plan_id="whatever")
    r = client.get("/api/trades", cookies=cookies_for(uid))
    assert r.status_code == 402


# --------------------------------------------------------------------------
# Feature gating: Solo blocked from premium features
# --------------------------------------------------------------------------

def test_solo_blocked_from_copy_trading():
    uid, _ = make_user(plan="solo")
    r = client.post("/broker/copy/set",
                    data={"account_id": "1", "in_box": "1"},
                    cookies=cookies_for(uid))
    assert r.status_code == 403
    assert "copy_trading" in r.json()["error"]


def test_pro_allowed_copy_trading():
    uid, _ = make_user(plan="pro")
    r = client.post("/broker/copy/set",
                    data={"account_id": "1", "in_box": "1"},
                    cookies=cookies_for(uid))
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_solo_can_still_reach_dashboard_and_risk():
    # Solo is a paying plan — core dashboard + risk engine must work.
    uid, _ = make_user(plan="solo")
    c = cookies_for(uid)
    assert client.get("/dashboard", cookies=c).status_code == 200
    assert client.get("/risk", cookies=c).status_code == 200
    assert client.get("/calendar", cookies=c).status_code == 200


# --------------------------------------------------------------------------
# Trade webhook
# --------------------------------------------------------------------------

def test_webhook_blocked_for_inactive_user():
    uid, email = make_user(plan=None)
    r = client.post("/webhook/trade", json={
        "client_id": email, "auth": "x", "symbol": "MNQ1!", "side": "buy", "qty": 1})
    assert r.status_code == 402
    assert r.json()["error"] == "blocked: no active subscription"


def test_webhook_not_blocked_by_subscription_for_active_user():
    uid, email = make_user(plan="pro")
    r = client.post("/webhook/trade", json={
        "client_id": email, "auth": "wrong-secret", "symbol": "MNQ1!", "side": "buy", "qty": 1})
    # It may fail later (bad secret / no accounts), but NOT for subscription.
    assert r.status_code != 402
    assert "no active subscription" not in r.text


# --------------------------------------------------------------------------
# Whop is authoritative: an active Whop membership beats a missing manual flag,
# and an inactive Whop membership is NOT rescued by a manual flag.
# --------------------------------------------------------------------------

def test_active_whop_membership_grants_access(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_pro_test")
    uid, _ = make_user(plan=None, status="active",
                       membership="mem_active", plan_id="plan_pro_test")
    r = client.post("/broker/copy/set", data={"account_id": "1", "in_box": "1"},
                    cookies=cookies_for(uid))
    assert r.status_code == 200  # Pro via Whop → copy trading allowed


def test_manual_flag_cannot_override_inactive_whop():
    # Has a Whop membership but it's expired; manual_plan set to elite must NOT help.
    uid, _ = make_user(plan="elite", status="expired",
                       membership="mem_dead", plan_id="plan_pro_test")
    r = client.get("/api/trades", cookies=cookies_for(uid))
    assert r.status_code == 402
