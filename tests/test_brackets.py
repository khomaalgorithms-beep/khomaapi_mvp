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


# ---- SAFETY: symbol-scoping must fail CLOSED (deploy-readiness blockers) ---------------

def test_contract_name_caches_and_retries(monkeypatch):
    # A transient blank (429/None) must NOT stick — retry, then cache the resolved name.
    appmod._CONTRACT_NAME_CACHE.clear()
    calls = {"n": 0}
    def flaky(e, t, c):
        calls["n"] += 1
        return None if calls["n"] == 1 else {"name": "mnqu6"}     # 1st blips, 2nd resolves
    monkeypatch.setattr(tvo, "get_contract", flaky)
    assert appmod._contract_name("demo", "tok", 77) == "MNQU6"    # retried past the blip
    n_after = calls["n"]
    assert appmod._contract_name("demo", "tok", 77) == "MNQU6"    # cached -> no further broker calls
    assert calls["n"] == n_after


def test_liquidate_fail_closed_on_blank_contract(monkeypatch):
    # BLOCKER 2: a blank contract name (lookup failed) must NOT flatten a position we can't
    # identify as the requested symbol — else an exit misfires onto the wrong contract.
    appmod._CONTRACT_NAME_CACHE.clear()
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "get_positions", lambda e, t: [{"accountId": 44, "netPos": 2, "contractId": 999}])
    monkeypatch.setattr(tvo, "get_contract", lambda e, t, c: None)        # name never resolves -> blank
    liqs = []
    monkeypatch.setattr(tvo, "liquidate_position", lambda e, t, a, c: liqs.append(c))
    acct = {"account_name": "A", "account_id": 44, "env": "demo"}
    assert appmod._liquidate_positions_for(acct, "MNQ1!") == 0 and liqs == []   # fail closed
    assert appmod._liquidate_positions_for(acct, None) == 1 and liqs == [999]   # symbol-less flatten-all still works


def test_cancel_fail_closed_on_blank_contract(monkeypatch):
    appmod._CONTRACT_NAME_CACHE.clear()
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "get_orders", lambda e, t: [])
    monkeypatch.setattr(tvo, "get_order_versions", lambda e, t: [])
    monkeypatch.setattr(tvo, "working_orders_for", lambda o, v, a, order_type=None: [{"id": 11, "contractId": 999}])
    monkeypatch.setattr(tvo, "get_contract", lambda e, t, c: None)
    cancels = []
    monkeypatch.setattr(tvo, "cancel_order", lambda e, t, i: cancels.append(i) or {"commandId": 1})
    acct = {"account_name": "A", "account_id": 44, "env": "demo"}
    assert appmod.cancel_working_orders_for(acct, "MNQ1!") == 0 and cancels == []   # fail closed
    assert appmod.cancel_working_orders_for(acct, None) == 1 and cancels == [11]    # flatten-all path still cancels


def test_move_stops_fail_closed_on_blank_contract(monkeypatch):
    appmod._CONTRACT_NAME_CACHE.clear()
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "get_orders", lambda e, t: [])
    monkeypatch.setattr(tvo, "get_order_versions", lambda e, t: [])
    monkeypatch.setattr(tvo, "working_orders_for", lambda o, v, a, order_type=None: [{"id": 22, "contractId": 999}])
    monkeypatch.setattr(tvo, "get_contract", lambda e, t, c: None)
    moves = []
    monkeypatch.setattr(tvo, "modify_stop_price", lambda e, t, i, p: moves.append((i, p)))
    acct = {"account_name": "A", "account_id": 44, "env": "demo"}
    resp = appmod.move_stops_to_accounts([acct], "MNQ1!", 30000)
    assert moves == []                                       # fail closed: never move an unidentifiable stop
    assert resp["results"][0]["moved"] == 0


def test_handler_move_stop_requires_symbol(monkeypatch):
    # BLOCKER 1: a symbol-less move_stop would yank EVERY resting stop to one price -> reject it.
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "move_stops_to_accounts",
                        lambda *a, **k: cap.update(moved=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "move_stop", "stop": 30000})
    assert r.status_code == 200
    assert r.json().get("status") == "REJECTED" and "moved" not in cap   # refused, never fanned out account-wide


