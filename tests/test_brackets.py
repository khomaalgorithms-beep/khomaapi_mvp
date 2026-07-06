"""Exchange-side OCO bracket orders: entry places real resting stop + take-profit at
Tradovate (placeOSO), move_stop modifies the resting stop, exit cancels + flattens.
These tests cover the pure logic + the exact Tradovate request bodies (broker calls
mocked) — the live fill path still needs a one-off demo verification."""

import os
import tempfile

os.environ["KHOMA_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ["KHOMA_DISABLE_WATCHDOG"] = "1"

from app import main as appmod          # noqa: E402
from app import tradovate_oauth as tvo  # noqa: E402


# ---- alert -> bracket legs -------------------------------------------------

def test_entry_legs_scaleout_two_brackets():
    legs = appmod.entry_legs_from_alert("buy", 2,
        {"sl": 29960, "tp1": 30012, "tp2": 30080, "qtyScale": 1, "qtyRunner": 1})
    assert legs == [(1, 29960.0, 30012.0), (1, 29960.0, 30080.0)]


def test_entry_legs_simple_one_bracket():
    assert appmod.entry_legs_from_alert("buy", 1, {"sl": 29960, "tp": 30080}) == [(1, 29960.0, 30080.0)]


def test_entry_legs_stop_only():
    assert appmod.entry_legs_from_alert("sell", 1, {"sl": 30040}) == [(1, 30040.0, None)]


def test_entry_legs_no_levels_is_plain():
    assert appmod.entry_legs_from_alert("buy", 3, {}) == [(3, None, None)]


# ---- safety: never place a wrong-side protective bracket -------------------

def test_bracket_side_validation():
    assert appmod._bracket_side_ok("buy", 29960, 30080) is True
    assert appmod._bracket_side_ok("buy", 30080, 29960) is False   # stop above target on a long
    assert appmod._bracket_side_ok("sell", 30040, 29900) is True
    assert appmod._bracket_side_ok("sell", 29900, 30040) is False  # stop below target on a short
    assert appmod._bracket_side_ok("buy", None, 30080) is True     # one-sided is fine


def test_round_tick_and_tick_for():
    assert appmod._round_tick(29960.07, 0.25) == 29960.0
    assert appmod._round_tick(30012.6, 0.25) == 30012.5
    assert appmod._round_tick(None, 0.25) is None
    assert appmod._tick_for("MNQU6") == 0.25
    assert appmod._tick_for("MGCM6") == 0.1


# ---- exact Tradovate request bodies ---------------------------------------

def test_placeOSO_body_for_bracket(monkeypatch):
    cap = {}
    monkeypatch.setattr(tvo, "resolve_contract", lambda env, tok, sym: "MNQU6")
    monkeypatch.setattr(tvo, "_post", lambda env, token, path, body: (cap.update(path=path, body=body), {"orderId": 1})[1])
    tvo.place_bracket_order("demo", "tok", "ACC", 44, "buy", "MNQ1!", 2, 29960.0, 30080.0)
    assert cap["path"] == "/order/placeOSO"
    b = cap["body"]
    assert b["action"] == "Buy" and b["orderType"] == "Market" and b["orderQty"] == 2
    assert b["isAutomated"] is True
    assert b["bracket1"] == {"action": "Sell", "orderType": "Stop", "stopPrice": 29960.0}
    assert b["bracket2"] == {"action": "Sell", "orderType": "Limit", "price": 30080.0}


def test_placeOSO_degrades_to_plain_when_no_levels(monkeypatch):
    cap = {}
    monkeypatch.setattr(tvo, "resolve_contract", lambda env, tok, sym: "MNQU6")
    monkeypatch.setattr(tvo, "_post", lambda env, token, path, body: (cap.update(path=path, body=body), {"orderId": 1})[1])
    tvo.place_bracket_order("demo", "tok", "ACC", 44, "sell", "MNQ1!", 1, None, None)
    assert cap["path"] == "/order/placeorder"      # plain market, no bracket
    assert "bracket1" not in cap["body"]


def test_modifyorder_body_for_move_stop(monkeypatch):
    cap = {}
    monkeypatch.setattr(tvo, "_post", lambda env, token, path, body: (cap.update(path=path, body=body), {"ok": True})[1])
    tvo.modify_stop_price("demo", "tok", 999, 30000.0)
    assert cap["path"] == "/order/modifyorder"
    b = cap["body"]
    assert b["orderId"] == 999 and b["orderType"] == "Stop"
    assert b["stopPrice"] == 30000.0 and b["isAutomated"] is True


def test_working_orders_join_identifies_stops():
    orders = [
        {"id": 10, "accountId": 44, "ordStatus": "Working", "contractId": 5},
        {"id": 11, "accountId": 44, "ordStatus": "Filled",  "contractId": 5},
        {"id": 12, "accountId": 99, "ordStatus": "Working", "contractId": 5},
    ]
    versions = [
        {"orderId": 10, "orderType": "Stop",  "stopPrice": 29960},
        {"orderId": 11, "orderType": "Limit", "price": 30080},
    ]
    stops = tvo.working_orders_for(orders, versions, 44, order_type="Stop")
    assert len(stops) == 1 and stops[0]["id"] == 10 and stops[0]["stopPrice"] == 29960
    # Only the working order for acct 44 comes back when no type filter is applied.
    allw = tvo.working_orders_for(orders, versions, 44)
    assert [o["id"] for o in allw] == [10]


# ---- webhook handler routing (broker layer mocked) ------------------------

from fastapi.testclient import TestClient  # noqa: E402

_client = TestClient(appmod.app)
_un = [0]


def _mk_user():
    _un[0] += 1
    secret = f"SEC{_un[0]}"          # webhook_secret is UNIQUE
    con = appmod.db(); cur = con.cursor()
    uid = appmod.dbmod.insert_returning_id(cur,
        "INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at,"
        "automation_status,max_orders,max_contracts,duplicate_seconds,max_rejections_per_day) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"brk{_un[0]}@t.com", "x", f"bk{_un[0]}", secret, "2026-01-01T00:00:00+00:00",
         "Running", 100, 10, 8, 9999))
    con.commit(); con.close()
    return uid, f"brk{_un[0]}@t.com", secret


