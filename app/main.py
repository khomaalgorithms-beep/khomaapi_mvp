from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from typing import Literal, Optional, Dict, Any, Tuple
from pathlib import Path
from datetime import datetime, timezone, date
from cryptography.fernet import Fernet
import sqlite3
import hashlib
import os
import secrets
import json
import time
import requests

# ============================================================
# KHOMAAPI v3.1 INSTITUTIONAL
# Direct Tradovate API execution engine
# Features:
# - Client accounts
# - Encrypted Tradovate credentials
# - Real Tradovate API login
# - Real market order routing
# - Simulation/live mode
# - Risk engine
# - Duplicate webhook protection
# - Position-aware execution
# - Same-direction skip
# - Close-and-flip logic
# - Retry logic
# - Emergency rejection lock
# - Execution logs
# - White/green SaaS dashboard
# ============================================================

app = FastAPI(title="KhomaAPI v3.1 Institutional")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "khomaapi_v31.db"
KEY_PATH = BASE_DIR / ".khoma_secret_v31"

if not KEY_PATH.exists():
    KEY_PATH.write_text(Fernet.generate_key().decode(), encoding="utf-8")
FERNET = Fernet(KEY_PATH.read_text(encoding="utf-8").strip().encode())

SESSIONS: Dict[str, int] = {}


# ============================================================
# DATABASE + ENCRYPTION
# ============================================================

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def enc(value: str) -> str:
    return FERNET.encrypt(value.encode()).decode() if value else ""