def test_handler_replace_stop_moves_not_entry(monkeypatch):
    # replace_stop must branch on event FIRST and NEVER open a position from side:"sell".
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "replace_stops_to_accounts",
                        lambda accts, sym, pos_side, stop: (cap.update(sym=sym, pos_side=pos_side, stop=stop),
                        {"placed": 1, "total": 1, "accounts": 1, "results": [{"ok": True}]})[1])
    monkeypatch.setattr(appmod, "execute_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    monkeypatch.setattr(appmod, "execute_bracket_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "replace_stop",
        "symbol": "MNQ1!", "side": "sell", "cancel_previous_stop": True, "stop": 29504.75, "applies_to": "remaining_runner"})
    assert r.status_code == 200
    b = r.json()
    assert b.get("status") == "EXECUTED" and b.get("action") == "REPLACE_STOP"
    assert cap.get("stop") == 29504.75 and cap.get("pos_side") == "sell" and "entered" not in cap   # moved a stop, opened NOTHING


def test_replace_stops_reocos_breakeven_stop_with_tp(monkeypatch):
    # Short runner: cancel OLD stop + OLD TP, re-arm breakeven stop + TP as ONE OCO on the
    # BUY side (opposite the position) so a stop fill takes the TP down in the same instant.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: -2)      # runner: short 2 still open
    monkeypatch.setattr(tvo, "get_orders", lambda e, t: [])
    monkeypatch.setattr(tvo, "get_order_versions", lambda e, t: [])
    monkeypatch.setattr(tvo, "working_orders_for",
        lambda o, v, a, order_type=None: [{"id": 7, "contractId": 5, "orderType": "Stop", "stopPrice": 29600},
                                          {"id": 8, "contractId": 5, "orderType": "Limit", "price": 29000}])
    monkeypatch.setattr(appmod, "_contract_name", lambda e, t, c: "MNQU6")
    cancels = []
    monkeypatch.setattr(tvo, "cancel_order", lambda e, t, i: cancels.append(i) or {"commandId": 1})
    oco = {}
    monkeypatch.setattr(tvo, "place_oco_exit",
        lambda env, tok, spec, aid, action, sym, qty, sp, lp: oco.update(action=action, qty=qty, stop=sp, tp=lp) or {"orderId": 1, "ocoId": 9})
    stops = []
    monkeypatch.setattr(tvo, "place_stop_order", lambda *a, **k: stops.append(1) or {"orderId": 2})
    out = appmod.replace_stops_to_accounts([{"account_name": "A", "account_id": 44, "env": "demo"}], "MNQ1!", "sell", 29504.75)
    assert sorted(cancels) == [7, 8]                       # cancelled BOTH the old stop and old TP
    assert oco["action"] == "buy" and oco["qty"] == 2      # short -> BUY exit OCO for the runner qty
    assert oco["stop"] == 29504.75 and oco["tp"] == 29000  # breakeven stop + the captured runner TP
    assert stops == []                                     # OCO accepted -> no bare-stop fallback
    assert out["results"][0]["kind"] == "oco" and out["placed"] == 1


def test_replace_stops_falls_back_to_bare_stop_when_oco_rejected(monkeypatch):
    # If placeOCO is rejected, the runner must STILL get a protective stop (never left naked).
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, s: -2)
    monkeypatch.setattr(tvo, "get_orders", lambda e, t: [])
    monkeypatch.setattr(tvo, "get_order_versions", lambda e, t: [])
    monkeypatch.setattr(tvo, "working_orders_for",
        lambda o, v, a, order_type=None: [{"id": 8, "contractId": 5, "orderType": "Limit", "price": 29000}])
    monkeypatch.setattr(appmod, "_contract_name", lambda e, t, c: "MNQU6")
    monkeypatch.setattr(tvo, "cancel_order", lambda e, t, i: {"commandId": 1})
    monkeypatch.setattr(tvo, "place_oco_exit", lambda *a, **k: {"failureReason": "RejectedByExchange"})
    placed = {}
    monkeypatch.setattr(tvo, "place_stop_order",
        lambda env, tok, spec, aid, action, sym, qty, px: placed.update(action=action, qty=qty, px=px) or {"orderId": 2})
    out = appmod.replace_stops_to_accounts([{"account_name": "A", "account_id": 44, "env": "demo"}], "MNQ1!", "sell", 29504.75)
    assert placed["action"] == "buy" and placed["qty"] == 2 and placed["px"] == 29504.75  # bare protective stop placed
    assert out["results"][0]["kind"] == "stop-fallback" and out["results"][0]["ok"] is True