def test_handler_entry_routes_to_bracket(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_bracket_to_accounts",
                        lambda accts, sym, side, legs: (cap.update(legs=legs, side=side, sym=sym), {"placed": 2, "total": 2, "accounts": 1, "results": []})[1])
    monkeypatch.setattr(appmod, "execute_to_accounts",
                        lambda *a, **k: cap.update(plain=True) or {"placed": 0, "total": 0, "accounts": 1, "results": []})
    r = _client.post("/webhook/trade", json={
        "client_id": email, "auth": secret, "symbol": "MNQ1!", "side": "buy", "event": "entry",
        "qtyTotal": 2, "qtyScale": 1, "qtyRunner": 1, "entry": 30000, "sl": 29960, "tp1": 30012, "tp2": 30080, "be": 30000})
    assert r.status_code == 200
    assert cap.get("legs") == [(1, 29960.0, 30012.0), (1, 29960.0, 30080.0)]   # two brackets
    assert cap.get("side") == "buy" and "plain" not in cap                     # not the plain path


def test_handler_no_levels_routes_to_plain(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_to_accounts",
                        lambda *a, **k: cap.update(plain=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    monkeypatch.setattr(appmod, "execute_bracket_to_accounts",
                        lambda *a, **k: cap.update(bracket=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "symbol": "MNQ1!", "side": "buy"})
    assert r.status_code == 200
    assert cap.get("plain") is True and "bracket" not in cap


def test_handler_noncanary_user_with_levels_stays_plain(monkeypatch):
    # BACKWARD-COMPAT / rollout guarantee: a user NOT on the bracket allow-list keeps
    # plain market orders even when their alert carries sl/tp -> every existing client is
    # byte-for-byte unchanged until brackets are enabled for everyone.
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "BRACKET_ORDERS", True)
    monkeypatch.setattr(appmod, "BRACKET_ORDERS_ONLY_USERS", {"only-admin@x.com"})   # NOT this user
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_to_accounts",
                        lambda *a, **k: cap.update(plain=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    monkeypatch.setattr(appmod, "execute_bracket_to_accounts",
                        lambda *a, **k: cap.update(bracket=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "symbol": "MNQ1!", "side": "buy",
                                             "event": "entry", "qtyTotal": 2, "qtyScale": 1, "qtyRunner": 1,
                                             "sl": 29960, "tp1": 30012, "tp2": 30080})
    assert r.status_code == 200
    assert cap.get("plain") is True and "bracket" not in cap    # brackets OFF for a non-canary user


def test_handler_move_stop_modifies_not_entry(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "move_stops_to_accounts",
                        lambda accts, sym, stop: (cap.update(stop=stop, sym=sym), {"placed": 1, "total": 1, "accounts": 1, "results": [{"moved": 1}]})[1])
    monkeypatch.setattr(appmod, "execute_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    monkeypatch.setattr(appmod, "execute_bracket_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "side": "buy", "event": "move_stop", "stop": 30000, "symbol": "MNQ1!"})
    assert r.status_code == 200
    assert cap.get("stop") == 30000 and "entered" not in cap    # moved a stop, did NOT open a position


def test_handler_exit_flattens(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "exit_from_accounts",
                        lambda accts, sym: (cap.update(exit=True, sym=sym), {"placed": 1, "total": 1, "accounts": 1, "results": []})[1])
    monkeypatch.setattr(appmod, "execute_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "exit"})
    assert r.status_code == 200
    assert cap.get("exit") is True and "entered" not in cap


# ---- SAFETY: placeOSO is non-atomic -> confirm the stop rests, else flatten ----------

def test_cancel_ok_detects_failures():
    assert appmod._cancel_ok({"commandId": 5}) is True
    assert appmod._cancel_ok({"failureReason": "Rejected"}) is False
    assert appmod._cancel_ok({"failureText": "nope"}) is False
    assert appmod._cancel_ok(None) is False          # swallowed transport failure
    assert appmod._cancel_ok("oops") is False


def test_contract_name_safe_on_nondict(monkeypatch):
    monkeypatch.setattr(tvo, "get_contract", lambda e, t, c: "a bare error string")  # non-dict
    assert appmod._contract_name("demo", "tok", 5) == ""       # must not raise
    monkeypatch.setattr(tvo, "get_contract", lambda e, t, c: {"name": "mnqu6"})
    assert appmod._contract_name("demo", "tok", 5) == "MNQU6"


def test_liquidate_position_body(monkeypatch):
    cap = {}
    monkeypatch.setattr(tvo, "_post", lambda e, t, p, b: (cap.update(path=p, body=b), {"ok": True})[1])
    tvo.liquidate_position("demo", "tok", 44, 123)
    assert cap["path"] == "/order/liquidatePosition"
    assert cap["body"] == {"accountId": 44, "contractId": 123, "admin": False}


def test_place_stop_order_body(monkeypatch):
    cap = {}
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(tvo, "_post", lambda e, t, p, b: (cap.update(path=p, body=b), {"orderId": 1})[1])
    tvo.place_stop_order("demo", "tok", "ACC", 44, "sell", "MNQ1!", 2, 29960.0)
    b = cap["body"]
    assert cap["path"] == "/order/placeorder"
    assert b["orderType"] == "Stop" and b["stopPrice"] == 29960.0
    assert b["action"] == "Sell" and b["orderQty"] == 2 and b["isAutomated"] is True


def test_ensure_protected_flattens_naked_position(monkeypatch):
    # placeOSO filled the entry but NO stop rests -> must flatten, never hold it naked.
    monkeypatch.setattr(appmod, "_STOP_CONFIRM_TRIES", 1)
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: 2)      # open long 2
    monkeypatch.setattr(appmod, "_resting_stop_count", lambda a, s: 0)    # NO resting stop
    did = {}
    monkeypatch.setattr(appmod, "cancel_working_orders_for", lambda a, s: did.setdefault("cancel", True))
    monkeypatch.setattr(appmod, "_liquidate_positions_for", lambda a, s: did.setdefault("liquidate", True))
    remedy = appmod._ensure_protected_or_flatten({"account_name": "A"}, "MNQU6", 1)
    assert remedy and remedy["ok"] is False and remedy["unprotected_flattened"] is True
    assert did.get("liquidate") is True


def test_ensure_protected_noop_when_stop_rests(monkeypatch):
    monkeypatch.setattr(appmod, "_STOP_CONFIRM_TRIES", 1)
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: 2)
    monkeypatch.setattr(appmod, "_resting_stop_count", lambda a, s: 1)    # a stop rests
    did = {}
    monkeypatch.setattr(appmod, "_liquidate_positions_for", lambda a, s: did.setdefault("liquidate", True))
    assert appmod._ensure_protected_or_flatten({"account_name": "A"}, "MNQU6", 1) is None
    assert "liquidate" not in did