def dec(value: Optional[str]) -> str:
    return FERNET.decrypt(value.encode()).decode() if value else ""


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 160000).hex()
    return f"{salt}:{hashed}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, old_hash = stored.split(":")
        new_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 160000).hex()
        return secrets.compare_digest(new_hash, old_hash)
    except Exception:
        return False


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        api_key TEXT UNIQUE NOT NULL,
        webhook_secret TEXT UNIQUE NOT NULL,
        automation_status TEXT DEFAULT 'Paused',
        live_mode TEXT DEFAULT 'simulation',
        max_contracts INTEGER DEFAULT 2,
        max_orders INTEGER DEFAULT 10,
        duplicate_seconds INTEGER DEFAULT 8,
        max_rejections_per_day INTEGER DEFAULT 3,
        allowed_symbols TEXT DEFAULT 'MNQ,NQ,MES,ES,MYM,YM',
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS brokers(
        user_id INTEGER PRIMARY KEY,
        env TEXT DEFAULT 'demo',
        username_enc TEXT,
        password_enc TEXT,
        app_id TEXT,
        app_version TEXT DEFAULT '1.0',
        cid TEXT,
        sec_enc TEXT,
        account_spec TEXT,
        account_id TEXT,
        device_id TEXT DEFAULT 'khomaapi-device-001',
        connected INTEGER DEFAULT 0,
        last_error TEXT DEFAULT '',
        last_test TEXT DEFAULT ''
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        ts TEXT,
        request_id TEXT,
        symbol TEXT,
        side TEXT,
        qty INTEGER,
        mode TEXT,
        status TEXT,
        latency_ms REAL,
        message TEXT,
        broker_response TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS locks(
        user_id INTEGER,
        symbol TEXT,
        side TEXT,
        request_id TEXT,
        ts REAL,
        PRIMARY KEY(user_id, symbol, side, request_id)
    )
    """)

    con.commit()
    con.close()


init_db()


# ============================================================
# SESSION HELPERS
# ============================================================

def current_user(request: Request):
    sid = request.cookies.get("khoma_session")
    uid = SESSIONS.get(sid)
    if not uid:
        return None

    con = db()
    user = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return user


def require_user(request: Request):
    return current_user(request)


def mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:10] + "••••••••••••••••" + key[-4:]


# ============================================================
# TRADOVATE API
# ============================================================

def tradovate_base(env: str) -> str:
    return "https://live.tradovateapi.com/v1" if env == "live" else "https://demo.tradovateapi.com/v1"


def get_broker(user_id: int) -> Optional[Dict[str, Any]]:
    con = db()
    row = con.execute("SELECT * FROM brokers WHERE user_id=?", (user_id,)).fetchone()
    con.close()

    if not row:
        return None

    broker = dict(row)
    broker["username"] = dec(broker.get("username_enc"))
    broker["password"] = dec(broker.get("password_enc"))
    broker["sec"] = dec(broker.get("sec_enc"))
    return broker


def tradovate_login(user_id: int) -> Tuple[str, Dict[str, Any]]:
    broker = get_broker(user_id)
    if not broker:
        raise Exception("Broker profile not found.")

    required = ["username", "password", "app_id", "cid", "sec", "account_spec", "account_id"]
    missing = [field for field in required if not broker.get(field)]
    if missing:
        raise Exception("Missing broker fields: " + ", ".join(missing))

    payload = {
        "name": broker["username"],
        "password": broker["password"],
        "appId": broker["app_id"],
        "appVersion": broker["app_version"] or "1.0",
        "cid": broker["cid"],
        "sec": broker["sec"],
        "deviceId": broker["device_id"] or "khomaapi-device-001",
    }

    response = requests.post(
        f"{tradovate_base(broker['env'])}/auth/accesstokenrequest",
        json=payload,
        timeout=15,
    )

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400 or not isinstance(data, dict) or not data.get("accessToken"):
        raise Exception(f"Tradovate login failed: {data}")

    return data["accessToken"], broker


def tv_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def tv_market_order(user_id: int, symbol: str, side: str, qty: int, retries: int = 3) -> Dict[str, Any]:
    """
    Institutional market order wrapper.
    - Retries temporary failures
    - Validates broker response
    - Raises exception if order is rejected
    """
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            token, broker = tradovate_login(user_id)

            payload = {
                "accountSpec": broker["account_spec"],
                "accountId": int(broker["account_id"]),
                "action": "Buy" if side.lower() == "buy" else "Sell",
                "symbol": symbol.upper(),
                "orderQty": int(qty),
                "orderType": "Market",
                "isAutomated": True,
                "deviceId": broker["device_id"] or "khomaapi-device-001",
            }

            response = requests.post(
                f"{tradovate_base(broker['env'])}/order/placeorder",
                json=payload,
                headers=tv_headers(token),
                timeout=8,
            )

            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

            if response.status_code < 400 and isinstance(data, dict):
                if data.get("failureReason") or data.get("failureText"):
                    raise Exception(f"Tradovate rejected order: {data}")
                data["attempt"] = attempt
                return data

            last_error = f"HTTP {response.status_code}: {data}"

        except Exception as e:
            last_error = str(e)

        time.sleep(0.25)

    raise Exception(f"Order FAILED after retries: {last_error}")


def tv_positions(user_id: int):
    """
    Best-effort position lookup.
    Tradovate response shapes may vary by account/platform.
    """
    token, broker = tradovate_login(user_id)
    base = tradovate_base(broker["env"])

    data = []
    for method in ("post", "get"):
        try:
            if method == "post":
                response = requests.post(f"{base}/position/list", headers=tv_headers(token), timeout=15)
            else:
                response = requests.get(f"{base}/position/list", headers=tv_headers(token), timeout=15)

            data = response.json()
            if response.status_code < 400:
                break
        except Exception as e:
            data = {"error": str(e)}

    if isinstance(data, dict) and data.get("error"):
        raise Exception(f"Position lookup failed: {data}")

    return data if isinstance(data, list) else []


def get_current_position(user_id: int, symbol: str):
    """
    Returns current open position for symbol.
    Example return:
    {"size": 1, "side": "long"}
    {"size": -1, "side": "short"}
    None if flat.
    """
    positions = tv_positions(user_id)
    symbol = symbol.upper()

    for position in positions:
        text = json.dumps(position).upper()
        net_pos = int(position.get("netPos", position.get("netPosition", 0)) or 0)

        if net_pos != 0 and symbol in text:
            return {
                "size": net_pos,
                "side": "long" if net_pos > 0 else "short",
                "raw": position,
            }

    return None


def handle_trade_logic(user_id: int, symbol: str, side: str, qty: int):
    """
    Institutional position-aware execution.
    Case 1: Flat -> open
    Case 2: Same direction -> skip
    Case 3: Opposite direction -> close then open
    """
    position = get_current_position(user_id, symbol)

    if position is None:
        result = tv_market_order(user_id, symbol, side, qty)
        return result, "OPENED"

    same_long = position["side"] == "long" and side == "buy"
    same_short = position["side"] == "short" and side == "sell"

    if same_long or same_short:
        return {"skipped": True, "position": position}, "SKIPPED_SAME_DIRECTION"

    close_side = "sell" if position["side"] == "long" else "buy"
    close_qty = abs(position["size"])

    close_result = tv_market_order(user_id, symbol, close_side, close_qty)
    time.sleep(0.35)
    open_result = tv_market_order(user_id, symbol, side, qty)

    return {
        "closed": close_result,
        "opened": open_result,
        "previous_position": position,
    }, "FLIPPED"


def safe_flatten_symbol(user_id: int, symbol: str):
    symbol = symbol.upper()
    position = get_current_position(user_id, symbol)

    if position is None:
        return {
            "flattened": False,
            "message": "No matching open position found.",
        }

    close_side = "sell" if position["side"] == "long" else "buy"
    close_qty = abs(position["size"])
    result = tv_market_order(user_id, symbol, close_side, close_qty)

    return {
        "flattened": True,
        "close_side": close_side,
        "close_qty": close_qty,
        "order": result,
    }


# ============================================================
# LOGGING + RISK ENGINE
# ============================================================

def log_trade(user_id, request_id, symbol, side, qty, mode, status, latency_ms, message, response):
    con = db()
    con.execute(
        """
        INSERT INTO trades(user_id,ts,request_id,symbol,side,qty,mode,status,latency_ms,message,broker_response)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            user_id,
            datetime.now(timezone.utc).isoformat(),
            request_id,
            symbol,
            side,
            qty,
            mode,
            status,
            latency_ms,
            message,
            json.dumps(response or {}),
        ),
    )
    con.commit()
    con.close()