# ---- resting stop-limit ORB entries (place_orb / cancel_pending_entries) ----

def test_place_stoplimit_bracket_body(monkeypatch):
    cap = {}
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(tvo, "_post", lambda e, t, p, b: (cap.update(path=p, body=b), {"orderId": 1})[1])
    tvo.place_stoplimit_bracket("demo", "tok", "ACC", 44, "buy", "MNQ1!", 2, 30000.0, 30002.0, 29995.0, 30020.0)
    b = cap["body"]
    assert cap["path"] == "/order/placeOSO"
    assert b["orderType"] == "StopLimit" and b["stopPrice"] == 30000.0 and b["price"] == 30002.0   # stop-limit entry w/ allowance
    assert b["action"] == "Buy" and b["orderQty"] == 2
    assert b["bracket1"] == {"action": "Sell", "orderType": "Stop", "stopPrice": 29995.0}           # SL = stop-MARKET
    assert b["bracket2"] == {"action": "Sell", "orderType": "Limit", "price": 30020.0}              # TP = limit


def test_execute_orb_places_both_sides(monkeypatch):
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "_cancel_pending_entries_one", lambda a, s: 0)
    calls = []
    monkeypatch.setattr(tvo, "place_stoplimit_bracket",
        lambda env, tok, spec, aid, action, sym, qty, es, el, sl, tp: calls.append((action, es, sl, tp)) or {"orderId": 1})
    ls = {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020}   # long: SL<TP
    ss = {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}   # short: SL>TP
    out = appmod.execute_orb_to_accounts([{"account_name": "A", "account_id": 44, "env": "demo"}], "MNQ1!", 2, ls, ss)
    assert sorted(c[0] for c in calls) == ["buy", "sell"]     # BOTH directions pre-placed
    assert out["placed"] == 1


def test_execute_orb_honors_per_account_contract_qty(monkeypatch):
    cap = {"qtys": []}
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda env, tok, sym: "MNQU6")
    monkeypatch.setattr(appmod, "_tick_for", lambda r: 0.25)
    monkeypatch.setattr(appmod, "_cancel_pending_entries_one", lambda a, r: 0)
    monkeypatch.setattr(tvo, "place_stoplimit_bracket",
        lambda env, tok, name, acct_id, side, resolved, q, es, el, sl, tp: (cap["qtys"].append(q), {"orderId": 1})[1])
    # account fixes its own size to 3 — must override the alert's qty of 10
    accounts = [{"account_name": "A", "account_id": 44, "env": "demo", "contract_qty": 3}]
    long_spec = {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020}
    appmod.execute_orb_to_accounts(accounts, "MNQ1!", 10, long_spec, None)
    assert cap["qtys"] == [3]   # used the per-account 3, not the alert's 10


def test_execute_orb_refuses_wrong_side_bracket(monkeypatch):
    # A long with SL above TP is nonsense -> refuse rather than place an instantly-triggering stop.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "_cancel_pending_entries_one", lambda a, s: 0)
    placed = []
    monkeypatch.setattr(tvo, "place_stoplimit_bracket", lambda *a, **k: placed.append(1) or {"orderId": 1})
    ls = {"entryStop": 30000, "entryLimit": 30002, "sl": 30050, "tp": 29990}   # SL ABOVE TP on a long -> reject
    out = appmod.execute_orb_to_accounts([{"account_name": "A", "account_id": 44, "env": "demo"}], "MNQ1!", 2, ls, None)
    assert placed == [] and out["placed"] == 0


