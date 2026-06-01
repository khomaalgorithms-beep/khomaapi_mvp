"""Whop billing integration.

Two responsibilities, both pure-ish (network only via explicit api_key args):
  1. Verify incoming webhook signatures (Standard Webhooks spec).
  2. Resolve a membership's authoritative state from the Whop API.

Design rule: a webhook is only a TRIGGER. We never trust the webhook body to
grant access — we re-fetch the membership from the API and use its `valid`
flag, so a forged/stale body can't unlock anything.
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.request
from datetime import datetime, timezone

API_BASE = "https://api.whop.com/api/v2"
SIG_TOLERANCE_SECONDS = 300  # reject webhooks older than 5 min (replay guard)


# ---- Signature verification (Standard Webhooks) -------------------------

def _secret_key_candidates(secret: str):
    """Whop secrets look like `ws_<64 hex>`. Standard Webhooks normally base64-
    decodes a `whsec_` secret. To be correct regardless of Whop's exact
    encoding — while still requiring possession of the secret — we try the
    plausible derivations and accept whichever the signer used. An attacker
    without the secret can forge none of them."""
    s = secret.strip()
    body = s
    for pref in ("whsec_", "ws_"):
        if body.startswith(pref):
            body = body[len(pref):]
            break
    cands = [s.encode(), body.encode()]
    try:
        cands.append(base64.b64decode(body))
    except Exception:
        pass
    try:
        cands.append(bytes.fromhex(body))
    except Exception:
        pass
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def verify_signature(body: bytes, headers, secret: str):
    """Verify a Standard Webhooks signature. Returns (ok: bool, reason: str).

    Signed content is "{webhook-id}.{webhook-timestamp}.{raw-body}", HMAC-SHA256,
    signature header is space-separated "v1,<base64>" entries."""
    if not secret:
        return False, "no-secret-configured"
    get = headers.get
    wid = get("webhook-id") or get("Webhook-Id")
    wts = get("webhook-timestamp") or get("Webhook-Timestamp")
    wsig = get("webhook-signature") or get("Webhook-Signature")
    if not (wid and wts and wsig):
        return False, "missing-headers"
    try:
        if abs(time.time() - int(wts)) > SIG_TOLERANCE_SECONDS:
            return False, "stale-timestamp"
    except Exception:
        return False, "bad-timestamp"

    signed = wid.encode() + b"." + str(wts).encode() + b"." + body
    presented = []
    for part in wsig.split():
        presented.append(part.split(",", 1)[1] if "," in part else part)

    for i, key in enumerate(_secret_key_candidates(secret)):
        mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        for p in presented:
            if hmac.compare_digest(mac, p):
                return True, f"ok:cand{i}"
    return False, "signature-mismatch"


# ---- Whop API -----------------------------------------------------------

def _api_get(path: str, api_key: str):
    req = urllib.request.Request(f"{API_BASE}{path}",
                                 headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_membership(membership_id: str, api_key: str):
    if not (membership_id and api_key):
        return None
    try:
        return _api_get(f"/memberships/{membership_id}", api_key)
    except Exception:
        return None


def find_membership_by_email(email: str, api_key: str, allowed_plan_ids=None):
    """Best membership for an email. Prefers a currently-valid one, and (if
    allowed_plan_ids given) only considers memberships on our plans — so a
    membership for a different Whop product (e.g. manual signals) is ignored."""
    email = (email or "").lower().strip()
    if not (email and api_key):
        return None
    allowed = set(allowed_plan_ids) if allowed_plan_ids else None
    best = None
    page = 1
    while page <= 10:
        try:
            d = _api_get(f"/memberships?per=50&page={page}", api_key)
        except Exception:
            break
        for m in d.get("data", []):
            if (m.get("email") or "").lower().strip() != email:
                continue
            if allowed and m.get("plan") not in allowed:
                continue
            if m.get("valid"):
                return m            # a valid one wins immediately
            best = best or m
        pg = d.get("pagination", {})
        if page >= pg.get("total_page", 1):
            break
        page += 1
    return best


# ---- Membership → stored state ------------------------------------------

def _to_iso(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except Exception:
        return str(ts)


def membership_state(m: dict) -> dict:
    """Normalize a Whop membership into the fields we persist. `valid` already
    encodes 'active, including canceled-but-paid-through-period-end'."""
    valid = bool(m.get("valid"))
    return {
        "membership_id": m.get("id"),
        "user_id": m.get("user"),
        "plan_id": m.get("plan"),
        "email": (m.get("email") or "").lower().strip(),
        "valid": valid,
        # Map to our subscription_status vocabulary; "active" iff Whop says valid.
        "status": "active" if valid else (m.get("status") or "inactive"),
        "period_end": _to_iso(m.get("renewal_period_end") or m.get("expires_at")),
    }


def extract_membership_id(payload: dict) -> str:
    """Pull a membership id out of any webhook event (membership.* or payment.*)."""
    data = payload.get("data") or {}
    mid = data.get("id")
    if isinstance(mid, str) and mid.startswith("mem_"):
        return mid
    for k in ("membership", "membership_id"):
        v = data.get(k)
        if isinstance(v, str) and v.startswith("mem_"):
            return v
    return ""