def test_ensure_protected_noop_when_already_flat(monkeypatch):
    monkeypatch.setattr(appmod, "_STOP_CONFIRM_TRIES", 1)
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: 0)      # already flat
    monkeypatch.setattr(appmod, "_resting_stop_count", lambda a, s: 0)
    did = {}
    monkeypatch.setattr(appmod, "_liquidate_positions_for", lambda a, s: did.setdefault("liquidate", True))
    assert appmod._ensure_protected_or_flatten({"account_name": "A"}, "MNQU6", 2) is None
    assert "liquidate" not in did


def test_bracket_aggregate_risk_gate_uses_total_qty(monkeypatch):
    # Scale-out must be gated ONCE against the TOTAL qty, not per-leg (per-leg reads a
    # stale net and lets the account exceed max_position).
    calls = []
    monkeypatch.setattr(appmod, "risk_gate", lambda a, side, qty, sym: (calls.append(qty), (True, "", False))[1])
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "dec", lambda x: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(tvo, "place_bracket_order", lambda *a, **k: {"orderId": 1})
    monkeypatch.setattr(appmod, "_ensure_protected_or_flatten", lambda a, s, n: None)
    accts = [{"account_name": "A", "account_id": 44, "env": "demo", "access_token_enc": "x"}]
    legs = [(1, 29960.0, 30012.0), (1, 29960.0, 30080.0)]         # scale-out, total 2
    appmod.execute_bracket_to_accounts(accts, "MNQ1!", "buy", legs)
    assert calls == [2]                                           # ONE gate, total qty