def test_orb_account_risk_locked_helper(monkeypatch):
    monkeypatch.setattr(appmod, "_maybe_auto_unlock", lambda cfg: cfg)
    monkeypatch.setattr(appmod, "ensure_risk_config",
                        lambda aid, uid: {"account_id": aid, "locked": 1, "locked_reason": "Max drawdown."})
    assert "Max drawdown" in appmod._orb_account_risk_locked({"id": 91, "user_id": 2})
    monkeypatch.setattr(appmod, "ensure_risk_config", lambda aid, uid: {"account_id": aid, "locked": 0})
    assert appmod._orb_account_risk_locked({"id": 91, "user_id": 2}) == ""
    # fail CLOSED: if the config read raises, the account is treated as locked (entry blocked).
    def _boom(aid, uid):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(appmod, "ensure_risk_config", _boom)
    assert appmod._orb_account_risk_locked({"id": 91, "user_id": 2}) != ""           # placement: block
    # fail OPEN for the manager re-sweep: a read error must NOT cancel a legit resting entry.
    assert appmod._orb_account_risk_locked({"id": 91, "user_id": 2}, fail_closed=False) == ""


def test_execute_orb_skips_risk_locked_account(monkeypatch):
    # A daily-loss / drawdown lock must block a NEW ORB entry (same as risk_gate on other paths):
    # the account is skipped, nothing is placed, and the lock reason is surfaced.
    monkeypatch.setattr(appmod, "_maybe_auto_unlock", lambda cfg: cfg)
    monkeypatch.setattr(appmod, "ensure_risk_config",
                        lambda aid, uid: {"account_id": aid, "locked": 1, "locked_reason": "Daily loss limit hit."})
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda e, t, s: "MNQU6")
    monkeypatch.setattr(appmod, "_cancel_pending_entries_one", lambda a, s: 0)
    placed = []
    monkeypatch.setattr(tvo, "place_stoplimit_bracket", lambda *a, **k: placed.append(1) or {"orderId": 1})
    ls = {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020}
    out = appmod.execute_orb_to_accounts(
        [{"account_name": "A", "account_id": 44, "id": 91, "user_id": 2, "env": "demo"}], "MNQ1!", 2, ls, None)
    assert placed == []                                          # locked -> nothing placed
    assert out["placed"] == 0
    assert "Daily loss limit" in (out["results"][0].get("error") or "")


def test_cancel_pending_entries_only_stoplimit(monkeypatch):
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "get_orders", lambda e, t: [])
    monkeypatch.setattr(tvo, "get_order_versions", lambda e, t: [])
    # Only asked for StopLimit orders -> returns the pending entries; brackets (Stop/Limit) never queried.
    monkeypatch.setattr(tvo, "working_orders_for",
        lambda o, v, a, order_type=None: [{"id": 3, "contractId": 5}] if order_type == "StopLimit" else [])
    monkeypatch.setattr(appmod, "_contract_name", lambda e, t, c: "MNQU6")
    cancels = []
    monkeypatch.setattr(tvo, "cancel_order", lambda e, t, i: cancels.append(i) or {"commandId": 1})
    n = appmod._cancel_pending_entries_one({"account_name": "A", "account_id": 44, "env": "demo"}, "MNQU6")
    assert n == 1 and cancels == [3]                          # cancels the pending StopLimit entry only


def test_handler_place_orb_routes(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
        lambda accts, sym, qty, ls, ss: (cap.update(sym=sym, qty=qty, ls=ls, ss=ss), {"placed": 1, "total": 1, "accounts": 1, "results": []})[1])
    monkeypatch.setattr(appmod, "execute_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    monkeypatch.setattr(appmod, "execute_bracket_to_accounts", lambda *a, **k: cap.update(entered=True) or {})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 2,
        "long": {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020},
        "short": {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}})
    assert r.status_code == 200
    b = r.json()
    assert b.get("status") == "EXECUTED" and b.get("action") == "PLACE_ORB"
    assert cap.get("qty") == 2 and cap["ls"]["entryStop"] == 30000 and "entered" not in cap   # brackets placed, NO market entry


def test_handler_cancel_pending_entries_routes(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "cancel_pending_entries_to_accounts",
        lambda accts, sym: (cap.update(sym=sym), {"placed": 1, "total": 1, "accounts": 1, "results": [{"cancelled": 1}]})[1])
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "cancel_pending_entries", "symbol": "MNQ1!"})
    assert r.status_code == 200
    assert r.json().get("action") == "CANCEL_PENDING_ENTRIES" and cap.get("sym") == "MNQ1!"