def today_order_count(user_id: int) -> int:
    con = db()
    row = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM trades
        WHERE user_id=?
        AND ts LIKE ?
        AND status IN ('SIMULATED','EXECUTED','SENT_TO_BROKER')
        """,
        (user_id, date.today().isoformat() + "%"),
    ).fetchone()
    con.close()
    return int(row["n"] or 0)


def emergency_risk_check(user_id: int):
    con = db()
    user = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    rejected = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM trades
        WHERE user_id=?
        AND ts LIKE ?
        AND status='REJECTED'
        """,
        (user_id, date.today().isoformat() + "%"),
    ).fetchone()["n"]
    con.close()

    max_rejections = int(user["max_rejections_per_day"] or 3) if user else 3

    if int(rejected or 0) >= max_rejections:
        raise Exception(f"SYSTEM LOCK: Too many rejected trades today ({rejected}/{max_rejections}).")


def broker_connection_check(user_id: int):
    con = db()
    broker = con.execute("SELECT * FROM brokers WHERE user_id=?", (user_id,)).fetchone()
    con.close()

    if not broker:
        raise Exception("Broker profile not found.")
    if not broker["connected"]:
        raise Exception("Broker not connected. Go to Broker Keys and click Test Connection first.")


def check_duplicate(user, symbol: str, side: str, request_id: str):
    now = time.time()
    window = int(user["duplicate_seconds"] or 8)

    con = db()
    con.execute("DELETE FROM locks WHERE user_id=? AND ts < ?", (user["id"], now - window))

    existing = con.execute(
        """
        SELECT * FROM locks
        WHERE user_id=? AND symbol=? AND side=? AND request_id=?
        """,
        (user["id"], symbol, side, request_id),
    ).fetchone()

    if existing:
        con.close()
        raise Exception("Duplicate webhook blocked.")

    con.execute(
        """
        INSERT OR REPLACE INTO locks(user_id,symbol,side,request_id,ts)
        VALUES(?,?,?,?,?)
        """,
        (user["id"], symbol, side, request_id, now),
    )
    con.commit()
    con.close()


def risk_check(user, auth: str, symbol: str, side: str, qty: int, request_id: str):
    if auth != user["webhook_secret"]:
        raise Exception("Invalid webhook secret.")

    if user["automation_status"] != "Running":
        raise Exception("Automation paused.")

    symbol = symbol.upper().strip()
    allowed = [item.strip().upper() for item in user["allowed_symbols"].split(",") if item.strip()]

    if symbol not in allowed:
        raise Exception(f"Symbol {symbol} not allowed.")

    if side not in ["buy", "sell"]:
        raise Exception("Side must be buy or sell.")

    if qty < 1 or qty > int(user["max_contracts"]):
        raise Exception(f"Qty violates max contract limit: {user['max_contracts']}.")

    if today_order_count(user["id"]) >= int(user["max_orders"]):
        raise Exception("Daily order limit reached.")

    emergency_risk_check(user["id"])
    check_duplicate(user, symbol, side, request_id)

    return symbol


# ============================================================
# UI DESIGN
# ============================================================

