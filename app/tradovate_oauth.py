"""Tradovate OAuth 2.0 helpers.

Flow (matches Tradovate's official example-api-oauth):
  1. Redirect user to https://trader.tradovate.com/oauth with client_id + redirect_uri.
  2. Tradovate redirects back to redirect_uri?code=...&state=...
  3. Exchange the code for an access token at https://live.tradovateapi.com/auth/oauthtoken.
  4. Use the access token (Bearer) against the v1 REST API to list accounts.
"""

import os
import urllib.parse

import requests

AUTHORIZE_URL = "https://trader.tradovate.com/oauth"
TOKEN_URL = "https://live.tradovateapi.com/auth/oauthtoken"

# REST API roots (note: /v1, unlike the token endpoint above).
REST_LIVE = "https://live.tradovateapi.com/v1"
REST_DEMO = "https://demo.tradovateapi.com/v1"


def _client_id() -> str:
    return (os.getenv("TRADOVATE_CLIENT_ID") or os.getenv("TRADOVATE_CID") or "").strip()


def _client_secret() -> str:
    return (os.getenv("TRADOVATE_CLIENT_SECRET") or os.getenv("TRADOVATE_SEC") or "").strip()


def _redirect_uri() -> str:
    return (os.getenv("TRADOVATE_REDIRECT_URI") or "").strip()


def build_tradovate_login(state: str = "") -> str:
    """Build the Tradovate authorization URL the user is redirected to."""
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code: str) -> dict:
    """Exchange an authorization code for an access token.

    Returns {"ok": True, "access_token": str, "expires_in": int} on success,
    or {"ok": False, "error": ...} on failure.
    """
    client_id = _client_id()
    client_secret = _client_secret()
    redirect_uri = _redirect_uri()

    if not client_id or not client_secret or not redirect_uri:
        return {
            "ok": False,
            "error": "Missing TRADOVATE_CLIENT_ID / TRADOVATE_CLIENT_SECRET / TRADOVATE_REDIRECT_URI.",
        }

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }

    try:
        response = requests.post(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    except Exception as e:
        return {"ok": False, "error": f"Token request failed: {e}"}

    try:
        data = response.json()
    except Exception:
        return {"ok": False, "error": "Token response was not JSON", "raw": response.text}

    access_token = data.get("access_token") or data.get("accessToken")
    if not access_token:
        return {"ok": False, "error": "No access token returned", "response": data}

    return {
        "ok": True,
        "access_token": access_token,
        "expires_in": int(data.get("expires_in") or data.get("expiresIn") or 0),
    }


def fetch_accounts(access_token: str) -> dict:
    """List all Tradovate accounts visible to this token.

    Tries the live REST root first, then demo, so a single OAuth login surfaces
    every account (cash, live, prop). Returns {"ok": True, "accounts": [...], "env": "live"|"demo"}.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    last_error = None

    for env, base in (("live", REST_LIVE), ("demo", REST_DEMO)):
        try:
            response = requests.get(f"{base}/account/list", headers=headers, timeout=20)
            data = response.json()
        except Exception as e:
            last_error = str(e)
            continue

        if isinstance(data, list):
            if data:
                return {"ok": True, "accounts": data, "env": env}
            # Valid but empty on this env — keep the empty result as a fallback.
            last_error = "No accounts returned"
        else:
            last_error = data

    return {"ok": False, "error": last_error or "Could not fetch accounts", "accounts": []}


# ----------------------------------------------------------------------------
# Read-only REST helpers used by the live monitor. All are defensive: they
# never raise, returning [] / None on any failure so the dashboard stays up.
# ----------------------------------------------------------------------------

def _rest_base(env: str) -> str:
    return REST_DEMO if env == "demo" else REST_LIVE


def _get(env: str, token: str, path: str, params: dict | None = None):
    try:
        r = requests.get(
            f"{_rest_base(env)}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        return r.json()
    except Exception:
        return None


def _post(env: str, token: str, path: str, body: dict):
    try:
        r = requests.post(
            f"{_rest_base(env)}{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        return r.json()
    except Exception:
        return None


def get_positions(env: str, token: str) -> list:
    data = _get(env, token, "/position/list")
    return data if isinstance(data, list) else []


def get_fills(env: str, token: str) -> list:
    data = _get(env, token, "/fill/list")
    return data if isinstance(data, list) else []


def get_cash_snapshot(env: str, token: str, account_id):
    try:
        return _post(env, token, "/cashBalance/getCashBalanceSnapshot", {"accountId": int(account_id)})
    except Exception:
        return None


def get_contract(env: str, token: str, contract_id):
    return _get(env, token, "/contract/item", {"id": contract_id})


def place_order(env: str, token: str, account_spec, account_id, action: str, symbol: str, qty: int):
    """Place a market order on a specific account using its OAuth token.

    Returns the parsed Tradovate response dict, or {"error": ...} on transport failure.
    """
    body = {
        "accountSpec": str(account_spec),
        "accountId": int(account_id),
        "action": "Buy" if str(action).lower() == "buy" else "Sell",
        "symbol": str(symbol).upper(),
        "orderQty": int(qty),
        "orderType": "Market",
        "isAutomated": True,
        "timeInForce": "Day",
    }
    resp = _post(env, token, "/order/placeorder", body)
    return resp if resp is not None else {"error": "request failed"}