# ---- server-side ORB manager (cancel-loser / cutoff / force-flat) -----------

def _orb_session(**kw):
    base = {"id": 7, "user_id": 1, "symbol": "MNQ1!", "target_name": "",
            "et_date": "2026-07-23", "cutoff_min": 720, "flat_min": 960, "status": "armed"}
    base.update(kw)
    return base


def _orb_mocks(monkeypatch, net, now, entry_filled=False, blocked="", loser_cleared=True, risk_locked=False):
    # cap["pending"] = StopLimit-only loser cancels (SAFE); cap["sweep"] = cancel-ALL-orders calls
    # (would strip a winner's bracket — must be 0 on the fill path); cap["cancel"] = either, for
    # back-compat assertions.
    cap = {"cancel": 0, "pending": 0, "sweep": 0, "flat": 0, "set": []}
    monkeypatch.setattr(appmod, "_orb_now_et", lambda: now)
    monkeypatch.setattr(appmod, "_orb_trading_blocked", lambda u, aid, nowu: blocked)
    monkeypatch.setattr(appmod, "_orb_get_user", lambda uid: {"id": uid, "email": "x", "automation_status": "Running"})
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda u, tn: ([{"account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda env, tok, sym: "MNQU6")
    monkeypatch.setattr(appmod, "_net_position_for", lambda a, r: net)
    monkeypatch.setattr(appmod, "_orb_entry_filled", lambda a, r, since=None: entry_filled)
    monkeypatch.setattr(appmod, "cancel_pending_entries_to_accounts",
                        lambda accts, sym: cap.update(cancel=cap["cancel"] + 1, pending=cap["pending"] + 1) or {})
    monkeypatch.setattr(appmod, "cancel_working_orders_for",
                        lambda a, sym: cap.update(cancel=cap["cancel"] + 1, sweep=cap["sweep"] + 1) or 0)
    monkeypatch.setattr(appmod, "_orb_loser_cleared", lambda accts, sym: loser_cleared)
    monkeypatch.setattr(appmod, "_orb_account_risk_locked",
                        lambda a, fail_closed=True: "Daily loss limit hit." if risk_locked else "")
    monkeypatch.setattr(appmod, "exit_from_accounts",
                        lambda accts, sym: cap.update(flat=cap["flat"] + 1) or {})
    monkeypatch.setattr(appmod, "_orb_set_status", lambda sid, st: cap["set"].append(st))
    return cap


def test_orb_manager_cancels_loser_on_fill(monkeypatch):
    cap = _orb_mocks(monkeypatch, net=2, now=("2026-07-23", 605))     # filled, before cutoff
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["cancel"] == 1 and cap["flat"] == 0 and cap["set"] == ["filled"]


def test_orb_manager_kill_switch_pauses_and_flattens(monkeypatch):
    # Paused automation OR active news lockout -> exit_from_accounts (cancel resting + flatten) + done,
    # BEFORE any fill/cancel logic. This is the fix for "I paused but a resting order still filled".
    cap = _orb_mocks(monkeypatch, net=2, now=("2026-07-23", 615), entry_filled=True, blocked="Automation paused.")
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["flat"] == 1 and cap["cancel"] == 0 and cap["set"] == ["done"]


def test_orb_manager_cancels_loser_on_fast_fill(monkeypatch):
    # REGRESSION: position already flat (net=0) because the trade opened AND took profit inside
    # one poll interval, but a StopLimit entry DID fill -> the loser must STILL be cancelled.
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-23", 615), entry_filled=True)
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["cancel"] == 1 and cap["flat"] == 0 and cap["set"] == ["filled"]


def test_orb_manager_resweeps_entries_on_risk_locked_account(monkeypatch):
    # DEFENSE-IN-DEPTH: no fill yet (net=0), but the risk engine LOCKED the account (daily-loss /
    # drawdown). The manager must re-cancel the locked account's resting entries (StopLimit-only)
    # every poll so a surviving entry can't trigger a new position on a locked account.
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-23", 615), entry_filled=False, risk_locked=True)
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["pending"] == 1 and cap["sweep"] == 0    # StopLimit-only re-sweep, never a full sweep
    assert cap["set"] == []                              # session stays armed; lock handled out-of-band


def test_orb_manager_cutoff_cancels_when_flat(monkeypatch):
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-23", 725))     # flat, past 12:00 cutoff
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["cancel"] == 1 and cap["flat"] == 0 and cap["set"] == ["done"]


def test_orb_manager_idle_before_cutoff(monkeypatch):
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-23", 700))     # flat, before cutoff -> nothing
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["cancel"] == 0 and cap["flat"] == 0 and cap["set"] == []


def test_orb_manager_force_flat_at_eod(monkeypatch):
    cap = _orb_mocks(monkeypatch, net=2, now=("2026-07-23", 965))     # open position, past 16:00
    appmod._orb_manage_one(_orb_session(status="filled"))
    assert cap["flat"] == 1 and cap["cancel"] == 0 and cap["set"] == ["done"]


def test_orb_manager_stale_day_force_flat(monkeypatch):
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-24", 600))     # session left over from prior day
    appmod._orb_manage_one(_orb_session(et_date="2026-07-23", status="armed"))
    assert cap["flat"] == 1 and cap["set"] == ["done"]


def test_orb_manager_fast_fill_cancels_loser_without_sweeping_all(monkeypatch):
    # 2026-08-04 live bug: winner filled AND took profit (net=0) leaving the LOSER entry + its
    # suspended bracket on the other side of the OR. The manager must cancel the loser using the
    # StopLimit-ONLY canceller (which cascade-clears the suspended legs) and MUST NOT sweep all
    # working orders (an aggregate-net sweep could strip an open winner's bracket -> naked position).
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-23", 615), entry_filled=True)
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["pending"] == 1 and cap["sweep"] == 0 and cap["flat"] == 0 and cap["set"] == ["filled"]


def test_orb_manager_stays_armed_until_loser_confirmed_cleared(monkeypatch):
    # A single best-effort cancel can be rejected / race the fill. Until the loser is CONFIRMED
    # gone, the session stays 'armed' and retries next poll instead of going silently quiet.
    cap = _orb_mocks(monkeypatch, net=0, now=("2026-07-23", 615), entry_filled=True, loser_cleared=False)
    appmod._orb_manage_one(_orb_session(status="armed"))
    assert cap["cancel"] == 1 and cap["set"] == []      # cancel attempted, but NOT marked filled


def test_orb_entry_filled_detects_triggered_stoplimit_reported_as_limit(monkeypatch):
    # THE root-cause regression: a stop-limit ENTRY, once triggered, is reported by Tradovate with
    # orderType 'Limit'. The old check required 'StopLimit' and so returned False for every fast
    # fill. The fix must return True for any terminal 'Filled' order on the contract.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "_contract_name", lambda env, tok, cid: "MNQU6")
    monkeypatch.setattr(tvo, "get_orders", lambda env, tok: [
        {"accountId": 44, "ordStatus": "Filled", "contractId": 1, "orderType": "Limit",
         "timestamp": "2026-08-04T14:05:00+00:00"},   # the winner's triggered entry, now 'Limit'
    ])
    assert appmod._orb_entry_filled({"env": "demo", "account_id": 44}, "MNQU6") is True


def test_orb_entry_filled_false_when_nothing_filled(monkeypatch):
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "_contract_name", lambda env, tok, cid: "MNQU6")
    monkeypatch.setattr(tvo, "get_orders", lambda env, tok: [
        {"accountId": 44, "ordStatus": "Working",  "contractId": 1, "timestamp": "2026-08-04T14:05:00+00:00"},
        {"accountId": 44, "ordStatus": "Canceled", "contractId": 1, "timestamp": "2026-08-04T14:05:00+00:00"},
    ])
    assert appmod._orb_entry_filled({"env": "demo", "account_id": 44}, "MNQU6") is False


def test_orb_entry_filled_ignores_prior_session_fill(monkeypatch):
    # A fill from a PRIOR trade on the same contract (older than the session start) must NOT
    # false-trigger a cancel of a fresh session's live entries.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "_contract_name", lambda env, tok, cid: "MNQU6")
    monkeypatch.setattr(tvo, "get_orders", lambda env, tok: [
        {"accountId": 44, "ordStatus": "Filled", "contractId": 1, "orderType": "Limit",
         "timestamp": "2026-08-03T14:05:00+00:00"},   # YESTERDAY's fill
    ])
    since = appmod._parse_iso("2026-08-04T13:55:00+00:00")
    assert appmod._orb_entry_filled({"env": "demo", "account_id": 44}, "MNQU6", since) is False


def test_orb_loser_cleared_gates_on_resting_entries(monkeypatch):
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "resolve_contract", lambda env, tok, sym: "MNQU6")
    accts = [{"env": "demo", "account_id": 44}]
    monkeypatch.setattr(appmod, "_resting_entry_count", lambda a, r: 0)
    assert appmod._orb_loser_cleared(accts, "MNQ1!") is True
    monkeypatch.setattr(appmod, "_resting_entry_count", lambda a, r: 1)   # loser still resting
    assert appmod._orb_loser_cleared(accts, "MNQ1!") is False


def test_orb_entry_filled_fail_closed_on_missing_timestamp(monkeypatch):
    # A Filled order with NO parseable timestamp cannot be proven to belong to THIS session, so with
    # a session scope active it must be IGNORED (fail closed) — otherwise a stale prior-day fill could
    # false-trigger a cancel of a fresh session's live entries.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "_contract_name", lambda env, tok, cid: "MNQU6")
    monkeypatch.setattr(tvo, "get_orders", lambda env, tok: [
        {"accountId": 44, "ordStatus": "Filled", "contractId": 1, "orderType": "Limit"},  # no timestamp
    ])
    since = appmod._parse_iso("2026-08-04T13:55:00+00:00")
    assert appmod._orb_entry_filled({"env": "demo", "account_id": 44}, "MNQU6", since) is False
    # ...but with NO session scope (since_dt=None) it still counts (fixes the original miss).
    assert appmod._orb_entry_filled({"env": "demo", "account_id": 44}, "MNQU6", None) is True


def test_orb_entry_filled_fail_closed_on_unnamed_contract(monkeypatch):
    # A Filled order whose contract can't be named this poll must NOT be attributed to this root
    # (fail closed) — else another instrument's fill could false-trigger a loser cancel.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(appmod, "_contract_name", lambda env, tok, cid: "")   # unresolvable
    monkeypatch.setattr(tvo, "get_orders", lambda env, tok: [
        {"accountId": 44, "ordStatus": "Filled", "contractId": 1, "orderType": "Limit"},
    ])
    assert appmod._orb_entry_filled({"env": "demo", "account_id": 44}, "MNQU6", None) is False


def test_resting_entry_count_fails_closed_on_unnamed_contract(monkeypatch):
    # A working stop-limit whose contract can't be named this poll must be COUNTED as live (the
    # canceller also fails closed on it), so _orb_loser_cleared can't falsely report it gone.
    monkeypatch.setattr(appmod, "ensure_fresh_token", lambda a: "tok")
    monkeypatch.setattr(tvo, "get_orders", lambda env, tok: [{"id": 1}])
    monkeypatch.setattr(tvo, "get_order_versions", lambda env, tok: [])
    monkeypatch.setattr(tvo, "working_orders_for",
                        lambda o, v, aid, order_type=None: [{"id": 1, "orderType": "StopLimit", "contractId": 99}])
    monkeypatch.setattr(appmod, "_contract_name", lambda env, tok, cid: "")   # unresolvable
    assert appmod._resting_entry_count({"env": "demo", "account_id": 44}, "MNQU6") == 1


def test_handler_place_orb_records_server_session(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
                        lambda *a, **k: {"placed": 1, "total": 1, "accounts": 1, "results": []})
    monkeypatch.setattr(appmod, "record_orb_session", lambda *a, **k: cap.update(rec=a))
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 40, "manage": "server", "cutoff": 720, "flat": 960,
        "long": {"entryStop": 30000, "entryLimit": 30003, "sl": 29995, "tp": 30005},
        "short": {"entryStop": 29900, "entryLimit": 29897, "sl": 29905, "tp": 29895}})
    assert r.status_code == 200 and r.json().get("status") == "EXECUTED"
    # record_orb_session(user_id, symbol, target_name, cutoff, flat, tz)
    assert cap.get("rec") and cap["rec"][1] == "MNQ1!" and cap["rec"][3] == 720 and cap["rec"][4] == 960