def layout(content, user=None, active="overview"):
    email = user["email"] if user else "Guest"
    initials = email[:1].upper() if email else "K"
    status = user["automation_status"] if user else "Paused"

    return f"""
<!DOCTYPE html>
<html>
<head>
<title>KhomaAPI v3.1</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
:root {{
  --green:#108b3e;
  --green2:#eaf7ef;
  --text:#111827;
  --muted:#6b7280;
  --line:#e5e7eb;
  --bg:#fbfcfb;
  --danger:#dc2626;
  --warning:#ca8a04;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:Inter,-apple-system,BlinkMacSystemFont,Arial,sans-serif; }}
.shell {{ display:flex; min-height:100vh; }}
.sidebar {{ width:278px; background:white; border-right:1px solid var(--line); padding:26px 16px; position:fixed; top:0; bottom:0; left:0; }}
.brand {{ display:flex; align-items:center; gap:12px; padding:4px 10px 42px; }}
.logo {{ width:38px; height:38px; border-radius:12px; background:linear-gradient(135deg,#10a64a,#0b6c35); color:white; display:flex; align-items:center; justify-content:center; font-weight:900; }}
.brand h1 {{ font-size:25px; margin:0; letter-spacing:-.8px; }}
.nav a {{ display:flex; gap:12px; align-items:center; padding:14px 15px; color:#374151; text-decoration:none; border-radius:10px; margin-bottom:8px; font-size:15px; }}
.nav a.active,.nav a:hover {{ background:var(--green2); color:var(--green); }}
.help {{ position:absolute; left:16px; right:16px; bottom:24px; background:linear-gradient(135deg,#eefaf2,#fff); border:1px solid #dcefe3; border-radius:14px; padding:18px; }}
.help b {{ color:var(--green); }} .help p {{ color:var(--muted); font-size:14px; line-height:1.5; }}
.main {{ margin-left:278px; flex:1; }}
.topbar {{ height:62px; border-bottom:1px solid var(--line); background:white; display:flex; justify-content:flex-end; align-items:center; padding:0 28px; gap:22px; }}
.operational {{ color:var(--green); font-weight:700; font-size:14px; }}
.avatar {{ width:42px; height:42px; border-radius:999px; background:var(--green2); color:var(--green); display:flex; align-items:center; justify-content:center; font-weight:800; }}
.content {{ padding:32px 40px 50px; }}
.header h2 {{ margin:0; font-size:30px; letter-spacing:-1px; }} .header p {{ color:var(--muted); margin:8px 0 28px; }}
.grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:22px; }}
.card {{ background:white; border:1px solid var(--line); border-radius:12px; padding:24px; box-shadow:0 8px 30px rgba(17,24,39,.025); }}
.span4 {{ grid-column:span 4; }} .span5 {{ grid-column:span 5; }} .span6 {{ grid-column:span 6; }} .span7 {{ grid-column:span 7; }} .span12 {{ grid-column:span 12; }}
.card h3 {{ margin:0 0 10px; font-size:17px; }} .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
.metric {{ font-size:32px; font-weight:900; margin:16px 0 8px; letter-spacing:-1px; }}
.good {{ color:var(--green)!important; }} .bad {{ color:var(--danger)!important; }} .warn {{ color:var(--warning)!important; }}
.pill {{ display:inline-flex; align-items:center; gap:7px; padding:7px 10px; border-radius:8px; font-size:13px; font-weight:700; background:var(--green2); color:var(--green); }}
.keybox {{ border:1px solid var(--line); border-radius:8px; padding:16px; display:flex; justify-content:space-between; gap:12px; font-family:monospace; overflow:auto; background:#fcfcfd; }}
.steps {{ display:grid; grid-template-columns:repeat(4,1fr); gap:18px; margin-top:18px; }}
.stepnum {{ width:28px; height:28px; border-radius:999px; background:var(--green); color:white; display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:800; box-shadow:0 0 0 8px var(--green2); }}
.chart {{ height:150px; display:flex; align-items:end; gap:12px; border-bottom:1px solid var(--line); padding-top:15px; }}
.bar {{ width:16px; border-radius:4px 4px 0 0; background:linear-gradient(180deg,#12a150,#087135); }}
input,select {{ width:100%; padding:13px 14px; border:1px solid var(--line); border-radius:10px; margin:8px 0 14px; outline:none; }}
input:focus,select:focus {{ border-color:var(--green); box-shadow:0 0 0 4px var(--green2); }}
.formgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
button,.btn {{ border:none; background:var(--green); color:white; padding:12px 15px; border-radius:10px; text-decoration:none; font-weight:800; display:inline-block; cursor:pointer; margin:4px 6px 4px 0; }}
.btn.secondary,button.secondary {{ background:white; color:#374151; border:1px solid var(--line); }} .btn.danger,button.danger {{ background:var(--danger); }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:12px 8px; font-size:14px; }} th {{ font-size:12px; color:#6b7280; text-transform:uppercase; letter-spacing:.06em; }}
@media(max-width:1050px) {{ .sidebar{{position:relative;width:100%;}} .main{{margin-left:0;}} .shell{{display:block;}} .span4,.span5,.span6,.span7{{grid-column:span 12;}} .steps,.formgrid{{grid-template-columns:1fr;}} }}
</style>
</head>
<body>
<div class="shell">
<aside class="sidebar">
<div class="brand"><div class="logo">K</div><h1>Khoma</h1></div>
<div class="nav">
<a class="{'active' if active=='overview' else ''}" href="/dashboard">⌂ Overview</a>
<a class="{'active' if active=='broker' else ''}" href="/broker">⌁ Broker Keys</a>
<a class="{'active' if active=='risk' else ''}" href="/risk">☰ Risk Engine</a>
<a class="{'active' if active=='webhooks' else ''}" href="/webhooks">⌘ Webhooks</a>
<a class="{'active' if active=='logs' else ''}" href="/logs">▥ Usage & Logs</a>
<a href="/docs">◇ API Docs</a>
<a href="/logout">↩ Logout</a>
</div>
<div class="help"><b>Execution Note</b><p>Use demo mode first. Live mode routes real orders and should only be enabled after broker and risk tests pass.</p></div>
</aside>
<main class="main"><div class="topbar"><div class="operational">● {status}</div><div class="avatar">{initials}</div></div><div class="content">{content}</div></main>
</div>
</body>
</html>
"""


