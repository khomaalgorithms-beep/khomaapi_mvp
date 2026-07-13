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


def renew_access_token(env: str, token: str) -> dict:
    """Renew a still-valid Tradovate access token. Must be called BEFORE the
    current token expires — an already-expired token cannot be renewed and the
    user must reconnect. Returns {"ok", "access_token", "expiration"} or error."""
    try:
        r = requests.get(
            f"{_rest_base(env)}/auth/renewAccessToken",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"renew failed: {e}"}

    new_token = data.get("accessToken") or data.get("access_token")
    if not new_token:
        return {"ok": False, "error": data}
    return {
        "ok": True,
        "access_token": new_token,
        "expiration": data.get("expirationTime") or data.get("expiration") or "",
    }


def fetch_accounts_for_env(access_token: str, env: str) -> dict:
    """List Tradovate accounts on a single environment ('live' or 'demo')."""
    base = REST_DEMO if env == "demo" else REST_LIVE
    try:
        r = requests.get(f"{base}/account/list", headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e), "accounts": [], "env": env}
    if isinstance(data, list):
        return {"ok": True, "accounts": data, "env": env}
    return {"ok": False, "error": data, "accounts": [], "env": env}


def fetch_accounts(access_token: str, envs=("live", "demo")) -> dict:
    """List accounts across the requested environments. A single Tradovate OAuth
    login exposes BOTH demo and live, so by default we import both (tagged with
    their env) — the user then chooses which to trade. Pass envs=("demo",) to
    import only demo, etc. Returns {"ok", "accounts": [...with _env...], "env"}.
    """
    if isinstance(envs, str):
        envs = (envs,)
    all_accounts = []
    last_error = None
    for env in envs:
        res = fetch_accounts_for_env(access_token, env)
        if res.get("ok"):
            for acc in res["accounts"]:
                acc["_env"] = env  # tag each account with the env it came from
            all_accounts.extend(res["accounts"])
        else:
            last_error = res.get("error")
    if all_accounts:
        return {"ok": True, "accounts": all_accounts, "env": all_accounts[0].get("_env", "live")}
    return {"ok": False, "error": last_error or "No accounts returned", "accounts": []}


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


def get_orders(env: str, token: str) -> list:
    # Tradovate Fill objects carry orderId (not accountId); orders carry the
    # accountId, so we use this to attribute fills to the right account.
    data = _get(env, token, "/order/list")
    return data if isinstance(data, list) else []


def get_order_versions(env: str, token: str) -> list:
    """orderVersion/list — carries the orderType (Stop/Limit), price and stopPrice for
    each order (the Order entity itself does not), so we can tell a resting stop from a
    resting target when modifying/cancelling exits."""
    data = _get(env, token, "/orderVersion/list")
    return data if isinstance(data, list) else []


def get_cash_snapshot(env: str, token: str, account_id):
    try:
        return _post(env, token, "/cashBalance/getCashBalanceSnapshot", {"accountId": int(account_id)})
    except Exception:
        return None


_WORKING_STATUSES = ("working", "pending", "suspended", "queued", "received", "accepted")


def place_bracket_order(env: str, token: str, account_spec, account_id, action: str,
                        symbol: str, qty: int, stop_price=None, limit_price=None):
    """Place a MARKET entry with a resting OCO bracket at the exchange (placeOSO): the
    stop and take-profit sit as working orders the instant the entry fills, so they fill
    at price-or-better and survive a server/VPS/internet outage. The bracket legs are the
    OPPOSITE action of the entry. If NEITHER a stop nor a limit is supplied, this degrades
    to a plain market order (unchanged behaviour for strategies that send no levels).

    Returns the parsed Tradovate response dict, or {"error": ...} on transport failure.
    """
    contract = resolve_contract(env, token, symbol)
    entry_action = "Buy" if str(action).lower() == "buy" else "Sell"
    exit_action = "Sell" if entry_action == "Buy" else "Buy"
    body = {
        "accountSpec": str(account_spec),
        "accountId": int(account_id),
        "action": entry_action,
        "symbol": str(contract).upper(),
        "orderQty": int(qty),
        "orderType": "Market",
        "isAutomated": True,
        "timeInForce": "Day",
    }
    brackets = []
    if stop_price is not None:
        brackets.append({"action": exit_action, "orderType": "Stop", "stopPrice": float(stop_price)})
    if limit_price is not None:
        brackets.append({"action": exit_action, "orderType": "Limit", "price": float(limit_price)})
    if not brackets:                       # no exits -> plain market entry
        resp = _post(env, token, "/order/placeorder", body)
        return resp if resp is not None else {"error": "request failed"}
    body["bracket1"] = brackets[0]
    if len(brackets) > 1:
        body["bracket2"] = brackets[1]
    resp = _post(env, token, "/order/placeOSO", body)
    return resp if resp is not None else {"error": "request failed"}


def modify_stop_price(env: str, token: str, order_id, new_stop, qty=None):
    """Move an existing working STOP order to a new price (used by move_stop → breakeven).
    Never opens/adds a position — it only edits a resting order."""
    body = {"orderId": int(order_id), "orderType": "Stop",
            "stopPrice": float(new_stop), "isAutomated": True}
    if qty is not None:
        body["orderQty"] = int(qty)
    try:
        return _post(env, token, "/order/modifyorder", body)
    except Exception:
        return None


def liquidate_position(env: str, token: str, account_id, contract_id):
    """Flatten a whole position for a contract server-side, RACE-SAFELY, in one call:
    Tradovate's /order/liquidatePosition cancels the position's resting orders AND
    closes the position atomically, and is a clean no-op (200) when already flat. This
    avoids the read-then-offset race of a manual market close (which can open a fresh
    reverse position if the position closes between the position read and the order)."""
    body = {"accountId": int(account_id), "contractId": int(contract_id), "admin": False}
    try:
        return _post(env, token, "/order/liquidatePosition", body)
    except Exception:
        return None


