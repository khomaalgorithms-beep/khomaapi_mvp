"""Tradovate OAuth 2.0 helpers.

Flow (matches Tradovate's official example-api-oauth):
  1. Redirect user to https://trader.tradovate.com/oauth with client_id + redirect_uri.
  2. Tradovate redirects back to redirect_uri?code=...&state=...
  3. Exchange the code for an access token at https://live.tradovateapi.com/auth/oauthtoken.
  4. Use the access token (Bearer) against the v1 REST API to list accounts.
"""

import os
import re
import calendar
import urllib.parse
from datetime import datetime, timezone, date, timedelta

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


# ----------------------------------------------------------------------------
# Symbol resolution: TradingView sends continuous / root symbols (e.g. "MNQ1!"
# or "MNQ") but Tradovate can only trade a specific dated contract (e.g.
# "MNQM6"). resolve_contract() turns the former into the active contract.
# ----------------------------------------------------------------------------

_MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
_CODE_MONTH = {v: k for k, v in _MONTH_CODE.items()}
_QUARTERLY = (3, 6, 9, 12)  # Mar / Jun / Sep / Dec cycle

# Roots that trade on the quarterly equity-index cycle (incl. CME micros).
_QUARTERLY_ROOTS = {"ES", "NQ", "RTY", "YM", "MES", "MNQ", "M2K", "MYM", "EMD", "NKD"}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _third_friday(year: int, month: int) -> date:
    cal = calendar.monthcalendar(year, month)
    fridays = [w[calendar.FRIDAY] for w in cal if w[calendar.FRIDAY]]
    return date(year, month, fridays[2])


def _front_quarter(today: date):
    """Return (year, month) of the quarterly contract to trade today. Rolls to
    the next contract about a week before expiry (third Friday)."""
    cands = []
    for y in (today.year, today.year + 1):
        for m in _QUARTERLY:
            roll = _third_friday(y, m) - timedelta(days=8)
            cands.append((roll, y, m))
    cands.sort()
    for roll, y, m in cands:
        if today <= roll:
            return y, m
    return cands[-1][1], cands[-1][2]


def _is_full_contract(sym: str) -> bool:
    # e.g. MNQM6 / ESZ25 — root + month code + 1-2 digit year.
    return bool(re.fullmatch(r"[A-Z0-9]+[FGHJKMNQUVXZ]\d{1,2}", sym))


def _root_of(sym: str) -> str:
    # MNQ1! -> MNQ, MNQ! -> MNQ, MNQ -> MNQ
    return re.sub(r"[0-9]*!*$", "", sym).strip() or sym


def suggest_contracts(env: str, token: str, text: str, limit: int = 20) -> list:
    data = _get(env, token, "/contract/suggest", {"t": text, "l": limit})
    return data if isinstance(data, list) else []


def _name_maturity(name: str, root: str):
    m = re.fullmatch(re.escape(root) + r"([FGHJKMNQUVXZ])(\d{1,2})", name)
    if not m:
        return None
    month = _CODE_MONTH[m.group(1)]
    yr = int(m.group(2))
    if yr < 100:  # 1-2 digit year -> nearest full year
        base = _today().year
        full = (base // 100) * 100 + yr
        if full < base - 1:
            full += 100
        yr = full
    try:
        return _third_friday(yr, month)
    except Exception:
        return None


def resolve_contract(env: str, token: str, raw_symbol: str) -> str:
    """Map a TradingView symbol to a tradable Tradovate contract.

    - Already a dated contract (MNQM6)        -> unchanged
    - Quarterly index root / continuous (MNQ1!) -> deterministic front month
    - Anything else                            -> nearest live contract from
      Tradovate's /contract/suggest, else passed through unchanged.
    """
    raw = (raw_symbol or "").upper().strip()
    if not raw:
        return raw
    if _is_full_contract(raw) and not raw.endswith("!"):
        return raw

    root = _root_of(raw)
    if root in _QUARTERLY_ROOTS:
        y, m = _front_quarter(_today())
        return f"{root}{_MONTH_CODE[m]}{y % 10}"

    # General fallback: pick the nearest non-expired live contract for this root.
    today = _today()
    best = None
    for c in suggest_contracts(env, token, root):
        name = str(c.get("name") or "").upper()
        mat = _name_maturity(name, root)
        if mat and mat >= today and (best is None or mat < best[0]):
            best = (mat, name)
    return best[1] if best else raw


def place_order(env: str, token: str, account_spec, account_id, action: str, symbol: str, qty: int):
    """Place a market order on a specific account using its OAuth token.

    The symbol is resolved to a tradable Tradovate contract first, so continuous
    TradingView tickers like "MNQ1!" become the active contract (e.g. "MNQM6").

    Returns the parsed Tradovate response dict, or {"error": ...} on transport failure.
    """
    contract = resolve_contract(env, token, symbol)
    body = {
        "accountSpec": str(account_spec),
        "accountId": int(account_id),
        "action": "Buy" if str(action).lower() == "buy" else "Sell",
        "symbol": str(contract).upper(),
        "orderQty": int(qty),
        "orderType": "Market",
        "isAutomated": True,
        "timeInForce": "Day",
    }
    resp = _post(env, token, "/order/placeorder", body)
    return resp if resp is not None else {"error": "request failed"}