def login_layout(content):
    return f"""
<!DOCTYPE html><html><head><title>KhomaAPI</title><style>
body{{margin:0;font-family:Arial;background:#fbfcfb;color:#111827;}}
.wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at top left,#dff5e7,transparent 35%),#fbfcfb;}}
.card{{width:460px;background:white;border:1px solid #e5e7eb;border-radius:16px;padding:30px;box-shadow:0 20px 80px rgba(0,0,0,.08);}}
.logo{{width:44px;height:44px;border-radius:13px;background:#108b3e;color:white;display:flex;align-items:center;justify-content:center;font-weight:900;margin-bottom:18px;}}
input{{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:10px;margin:8px 0 14px;box-sizing:border-box;}}
button,.btn{{background:#108b3e;color:white;border:none;padding:13px 16px;border-radius:10px;font-weight:800;text-decoration:none;display:inline-block;}}
p{{color:#6b7280;line-height:1.5;}} a{{color:#108b3e;font-weight:800;}}
</style></head><body><div class="wrap"><div class="card">{content}</div></div></body></html>
"""


# ============================================================
# AUTH ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/dashboard" if current_user(request) else "/login")


@app.get("/signup", response_class=HTMLResponse)
def signup_page():
    return login_layout('''
    <div class="logo">K</div>
    <h1>Create KhomaAPI account</h1>
    <p>Client account for broker execution, webhook routing, risk limits, and execution logs.</p>
    <form method="post" action="/signup">
      <input name="email" placeholder="Client email" required>
      <input name="password" type="password" placeholder="Password" required>
      <button>Create Account</button>
    </form>
    <p>Already have an account? <a href="/login">Login</a></p>
    ''')