def test_bracket_entry_flattens_leftover_before_placing(monkeypatch):
    # Fresh-entry reset: if a position is still open (a diverged/un-filled bracket), the
    # new entry flattens it + cancels stale orders BEFORE placing, so it can't stack.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "dec", lambda x: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "risk_gate", lambda a, side, qty, sym: (True, "", False))
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: 2)     # leftover long 2
    did = {}
    monkeypatch.setattr(appmod, "cancel_working_orders_for", lambda a, s: did.setdefault("cancel", True))
    monkeypatch.setattr(appmod, "flatten_on_account", lambda a, s: (did.setdefault("flatten", True), [])[1])
    monkeypatch.setattr(tvo, "place_bracket_order", lambda *a, **k: {"orderId": 1})
    monkeypatch.setattr(appmod, "_ensure_protected_or_flatten", lambda a, s, n: None)
    accts = [{"account_name": "A", "account_id": 44, "env": "demo", "access_token_enc": "x"}]
    legs = [(1, 29960.0, 30012.0), (1, 29960.0, 30080.0)]
    r = appmod.execute_bracket_to_accounts(accts, "MNQ1!", "buy", legs)
    assert did.get("flatten") is True and did.get("cancel") is True     # leftover cleared first
    assert r["placed"] == 2                                             # then new bracket placed


def test_bracket_entry_no_reset_when_flat(monkeypatch):
    # Already flat -> the reset is a no-op (no flatten), just places the bracket.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "dec", lambda x: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "risk_gate", lambda a, side, qty, sym: (True, "", False))
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: 0)     # already flat
    did = {}
    monkeypatch.setattr(appmod, "flatten_on_account", lambda a, s: (did.setdefault("flatten", True), [])[1])
    monkeypatch.setattr(tvo, "place_bracket_order", lambda *a, **k: {"orderId": 1})
    monkeypatch.setattr(appmod, "_ensure_protected_or_flatten", lambda a, s, n: None)
    accts = [{"account_name": "A", "account_id": 44, "env": "demo", "access_token_enc": "x"}]
    r = appmod.execute_bracket_to_accounts(accts, "MNQ1!", "buy", [(1, 29960.0, 30012.0)])
    assert "flatten" not in did and r["placed"] == 1


def test_exit_flatten_runs_even_if_cancel_raises(monkeypatch):
    # A cancel-phase exception must NEVER skip the flatten (it's the prop kill-switch).
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    def _boom(a, s):
        raise RuntimeError("cancel boom")
    monkeypatch.setattr(appmod, "cancel_working_orders_for", _boom)
    seq = {}
    monkeypatch.setattr(appmod, "_liquidate_positions_for", lambda a, s: seq.setdefault("liq", 1))
    out = appmod.exit_from_accounts([{"account_name": "A", "account_id": 44, "env": "demo"}], "MNQ1!")
    r = out["results"][0]
    assert seq.get("liq") == 1 and r["ok"] is True and r.get("cancel_error")


def test_canary_gate_scopes_brackets_to_allowlist(monkeypatch):
    # Global on, no canary -> everyone gets brackets.
    monkeypatch.setattr(appmod, "BRACKET_ORDERS", True)
    monkeypatch.setattr(appmod, "BRACKET_ORDERS_ONLY_USERS", set())
    assert appmod._brackets_on_for("anyone@x.com") is True
    # Canary set -> ONLY the listed user; everyone else stays on plain market orders.
    monkeypatch.setattr(appmod, "BRACKET_ORDERS_ONLY_USERS", {"khomadima89@gmail.com"})
    assert appmod._brackets_on_for("khomadima89@gmail.com") is True
    assert appmod._brackets_on_for("KHOMADIMA89@gmail.com") is True     # case-insensitive
    assert appmod._brackets_on_for("client@x.com") is False
    # Global kill-switch off -> nobody, even if on the allow-list.
    monkeypatch.setattr(appmod, "BRACKET_ORDERS", False)
    assert appmod._brackets_on_for("khomadima89@gmail.com") is False


def test_exit_idempotent_when_flat(monkeypatch):
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "cancel_working_orders_for", lambda a, s: 0)
    monkeypatch.setattr(appmod, "_liquidate_positions_for", lambda a, s: 0)
    out = appmod.exit_from_accounts([{"account_name": "A", "account_id": 44, "env": "demo"}], "MNQ1!")
    r = out["results"][0]
    assert r["ok"] is True and r["flat"] is True and r["cancelled"] == 0 and r["liquidated"] == 0
