"""Whop integration tests: signature verification (the security boundary) +
membership state mapping."""

import base64
import hashlib
import hmac
import time

from app import whop


def _sign(secret_key_bytes, wid, wts, body):
    signed = f"{wid}.{wts}.".encode() + body
    mac = base64.b64encode(hmac.new(secret_key_bytes, signed, hashlib.sha256).digest()).decode()
    return f"v1,{mac}"


def test_verify_accepts_standardwebhooks_base64_secret():
    raw_key = b"super-secret-key-bytes-32-long!!"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    wid, wts, body = "msg_1", str(int(time.time())), b'{"action":"membership.went_valid"}'
    sig = _sign(raw_key, wid, wts, body)
    headers = {"webhook-id": wid, "webhook-timestamp": wts, "webhook-signature": sig}
    ok, reason = whop.verify_signature(body, headers, secret)
    assert ok, reason


def test_verify_accepts_whop_hex_secret():
    # Whop's actual format: ws_<64 hex> → 32-byte key via fromhex.
    hexpart = "5adaf5ae17c41c84fd72722244816a4db5f6571aa3cf09d07a077fed5f0fefcf"
    secret = "ws_" + hexpart
    wid, wts, body = "msg_2", str(int(time.time())), b'{"x":1}'
    sig = _sign(bytes.fromhex(hexpart), wid, wts, body)
    headers = {"webhook-id": wid, "webhook-timestamp": wts, "webhook-signature": sig}
    ok, reason = whop.verify_signature(body, headers, secret)
    assert ok, reason


def test_verify_rejects_tampered_body():
    raw_key = b"another-32-byte-secret-key-here!"
    secret = "whsec_" + base64.b64encode(raw_key).decode()
    wid, wts = "msg_3", str(int(time.time()))
    sig = _sign(raw_key, wid, wts, b'{"amount":1}')
    headers = {"webhook-id": wid, "webhook-timestamp": wts, "webhook-signature": sig}
    ok, _ = whop.verify_signature(b'{"amount":999999}', headers, secret)  # body changed
    assert ok is False


def test_verify_rejects_missing_headers_and_stale():
    secret = "whsec_" + base64.b64encode(b"k" * 16).decode()
    assert whop.verify_signature(b"{}", {}, secret)[0] is False
    # stale timestamp (10 minutes old)
    wid, wts = "m", str(int(time.time()) - 600)
    sig = _sign(base64.b64decode(secret[len("whsec_"):]), wid, wts, b"{}")
    ok, reason = whop.verify_signature(
        b"{}", {"webhook-id": wid, "webhook-timestamp": wts, "webhook-signature": sig}, secret)
    assert ok is False and reason == "stale-timestamp"


def test_verify_rejects_when_no_secret():
    assert whop.verify_signature(b"{}", {"webhook-id": "x"}, "")[0] is False


def test_membership_state_valid():
    m = {"id": "mem_1", "user": "user_1", "plan": "plan_x", "email": "A@B.com",
         "valid": True, "status": "completed", "renewal_period_end": 1893456000}
    st = whop.membership_state(m)
    assert st["membership_id"] == "mem_1"
    assert st["plan_id"] == "plan_x"
    assert st["email"] == "a@b.com"
    assert st["status"] == "active"        # valid → active
    assert st["period_end"].startswith("2030")


def test_membership_state_invalid():
    m = {"id": "mem_2", "user": "u", "plan": "p", "email": "x@y.com",
         "valid": False, "status": "expired", "expires_at": 1577836800}
    st = whop.membership_state(m)
    assert st["status"] == "expired"       # not valid → keeps real status
    assert st["period_end"].startswith("2020")


def test_extract_membership_id():
    assert whop.extract_membership_id({"data": {"id": "mem_abc"}}) == "mem_abc"
    assert whop.extract_membership_id({"data": {"membership": "mem_pay"}}) == "mem_pay"
    assert whop.extract_membership_id({"data": {"id": "pay_123", "membership": "mem_z"}}) == "mem_z"
    assert whop.extract_membership_id({"data": {}}) == ""