def test_handler_place_orb_no_session_without_flag(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {"rec": False}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
                        lambda *a, **k: {"placed": 1, "total": 1, "accounts": 1, "results": []})
    monkeypatch.setattr(appmod, "record_orb_session", lambda *a, **k: cap.update(rec=True))
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 2,
        "long": {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020},
        "short": {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}})
    assert r.status_code == 200 and cap["rec"] is False   # no manage flag -> no server session


def test_handler_place_orb_rejected_when_paused(monkeypatch):
    uid, email, secret = _mk_user()
    con = appmod.db(); con.execute("UPDATE users SET automation_status='Paused' WHERE id=?", (uid,)); con.commit(); con.close()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
                        lambda *a, **k: cap.update(ran=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 2,
        "long": {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020},
        "short": {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}})
    assert r.status_code == 200
    b = r.json()
    assert b.get("status") == "REJECTED" and "paused" in b.get("error", "").lower()
    assert "ran" not in cap   # paused -> never even attempted to place


def test_handler_place_orb_rejected_surfaces_reason(monkeypatch):
    uid, email, secret = _mk_user()
    cap = {"recorded": False}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
                        lambda *a, **k: {"placed": 0, "total": 1, "accounts": 1,
                                         "results": [{"account": "A", "ok": False, "error": "Reconnect required"}]})
    monkeypatch.setattr(appmod, "record_orb_session", lambda *a, **k: cap.update(recorded=True))
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 2, "manage": "server", "cutoff": 720, "flat": 960,
        "long": {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020},
        "short": {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}})
    assert r.status_code == 200
    b = r.json()
    # NOT a green EXECUTED — REJECTED with the real reason, and no phantom session recorded
    assert b.get("status") == "REJECTED" and "Reconnect required" in b.get("error", "")
    assert cap["recorded"] is False