@app.post("/signup")
def signup(email: str = Form(...), password: str = Form(...)):
    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at)
            VALUES(?,?,?,?,?)
            """,
            (
                email.lower().strip(),
                hash_password(password),
                "khoma_live_" + secrets.token_urlsafe(24),
                secrets.token_hex(20),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        uid = cur.lastrowid
        cur.execute("INSERT INTO brokers(user_id) VALUES(?)", (uid,))
        con.commit()
    except Exception as e:
        con.close()
        return login_layout(f"<h1>Account exists or failed.</h1><p>{str(e)}</p><a href='/login'>Go to login</a>")

    con.close()
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return login_layout('''
    <div class="logo">K</div>
    <h1>Welcome back</h1>
    <p>Login to your KhomaAPI execution dashboard.</p>
    <form method="post" action="/login">
      <input name="email" placeholder="Email" required>
      <input name="password" type="password" placeholder="Password" required>
      <button>Login</button>
    </form>
    <p>New client? <a href="/signup">Create account</a></p>
    ''')


@app.post("/login")
def login(email: str = Form(...), password: str = Form(...)):
    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    con.close()

    if not user or not verify_password(password, user["password_hash"]):
        return login_layout("<h1>Invalid login</h1><p>Email or password is wrong.</p><a href='/login'>Try again</a>")

    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = user["id"]
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie("khoma_session", sid, httponly=True, samesite="lax")
    return response


@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("khoma_session")
    SESSIONS.pop(sid, None)
    response = RedirectResponse("/login")
    response.delete_cookie("khoma_session")
    return response


# ============================================================
# UI ROUTES
# ============================================================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    broker = con.execute("SELECT * FROM brokers WHERE user_id=?", (user["id"],)).fetchone()
    trades = con.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 8", (user["id"],)).fetchall()
    con.close()

    rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['mode']}</td><td>{t['status']}</td><td>{t['latency_ms']}</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='7'>No trades yet.</td></tr>"

    chart = "".join([f"<div class='bar' style='height:{h}px'></div>" for h in [42,55,36,74,28,66,35,44,61,51,82,49,38,56,72,46,64,30,58,43,68,53,77,41]])

    content = f'''
    <div class="header"><h2>Overview</h2><p>Execution infrastructure for TradingView webhook routing into Tradovate API.</p></div>
    <div class="grid">
      <div class="card span6"><h3>Your API Key</h3><p class="muted">Use this key internally for client-level authentication.</p><div class="keybox"><span>{mask_key(user['api_key'])}</span><span>copy</span></div><p class="muted">Never expose this key publicly.</p></div>
      <div class="card span6"><h3>System Status <span class="pill">● Online</span></h3><p class="muted">Broker status</p><div class="metric {'good' if broker['connected'] else 'bad'}">{'Connected' if broker['connected'] else 'Disconnected'}</div><p class="muted">Automation: <b>{user['automation_status']}</b> | Mode: <b>{user['live_mode'].upper()}</b></p></div>
      <div class="card span12"><h3>Institutional Control Center</h3><div class="steps"><div class="step"><div class="stepnum">1</div><h4>Connect Broker</h4><p class="muted">Save and test Tradovate credentials.</p></div><div class="step"><div class="stepnum">2</div><h4>Set Risk</h4><p class="muted">Max contracts, symbols, duplicate lock, rejection lock.</p></div><div class="step"><div class="stepnum">3</div><h4>Start Engine</h4><p class="muted">Enable execution after demo testing.</p></div><div class="step"><div class="stepnum">4</div><h4>Route Orders</h4><p class="muted">TradingView sends JSON into KhomaAPI.</p></div></div></div>
      <div class="card span7"><h3>API Usage</h3><div class="metric">{today_order_count(user['id'])}</div><p class="muted">Accepted execution requests today</p><div class="chart">{chart}</div></div>
      <div class="card span5"><h3>Automation</h3><p class="muted">Start only after demo broker connection and risk rules pass.</p><a class="btn" href="/start">Start</a><a class="btn secondary" href="/pause">Pause</a><a class="btn danger" href="/flatten-form">Emergency Flatten</a></div>
      <div class="card span12"><h3>Recent Execution Logs</h3><table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Mode</th><th>Status</th><th>Latency</th></tr>{rows}</table></div>
    </div>
    '''
    return layout(content, user, "overview")


@app.get("/broker", response_class=HTMLResponse)
def broker_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    broker = con.execute("SELECT * FROM brokers WHERE user_id=?", (user["id"],)).fetchone()
    con.close()

    content = f'''
    <div class="header"><h2>Broker Keys</h2><p>Save Tradovate credentials and test direct API connection.</p></div>
    <div class="card"><h3>Tradovate API Credentials</h3><p class="muted">Use demo first. Credentials are encrypted locally.</p>
    <form method="post" action="/broker/save"><div class="formgrid">
      <div><label>Environment</label><select name="env"><option value="demo" {'selected' if broker['env']=='demo' else ''}>Demo</option><option value="live" {'selected' if broker['env']=='live' else ''}>Live</option></select></div>
      <div><label>Device ID</label><input name="device_id" value="{broker['device_id'] or 'khomaapi-device-001'}"></div>
      <div><label>Username</label><input name="username" placeholder="Tradovate username"></div>
      <div><label>Password</label><input name="password" type="password" placeholder="Tradovate password"></div>
      <div><label>App ID</label><input name="app_id" value="{broker['app_id'] or ''}"></div>
      <div><label>App Version</label><input name="app_version" value="{broker['app_version'] or '1.0'}"></div>
      <div><label>CID</label><input name="cid" value="{broker['cid'] or ''}"></div>
      <div><label>SEC</label><input name="sec" type="password" placeholder="SEC"></div>
      <div><label>Account Spec</label><input name="account_spec" value="{broker['account_spec'] or ''}" placeholder="Example: DEMO12345"></div>
      <div><label>Account ID</label><input name="account_id" value="{broker['account_id'] or ''}" placeholder="Numeric account ID"></div>
    </div><button>Save Credentials</button><a class="btn secondary" href="/broker/test">Test Connection</a></form>
    <p class="muted">Status: <b class="{'good' if broker['connected'] else 'bad'}">{'Connected' if broker['connected'] else 'Disconnected'}</b></p>
    <p class="muted">Last Error: {broker['last_error'] or 'None'}</p></div>
    '''
    return layout(content, user, "broker")


@app.post("/broker/save")
def broker_save(
    request: Request,
    env: str = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    app_id: str = Form(""),
    app_version: str = Form("1.0"),
    cid: str = Form(""),
    sec: str = Form(""),
    account_spec: str = Form(""),
    account_id: str = Form(""),
    device_id: str = Form("khomaapi-device-001"),
):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    old = con.execute("SELECT * FROM brokers WHERE user_id=?", (user["id"],)).fetchone()

    username_enc = enc(username) if username else old["username_enc"]
    password_enc = enc(password) if password else old["password_enc"]
    sec_enc = enc(sec) if sec else old["sec_enc"]

    con.execute(
        """
        UPDATE brokers
        SET env=?, username_enc=?, password_enc=?, app_id=?, app_version=?, cid=?, sec_enc=?,
            account_spec=?, account_id=?, device_id=?, connected=0, last_error=''
        WHERE user_id=?
        """,
        (env, username_enc, password_enc, app_id, app_version, cid, sec_enc, account_spec, account_id, device_id, user["id"]),
    )
    con.commit()
    con.close()
    return RedirectResponse("/broker", status_code=302)


@app.get("/broker/test")
def broker_test(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    try:
        tradovate_login(user["id"])
        con.execute(
            "UPDATE brokers SET connected=1,last_error='',last_test=? WHERE user_id=?",
            (datetime.now(timezone.utc).isoformat(), user["id"]),
        )
    except Exception as e:
        con.execute(
            "UPDATE brokers SET connected=0,last_error=?,last_test=? WHERE user_id=?",
            (str(e), datetime.now(timezone.utc).isoformat(), user["id"]),
        )
    con.commit()
    con.close()
    return RedirectResponse("/broker")


@app.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    content = f'''
    <div class="header"><h2>Risk Engine</h2><p>Protect every account before orders are routed.</p></div>
    <div class="card"><form method="post" action="/risk/save"><div class="formgrid">
      <div><label>Webhook Secret</label><input name="webhook_secret" value="{user['webhook_secret']}"></div>
      <div><label>Allowed Symbols</label><input name="allowed_symbols" value="{user['allowed_symbols']}"></div>
      <div><label>Max Contracts Per Order</label><input name="max_contracts" value="{user['max_contracts']}"></div>
      <div><label>Max Orders Per Day</label><input name="max_orders" value="{user['max_orders']}"></div>
      <div><label>Duplicate Lock Seconds</label><input name="duplicate_seconds" value="{user['duplicate_seconds']}"></div>
      <div><label>Max Rejections Per Day</label><input name="max_rejections_per_day" value="{user['max_rejections_per_day']}"></div>
      <div><label>Execution Mode</label><select name="live_mode"><option value="simulation" {'selected' if user['live_mode']=='simulation' else ''}>Simulation Only</option><option value="live" {'selected' if user['live_mode']=='live' else ''}>Live Broker Orders</option></select></div>
    </div><button>Save Risk Settings</button></form></div>
    '''
    return layout(content, user, "risk")


@app.post("/risk/save")
def risk_save(
    request: Request,
    webhook_secret: str = Form(...),
    allowed_symbols: str = Form(...),
    max_contracts: int = Form(...),
    max_orders: int = Form(...),
    duplicate_seconds: int = Form(...),
    max_rejections_per_day: int = Form(...),
    live_mode: str = Form(...),
):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    con.execute(
        """
        UPDATE users
        SET webhook_secret=?, allowed_symbols=?, max_contracts=?, max_orders=?,
            duplicate_seconds=?, max_rejections_per_day=?, live_mode=?
        WHERE id=?
        """,
        (webhook_secret, allowed_symbols, max_contracts, max_orders, duplicate_seconds, max_rejections_per_day, live_mode, user["id"]),
    )
    con.commit()
    con.close()
    return RedirectResponse("/risk", status_code=302)


@app.get("/webhooks", response_class=HTMLResponse)
def webhooks_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    example = '{\n  "client_id": "' + user['email'] + '",\n  "auth": "' + user['webhook_secret'] + '",\n  "symbol": "MNQ",\n  "side": "buy",\n  "qty": 1,\n  "request_id": "{{strategy.order.id}}"\n}'

    content = f'''
    <div class="header"><h2>Webhooks</h2><p>Use this JSON from TradingView alerts.</p></div>
    <div class="card"><h3>Trade Webhook</h3><div class="keybox">http://127.0.0.1:8000/webhook/trade</div><pre style="background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;padding:18px;overflow:auto;">{example}</pre><h3>Flatten Webhook</h3><div class="keybox">http://127.0.0.1:8000/webhook/flatten</div></div>
    '''
    return layout(content, user, "webhooks")


@app.get("/start")
def start(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    con.execute("UPDATE users SET automation_status='Running' WHERE id=?", (user["id"],))
    con.commit()
    con.close()
    return RedirectResponse("/dashboard")


@app.get("/pause")
def pause(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    con.execute("UPDATE users SET automation_status='Paused' WHERE id=?", (user["id"],))
    con.commit()
    con.close()
    return RedirectResponse("/dashboard")


@app.get("/flatten-form", response_class=HTMLResponse)
def flatten_form(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    return layout('''
    <div class="header"><h2>Emergency Flatten</h2><p>Enter the exact Tradovate contract symbol you want to flatten.</p></div>
    <div class="card"><form method="post" action="/flatten"><input name="symbol" placeholder="Example: MNQM6" required><button class="danger">Flatten Symbol</button></form></div>
    ''', user, "overview")


@app.post("/flatten")
def flatten_post(request: Request, symbol: str = Form(...)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    start_time = time.perf_counter()
    try:
        if user["live_mode"] != "live":
            raise Exception("Flatten blocked because account is in simulation mode.")

        broker_connection_check(user["id"])
        result = safe_flatten_symbol(user["id"], symbol.upper())
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], "manual-flatten", symbol.upper(), "flatten", 0, "live", "FLATTEN_SENT", latency, "Flatten attempted.", result)
    except Exception as e:
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], "manual-flatten", symbol.upper(), "flatten", 0, "rejected", "REJECTED", latency, str(e), {})

    return RedirectResponse("/logs", status_code=302)


@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    trades = con.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 150", (user["id"],)).fetchall()
    con.close()

    rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['request_id']}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['mode']}</td><td>{t['status']}</td><td>{t['latency_ms']}</td><td>{t['message']}</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='9'>No logs.</td></tr>"

    content = f'''
    <div class="header"><h2>Usage & Logs</h2><p>Every accepted, rejected, simulated, and live broker request is recorded.</p></div>
    <div class="card"><table><tr><th>Time</th><th>Request ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Mode</th><th>Status</th><th>Latency</th><th>Message</th></tr>{rows}</table></div>
    '''
    return layout(content, user, "logs")


# ============================================================
# WEBHOOK API
# ============================================================

class WebhookTrade(BaseModel):
    client_id: str
    auth: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int = 1
    request_id: Optional[str] = None


class WebhookFlatten(BaseModel):
    client_id: str
    auth: str
    symbol: str
    request_id: Optional[str] = None


@app.post("/webhook/trade")
def webhook_trade(payload: WebhookTrade):
    start_time = time.perf_counter()
    request_id = payload.request_id or secrets.token_hex(8)

    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (payload.client_id.lower().strip(),)).fetchone()
    con.close()

    if not user:
        return JSONResponse(status_code=200, content={"ok": False, "error": "Client not found."})

    try:
        symbol = risk_check(user, payload.auth, payload.symbol, payload.side, payload.qty, request_id)

        if user["live_mode"] == "live":
            broker_connection_check(user["id"])
            response, action = handle_trade_logic(user["id"], symbol, payload.side, payload.qty)
            mode = "live"
            status = "EXECUTED" if action != "SKIPPED_SAME_DIRECTION" else "SKIPPED"
            message = action
        else:
            response = {"simulated": True}
            action = "SIMULATED"
            mode = "simulation"
            status = "SIMULATED"
            message = "Simulation accepted. No real broker order sent."

        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, symbol, payload.side, payload.qty, mode, status, latency, message, response)

        return {
            "ok": True,
            "action": action,
            "status": status,
            "mode": mode,
            "latency_ms": latency,
            "response": response,
        }

    except Exception as e:
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, payload.symbol.upper(), payload.side, payload.qty, "rejected", "REJECTED", latency, str(e), {})
        return {
            "ok": False,
            "error": str(e),
            "latency_ms": latency,
        }


@app.post("/webhook/flatten")
def webhook_flatten(payload: WebhookFlatten):
    start_time = time.perf_counter()
    request_id = payload.request_id or secrets.token_hex(8)

    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (payload.client_id.lower().strip(),)).fetchone()
    con.close()

    if not user:
        return {"ok": False, "error": "Client not found."}

    try:
        if payload.auth != user["webhook_secret"]:
            raise Exception("Invalid webhook secret.")

        if user["live_mode"] != "live":
            raise Exception("Flatten blocked in simulation mode.")

        broker_connection_check(user["id"])
        result = safe_flatten_symbol(user["id"], payload.symbol.upper())
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, payload.symbol.upper(), "flatten", 0, "live", "FLATTEN_SENT", latency, "Flatten attempted.", result)

        return {
            "ok": True,
            "latency_ms": latency,
            "result": result,
        }

    except Exception as e:
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, payload.symbol.upper(), "flatten", 0, "rejected", "REJECTED", latency, str(e), {})
        return {
            "ok": False,
            "error": str(e),
            "latency_ms": latency,
        }


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": "KhomaAPI v3.1 Institutional",
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/trades")
def api_trades(request: Request):
    user = require_user(request)
    if not user:
        return {"ok": False, "error": "not authenticated"}

    con = db()
    rows = con.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 100", (user["id"],)).fetchall()
    con.close()

    return [dict(row) for row in rows]
@app.get("/auth/tradovate/connect")
def tradovate_connect():

    client_id = "13286"

    redirect_uri = "https://web-production-6ad48.up.railway.app/auth/callback"

    oauth_url = (
        "https://trader.tradovateapi.com/oauth/authorize"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
    )

    return RedirectResponse(oauth_url)

@app.get("/auth/callback")
def tradovate_callback(code: str = ""):

    return {
        "ok": True,
        "message": "OAuth callback received.",
        "code": code
    }