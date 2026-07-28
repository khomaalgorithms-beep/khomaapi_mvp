"""End-to-end access-control tests against the real FastAPI app.

Runs with ENFORCE_SUBSCRIPTIONS=1 + ALLOW_MANUAL_PLAN=1 on a throwaway SQLite DB,
so we exercise the live middleware, gate(), and webhook checks — the things that
must never let a non-payer trade or lock out a paying customer.
"""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time

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


def make_user(plan=None, status=None, period_end=None, membership=None, plan_id=None, email=None):
    """Create a user row with the given subscription state and return its id."""
    _counter[0] += 1
    n = _counter[0]
    email = email or f"u{n}@test.com"
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


# --------------------------------------------------------------------------
# Whop webhook: end-to-end grant → revoke (Whop API fetch mocked)
# --------------------------------------------------------------------------

_WH_SECRET_RAW = b"webhook-signing-key-32-bytes!!!!"
_WH_SECRET = "whsec_" + base64.b64encode(_WH_SECRET_RAW).decode()


def _signed_post(body_dict):
    body = json.dumps(body_dict).encode()
    wid, wts = "msg_test", str(int(time.time()))
    mac = base64.b64encode(
        hmac.new(_WH_SECRET_RAW, f"{wid}.{wts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    headers = {"webhook-id": wid, "webhook-timestamp": wts,
               "webhook-signature": f"v1,{mac}", "content-type": "application/json"}
    return client.post("/whop/webhook", content=body, headers=headers)


def test_whop_webhook_grant_then_revoke(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_wh_pro")
    monkeypatch.setattr(appmod, "WHOP_WEBHOOK_SECRET", _WH_SECRET)

    uid, email = make_user(plan=None)  # no access yet
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 402

    # Whop says this membership is valid → grant.
    valid_member = {"id": "mem_live", "user": "user_1", "plan": "plan_wh_pro",
                    "email": email, "valid": True, "status": "completed",
                    "renewal_period_end": 1893456000}
    monkeypatch.setattr(appmod.whopmod, "fetch_membership", lambda mid, key: valid_member)
    r = _signed_post({"action": "membership.went_valid", "data": {"id": "mem_live"}})
    assert r.status_code == 200
    # Access granted (Pro): copy trading now allowed.
    assert client.post("/broker/copy/set", data={"account_id": "1", "in_box": "1"},
                       cookies=cookies_for(uid)).status_code == 200

    # Now Whop says invalid → revoke.
    dead_member = dict(valid_member, valid=False, status="expired")
    monkeypatch.setattr(appmod.whopmod, "fetch_membership", lambda mid, key: dead_member)
    r = _signed_post({"action": "membership.went_invalid", "data": {"id": "mem_live"}})
    assert r.status_code == 200
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 402


def test_webhook_deleted_membership_revokes(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_revoke_pro")
    monkeypatch.setattr(appmod, "WHOP_WEBHOOK_SECRET", _WH_SECRET)
    uid, _ = make_user(status="active", period_end="2099-01-01T00:00:00+00:00",
                       membership="mem_gone", plan_id="plan_revoke_pro")
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 200  # active first
    # Whop hard-deleted the membership → fetch returns GONE.
    monkeypatch.setattr(appmod.whopmod, "fetch_membership", lambda mid, key: appmod.whopmod.GONE)
    r = _signed_post({"action": "membership.went_invalid", "data": {"id": "mem_gone"}})
    assert r.status_code == 200 and r.json().get("revoked") is True
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 402  # revoked


def test_reverify_revokes_deleted_membership(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_rv2")
    monkeypatch.setattr(appmod, "WHOP_API_KEY", "k")
    uid, _ = make_user(status="active", period_end="2099-01-01T00:00:00+00:00",
                       membership="mem_rv", plan_id="plan_rv2")
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 200
    monkeypatch.setattr(appmod.whopmod, "fetch_membership", lambda mid, key: appmod.whopmod.GONE)
    appmod.whop_reverify_tick()
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 402  # revoked by re-verify


def test_buy_before_signup_links_on_login(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_wh_pro2")
    monkeypatch.setattr(appmod, "WHOP_WEBHOOK_SECRET", _WH_SECRET)
    monkeypatch.setattr(appmod, "WHOP_API_KEY", "test-key")  # gate try_link_whop on
    buyer = "buyer_first@test.com"
    member = {"id": "mem_pending", "user": "user_p", "plan": "plan_wh_pro2",
              "email": buyer, "valid": True, "status": "completed",
              "renewal_period_end": 1893456000}
    monkeypatch.setattr(appmod.whopmod, "fetch_membership", lambda mid, key: member)

    # 1) Webhook arrives BEFORE the buyer has an account → recorded as pending.
    r = _signed_post({"action": "membership.went_valid", "data": {"id": "mem_pending"}})
    assert r.status_code == 200 and r.json()["linked_user_id"] is None

    # 2) Buyer signs up with that email, then logs in → auto-linked.
    uid, _ = make_user(plan=None, email=buyer)
    lr = client.post("/login", data={"email": buyer, "password": "pw"},
                     follow_redirects=False)
    assert lr.status_code in (302, 303)
    # 3) Now active (Pro) — no longer blocked.
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code != 402


def test_signup_blocked_without_whop_plan(monkeypatch):
    # Gated set-password: no active Whop purchase → cannot create an account.
    monkeypatch.setattr(appmod, "whop_membership_for_email", lambda e: None)
    r = client.post("/signup", data={"email": "noplan@test.com", "password": "TestPw123456!"})
    assert "No active plan" in r.text
    con = appmod.db()
    row = con.execute("SELECT 1 FROM users WHERE email=?", ("noplan@test.com",)).fetchone()
    con.close()
    assert row is None  # nothing created


def test_signup_succeeds_for_whop_buyer(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_signup_pro")
    member = {"id": "mem_signup", "user": "user_s", "plan": "plan_signup_pro",
              "email": "buyer2@test.com", "valid": True, "status": "completed",
              "renewal_period_end": 1893456000}
    monkeypatch.setattr(appmod, "whop_membership_for_email", lambda e: member)
    r = client.post("/signup", data={"email": "buyer2@test.com", "password": "TestPw123456!"},
                    follow_redirects=False)
    assert r.status_code == 302  # success → sign in
    con = appmod.db()
    u = con.execute("SELECT * FROM users WHERE email=?", ("buyer2@test.com",)).fetchone()
    con.close()
    assert u is not None
    e = appmod.user_entitlements(u)
    assert e.active is True and e.tier == "pro"


# --------------------------------------------------------------------------
# Google OAuth gating + WebSocket + debug lockdown (close every bypass)
# --------------------------------------------------------------------------

from starlette.websockets import WebSocketDisconnect


class _FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _fake_requests(token_data, userinfo):
    class _R:
        def post(self, url, **kw):
            return _FakeResp(token_data)

        def get(self, url, **kw):
            return _FakeResp(userinfo)
    return _R()


def test_google_login_new_email_no_sub_denied(monkeypatch):
    monkeypatch.setattr(appmod, "requests",
                        _fake_requests({"access_token": "tok"},
                                       {"email": "gnew@test.com", "verified_email": True}))
    monkeypatch.setattr(appmod, "whop_membership_for_email", lambda e: None)
    r = client.get("/auth/google/callback?code=abc", follow_redirects=False)
    assert r.status_code == 302 and "/subscribe" in r.headers.get("location", "")
    con = appmod.db()
    row = con.execute("SELECT 1 FROM users WHERE email=?", ("gnew@test.com",)).fetchone()
    con.close()
    assert row is None  # no account created for a non-buyer


def test_google_login_existing_no_sub_denied(monkeypatch):
    uid, email = make_user(plan=None)  # existing account, no sub
    monkeypatch.setattr(appmod, "requests",
                        _fake_requests({"access_token": "tok"},
                                       {"email": email, "verified_email": True}))
    r = client.get("/auth/google/callback?code=abc", follow_redirects=False)
    sid = r.cookies.get("khoma_session")
    assert sid  # auth succeeded (session minted)
    rr = client.get("/api/trades", cookies={"khoma_session": sid})
    assert rr.status_code == 402  # but access is DENIED


def test_google_login_with_sub_allowed(monkeypatch):
    monkeypatch.setenv("WHOP_PLAN_PRO_M", "plan_g_pro")
    member = {"id": "mem_g", "user": "user_g", "plan": "plan_g_pro",
              "email": "gbuyer@test.com", "valid": True, "status": "completed",
              "renewal_period_end": 1893456000}
    monkeypatch.setattr(appmod, "requests",
                        _fake_requests({"access_token": "tok"},
                                       {"email": "gbuyer@test.com", "verified_email": True}))
    monkeypatch.setattr(appmod, "whop_membership_for_email", lambda e: member)
    r = client.get("/auth/google/callback?code=abc", follow_redirects=False)
    sid = r.cookies.get("khoma_session")
    assert sid
    rr = client.get("/api/trades", cookies={"khoma_session": sid})
    assert rr.status_code != 402  # active Pro → allowed


def test_websocket_denied_without_subscription():
    c = TestClient(appmod.app)  # fresh client, no cookies
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws"):
            pass
    assert exc.value.code == 1008


def test_websocket_allowed_with_subscription():
    uid, _ = make_user(plan="pro")
    c = TestClient(appmod.app)
    c.cookies.set("khoma_session", appmod.create_session(uid))
    with c.websocket_connect("/ws") as ws:
        ws.send_text("ping")  # accepted → no exception


def test_session_revoked_when_sub_lapses_midsession():
    uid, _ = make_user(plan="pro")          # active when the session is minted
    cookies = cookies_for(uid)
    assert client.get("/api/trades", cookies=cookies).status_code != 402
    # Sub lapses (webhook would mark expired). Same long-lived session:
    con = appmod.db()
    con.execute("UPDATE users SET manual_plan=NULL, subscription_status='expired', "
                "whop_membership_id='mem_lapse', whop_plan_id='p' WHERE id=?", (uid,))
    con.commit()
    con.close()
    # Next request on the SAME session is denied (status re-checked server-side).
    assert client.get("/api/trades", cookies=cookies).status_code == 402


def test_login_post_allowed_from_marketing_site():
    # Cross-origin POST from our marketing domain must NOT be CSRF-blocked.
    r = client.post("/login", data={"email": "x@y.com", "password": "nope"},
                    headers={"origin": "https://khomaapi.com", "referer": "https://khomaapi.com/"})
    assert r.status_code != 403  # reaches the handler (bad creds), not blocked


def test_state_change_blocked_from_untrusted_origin():
    uid, _ = make_user(plan="pro")
    r = client.post("/broker/copy/set", data={"account_id": "1", "in_box": "1"},
                    cookies=cookies_for(uid),
                    headers={"origin": "https://evil.example", "referer": "https://evil.example/"})
    assert r.status_code == 403  # cross-site state change still blocked


def test_login_page_loads_public():
    assert client.get("/login").status_code == 200
    assert client.get("/signup").status_code == 200
    assert client.get("/subscribe").status_code == 200


def test_debug_endpoints_404_in_production():
    assert client.get("/debug/db-path").status_code == 404
    assert client.get("/debug/accounts").status_code == 404
    assert client.get("/test").status_code == 404


def test_whop_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(appmod, "WHOP_WEBHOOK_SECRET", _WH_SECRET)
    body = json.dumps({"action": "membership.went_valid", "data": {"id": "mem_x"}}).encode()
    r = client.post("/whop/webhook", content=body, headers={
        "webhook-id": "m", "webhook-timestamp": str(int(time.time())),
        "webhook-signature": "v1,not-a-real-signature"})
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Owner-granted comp / team access (full Elite, independent of Whop billing)
# --------------------------------------------------------------------------

COMP_EMAIL = "amar.sgnm@gmail.com"


def test_comp_access_email_resolves_full_elite():
    e = appmod.user_entitlements({"email": COMP_EMAIL})
    assert e.active is True and e.tier == "elite" and e.source == "comp"
    # a normal email with no plan is NOT granted
    assert appmod.user_entitlements({"email": "stranger@test.com"}).active is False


def test_comp_access_email_reaches_dashboard_and_api_under_enforcement():
    # No plan, no Whop membership — access comes purely from the comp allowlist.
    uid, _ = make_user(plan=None, email=COMP_EMAIL)
    c = cookies_for(uid)
    assert client.get("/dashboard", cookies=c).status_code == 200      # full dashboard
    assert client.get("/api/trades", cookies=c).status_code == 200     # not 402
    # Elite includes copy trading — a gated feature works.
    r = client.post("/broker/copy/set", data={"account_id": "1", "in_box": "1"}, cookies=c)
    assert r.status_code == 200 and r.json().get("ok") is True


def test_comp_access_webhook_allowed():
    # Trade webhook auth is by payload; the comp email must clear the subscription gate.
    assert appmod.webhook_subscription_ok({"email": COMP_EMAIL}) is True
    assert appmod.webhook_subscription_ok({"email": "stranger@test.com"}) is False


def test_comp_access_revocable_via_env(monkeypatch):
    monkeypatch.setenv("COMP_ACCESS_REVOKED", COMP_EMAIL)
    e = appmod.user_entitlements({"email": COMP_EMAIL})
    assert e.active is False                                           # grant pulled -> blocked
    uid, _ = make_user(plan=None, email="amar2@test.com")             # sanity: unrelated user still blocked
    assert client.get("/api/trades", cookies=cookies_for(uid)).status_code == 402


def test_comp_access_addable_via_env(monkeypatch):
    monkeypatch.setenv("COMP_ACCESS_EMAILS", "granted@team.com")
    assert appmod.user_entitlements({"email": "granted@team.com"}).tier == "elite"