def place_stop_order(env: str, token: str, account_spec, account_id, action: str,
                     symbol: str, qty: int, stop_price):
    """Place a standalone working STOP order — used to RE-PROTECT a position when a
    placeOSO bracket's stop leg was rejected (non-atomic OSO) so the account is never
    left naked. `action` is the EXIT action (opposite the entry). Returns parsed resp."""
    contract = resolve_contract(env, token, symbol)
    body = {
        "accountSpec": str(account_spec),
        "accountId": int(account_id),
        "action": "Buy" if str(action).lower() == "buy" else "Sell",
        "symbol": str(contract).upper(),
        "orderQty": int(qty),
        "orderType": "Stop",
        "stopPrice": float(stop_price),
        "isAutomated": True,
        "timeInForce": "Day",
    }
    resp = _post(env, token, "/order/placeorder", body)
    return resp if resp is not None else {"error": "request failed"}


def place_oco_exit(env: str, token: str, account_spec, account_id, exit_action: str,
                   symbol: str, qty: int, stop_price, limit_price):
    """Place a standalone OCO exit pair (protective STOP + take-profit LIMIT) for an EXISTING
    position — NO entry order, so it never opens/adds to a position. One leg filling cancels
    the other AT THE EXCHANGE, so a breakeven-stop fill takes the runner's take-profit down in
    the same instant (no orphaned limit). `exit_action` is the CLOSING side (opposite the
    position): a short runner closes with BUY orders, a long runner with SELL orders."""
    contract = resolve_contract(env, token, symbol)
    act = "Buy" if str(exit_action).lower() == "buy" else "Sell"
    body = {
        "accountSpec": str(account_spec),
        "accountId": int(account_id),
        "action": act,
        "symbol": str(contract).upper(),
        "orderQty": int(qty),
        "orderType": "Stop",
        "stopPrice": float(stop_price),
        "isAutomated": True,
        "timeInForce": "Day",
        "other": {
            "action": act,
            "orderType": "Limit",
            "price": float(limit_price),
            "isAutomated": True,
            "timeInForce": "Day",
        },
    }
    resp = _post(env, token, "/order/placeOCO", body)
    return resp if resp is not None else {"error": "request failed"}


def place_stoplimit_bracket(env: str, token: str, account_spec, account_id, action: str,
                            symbol: str, qty: int, entry_stop, entry_limit, stop_price, limit_price):
    """Place a RESTING stop-limit ENTRY with an attached OCO bracket (protective stop-market SL
    + take-profit limit) via placeOSO. The entry rests until price triggers it; the SL/TP only
    become working orders once the entry FILLS (OSO) — no naked window. Entry = StopLimit (a
    stop trigger with a limit cap = a small slippage allowance so it can't chase); SL = Stop
    (stop-MARKET, always exits even through the level); TP = Limit. `action` is the ENTRY side."""
    contract = resolve_contract(env, token, symbol)
    entry_action = "Buy" if str(action).lower() == "buy" else "Sell"
    exit_action = "Sell" if entry_action == "Buy" else "Buy"
    body = {
        "accountSpec": str(account_spec),
        "accountId": int(account_id),
        "action": entry_action,
        "symbol": str(contract).upper(),
        "orderQty": int(qty),
        "orderType": "StopLimit",
        "stopPrice": float(entry_stop),
        "price": float(entry_limit),
        "isAutomated": True,
        "timeInForce": "Day",
        "bracket1": {"action": exit_action, "orderType": "Stop", "stopPrice": float(stop_price)},
        "bracket2": {"action": exit_action, "orderType": "Limit", "price": float(limit_price)},
    }
    resp = _post(env, token, "/order/placeOSO", body)
    return resp if resp is not None else {"error": "request failed"}


def working_orders_for(orders: list, versions: list, account_id, contract_id=None,
                       order_type: str = None) -> list:
    """Join order/list with orderVersion/list to return WORKING orders for an account
    (optionally one contract and/or one orderType like 'Stop'), each annotated with
    {id, orderType, stopPrice, price, contractId}. Used by move_stop and exit."""
    ver_by_order = {}
    for v in versions or []:
        oid = v.get("orderId")
        if oid is not None:
            ver_by_order[oid] = v          # latest wins (list is chronological)
    out = []
    for o in orders or []:
        if str(o.get("accountId")) != str(account_id):
            continue
        status = str(o.get("ordStatus") or o.get("status") or "").lower()
        if status not in _WORKING_STATUSES:
            continue
        cid = o.get("contractId")
        if contract_id is not None and str(cid) != str(contract_id):
            continue
        v = ver_by_order.get(o.get("id"), {})
        otype = str(v.get("orderType") or o.get("orderType") or "")
        if order_type is not None and otype.lower() != order_type.lower():
            continue
        out.append({"id": o.get("id"), "orderType": otype, "contractId": cid,
                    "stopPrice": v.get("stopPrice"), "price": v.get("price")})
    return out


def cancel_order(env: str, token: str, order_id):
    """Cancel one working order. Used by the risk kill-switch before flattening."""
    try:
        return _post(env, token, "/order/cancelorder", {"orderId": int(order_id)})
    except Exception:
        return None


def working_order_ids(orders, account_id) -> list:
    """IDs of still-working orders for an account (to cancel on a risk breach)."""
    out = []
    for o in orders or []:
        if str(o.get("accountId")) != str(account_id):
            continue
        status = str(o.get("ordStatus") or o.get("status") or "").lower()
        if status in ("working", "pending", "suspended", "queued", "received", "accepted"):
            out.append(o.get("id"))
    return [i for i in out if i is not None]


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