def test_handler_place_orb_rejected_during_news(monkeypatch):
    uid, email, secret = _mk_user()
    con = appmod.db()   # an always-active news lockout window (user-wide, account_id NULL)
    con.execute("INSERT INTO news_windows(user_id,account_id,starts_at,ends_at,label,created_at) VALUES(?,?,?,?,?,?)",
                (uid, None, "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00", "CPI", "2026-01-01T00:00:00+00:00"))
    con.commit(); con.close()
    cap = {}
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts",
                        lambda user, tn: ([{"id": 1, "account_name": "A", "account_id": 44, "env": "demo"}], "BROADCAST"))
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
                        lambda *a, **k: cap.update(ran=True) or {"placed": 1, "total": 1, "accounts": 1, "results": []})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 2,
        "long": {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020},
        "short": {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}})
    assert r.status_code == 200
    b = r.json()
    assert b.get("status") == "REJECTED" and "news lockout" in b.get("error", "").lower()
    assert "ran" not in cap   # never placed during the lockout window


def test_handler_place_orb_rejected_no_account(monkeypatch):
    uid, email, secret = _mk_user()
    monkeypatch.setattr(appmod, "webhook_subscription_ok", lambda u: True)
    monkeypatch.setattr(appmod, "_route_signal_accounts", lambda user, tn: ([], "BROADCAST"))   # nothing connected
    monkeypatch.setattr(appmod, "execute_orb_to_accounts",
                        lambda *a, **k: {"placed": 0, "total": 0, "accounts": 0, "results": []})
    r = _client.post("/webhook/trade", json={"client_id": email, "auth": secret, "event": "place_orb",
        "symbol": "MNQ1!", "qty": 2,
        "long": {"entryStop": 30000, "entryLimit": 30002, "sl": 29995, "tp": 30020},
        "short": {"entryStop": 29900, "entryLimit": 29898, "sl": 29905, "tp": 29880}})
    assert r.status_code == 200
    b = r.json()
    assert b.get("status") == "REJECTED" and "connect a broker" in b.get("error", "").lower()
