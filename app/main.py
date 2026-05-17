from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from typing import Optional, Dict, Any, Tuple
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

import re
import smtplib

from email.mime.text import MIMEText
from email_validator import validate_email, EmailNotValidError




app = FastAPI(title="KhomaAPI v5")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = os.path.join(BASE_DIR, "static")

print("STATIC DIR:", STATIC_DIR)
print("STATIC EXISTS:", os.path.exists(STATIC_DIR))

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)

@app.get("/debug-static")
def debug_static():
    return {
        "base_dir": str(BASE_DIR),
        "static_dir": STATIC_DIR,
        "exists": os.path.exists(STATIC_DIR),
        "files": os.listdir(STATIC_DIR) if os.path.exists(STATIC_DIR) else []
    }

DB_PATH = BASE_DIR / "khomaapi_v31.db"
KEY_PATH = BASE_DIR / ".khoma_secret_v31"

if not KEY_PATH.exists():
    KEY_PATH.write_text(Fernet.generate_key().decode(), encoding="utf-8")
FERNET = Fernet(KEY_PATH.read_text(encoding="utf-8").strip().encode())

SESSIONS: Dict[str, int] = {}

APP_URL = os.getenv("APP_URL", "https://web-production-6ad48.up.railway.app")


# ============================================================
# WEBSOCKET LIVE CONNECTIONS
# ============================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        disconnected = []

        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                disconnected.append(connection)

        for dead in disconnected:
            self.disconnect(dead)

manager = ConnectionManager()



# ============================================================
# DATABASE + SECURITY
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
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 180000).hex()
    return f"{salt}:{hashed}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, old_hash = stored.split(":")
        new_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 180000).hex()
        return secrets.compare_digest(new_hash, old_hash)
    except Exception:
        return False


def strong_password(password: str):

    if len(password) < 8:
        return False, "Password must be at least 8 characters."

    if not re.search(r"[A-Z]", password):
        return False, "Password must contain uppercase letter."

    if not re.search(r"[a-z]", password):
        return False, "Password must contain lowercase letter."

    if not re.search(r"\d", password):
        return False, "Password must contain number."

    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]", password):
        return False, "Password must contain special character."

    return True, "Strong password"


def valid_email(email: str):
    try:
        validate_email(email)
        return True
    except EmailNotValidError:
        return False



def send_email(to_email, subject, body):

    try:
        host = os.getenv("SMTP_HOST")
        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASS")

        if not host or not user or not password:
            print("SMTP VARIABLES MISSING")
            return False

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_email

        server = smtplib.SMTP(host, port, timeout=10)

        server.starttls()

        server.login(user, password)

        server.sendmail(user, [to_email], msg.as_string())

        server.quit()

        
        print("EMAIL SENT TO:", to_email)


        return True

    except Exception as e:
        print("EMAIL ERROR:", str(e))
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
        allowed_symbols TEXT DEFAULT '*',
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
        access_token_enc TEXT,
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

    # Add columns safely if old DB already exists.
    try:
        cur.execute("ALTER TABLE brokers ADD COLUMN access_token_enc TEXT")
    except Exception:
        pass

    # Upgrade existing users to allow all symbols by default if they still have restricted list.
    # You can still restrict this later from Risk Engine.
    cur.execute("UPDATE users SET allowed_symbols='*' WHERE allowed_symbols IS NULL OR allowed_symbols='' OR allowed_symbols='MNQ,NQ,MES,ES,MYM,YM' OR allowed_symbols='MNQ,MNQM6,NQ,MES,ES,MYM,YM'")

    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS email_verifications(
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        new_email TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS password_resets(
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        created_at TEXT
    )
    """)


    con.commit()
    con.close()

    try:
        import asyncio

        asyncio.create_task(
            manager.broadcast({
                "event": "trade",
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "status": status,
                "latency_ms": latency_ms,
                "message": message,
                "time": datetime.now(timezone.utc).isoformat()
            })
        )
    except Exception:
        pass


init_db()


@app.get("/test")
def test():
    return {"working": True}


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


def mask_value(value: str, visible: int = 5) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "••••••"
    return value[:visible] + "••••••••" + value[-visible:]


# ============================================================
# TRADOVATE API CONFIG
# ============================================================

def tradovate_base(env: str) -> str:
    return "https://live.tradovateapi.com" if env == "live" else "https://demo.tradovateapi.com"


def tv_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_backend_tradovate_config(user_id: int) -> Dict[str, str]:
    cid = os.getenv("TRADOVATE_CID", "").strip()
    sec = os.getenv("TRADOVATE_SEC", "").strip()
    app_id = os.getenv("TRADOVATE_APP_ID", "KhomaAPI").strip() or "KhomaAPI"
    app_version = os.getenv("TRADOVATE_APP_VERSION", "1.0").strip() or "1.0"
    device_id = f"khomaapi-cloud-{user_id}"

    if not cid or not sec:
        raise Exception("TRADOVATE_CID or TRADOVATE_SEC is missing in Railway variables.")

    return {
        "cid": cid,
        "sec": sec,
        "app_id": app_id,
        "app_version": app_version,
        "device_id": device_id,
    }


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
    broker["access_token"] = dec(broker.get("access_token_enc")) if broker.get("access_token_enc") else ""
    return broker


def tradovate_login_raw(env: str, username: str, password: str, user_id: int) -> Tuple[str, Dict[str, Any]]:
    cfg = get_backend_tradovate_config(user_id)

    payload = {
        "name": username,
        "password": password,
        "appId": cfg["app_id"],
        "appVersion": cfg["app_version"],
        "cid": cfg["cid"],
        "sec": cfg["sec"],
        "deviceId": cfg["device_id"],
    }

    response = requests.post(
        f"{tradovate_base(env)}/auth/accesstokenrequest",
        json=payload,
        timeout=15,
    )

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400 or not isinstance(data, dict) or not data.get("accessToken"):
        raise Exception(f"Tradovate login failed: {data}")

    return data["accessToken"], cfg


def fetch_tradovate_accounts(env: str, token: str):
    response = requests.get(
        f"{tradovate_base(env)}/account/list",
        headers=tv_headers(token),
        timeout=15,
    )

    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}

    if response.status_code >= 400 or not isinstance(data, list) or len(data) == 0:
        raise Exception(f"Could not fetch Tradovate accounts: {data}")

    active_accounts = [a for a in data if not a.get("closed") and not a.get("archived") and a.get("active", True)]
    return active_accounts or data


def tradovate_login(user_id: int) -> Tuple[str, Dict[str, Any]]:
    broker = get_broker(user_id)
    if not broker:
        raise Exception("Broker profile not found.")

    if not broker.get("username") or not broker.get("password"):
        raise Exception("Broker username/password missing. Go to Broker Connect.")

    if not broker.get("account_spec") or not broker.get("account_id"):
        raise Exception("Broker account not detected. Reconnect broker.")

    token, cfg = tradovate_login_raw(broker.get("env", "demo"), broker["username"], broker["password"], user_id)

    broker["app_id"] = cfg["app_id"]
    broker["app_version"] = cfg["app_version"]
    broker["cid"] = cfg["cid"]
    broker["sec"] = cfg["sec"]
    broker["device_id"] = cfg["device_id"]
    broker["access_token"] = token
    return token, broker


def tv_market_order(user_id: int, symbol: str, side: str, qty: int, retries: int = 3) -> Dict[str, Any]:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            token, broker = tradovate_login(user_id)

            payload = {
                "accountSpec": str(broker["account_spec"]),
                "accountId": int(broker["account_id"]),
                "action": "Buy" if side.lower() == "buy" else "Sell",
                "symbol": symbol.upper(),
                "orderQty": int(qty),
                "orderType": "Market",
                "isAutomated": True,
                "timeInForce": "Day",
                "deviceId": broker["device_id"],
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
    side = normalize_side(side)

    if side == "flatten":
        result = safe_flatten_symbol(user_id, symbol)
        return result, "FLATTENED"

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
# LOGGING + METRICS
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


def get_user_trades(user_id: int, limit: int = 200):
    con = db()
    rows = con.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    con.close()
    return rows


def estimate_trade_pnl(row) -> float:
    try:
        data = json.loads(row["broker_response"] or "{}")
        for key in ["pnl", "realizedPnl", "realizedPNL", "profit", "netPnL"]:
            if key in data:
                return float(data[key])
    except Exception:
        pass
    return 0.0


def dashboard_metrics(user_id: int):
    rows = list(reversed(get_user_trades(user_id, 500)))
    executed = [r for r in rows if r["status"] in ("EXECUTED", "SIMULATED", "FLATTEN_SENT", "SKIPPED")]
    rejected = [r for r in rows if r["status"] == "REJECTED"]

    pnl_values = []
    equity = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0

    for r in executed:
        pnl = estimate_trade_pnl(r)
        pnl_values.append(pnl)
        running += pnl
        equity.append(round(running, 2))
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    wins = len([p for p in pnl_values if p > 0])
    losses = len([p for p in pnl_values if p < 0])
    closed_with_pnl = wins + losses
    win_rate = round((wins / closed_with_pnl) * 100, 1) if closed_with_pnl else 0.0

    latencies = [float(r["latency_ms"] or 0) for r in rows if r["latency_ms"]]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0

    return {
        "total_trades": len(executed),
        "rejected": len(rejected),
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "total_pnl": round(running, 2),
        "max_drawdown": round(abs(max_dd), 2),
        "avg_latency": avg_latency,
        "equity": equity[-40:] if equity else [0, 0, 0, 0, 0],
    }


def daily_journal(user_id: int):
    rows = get_user_trades(user_id, 500)
    days: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        day = (r["ts"] or "")[:10]
        if not day:
            continue
        days.setdefault(day, {"trades": 0, "executed": 0, "rejected": 0, "pnl": 0.0})
        days[day]["trades"] += 1
        if r["status"] == "REJECTED":
            days[day]["rejected"] += 1
        if r["status"] in ("EXECUTED", "SIMULATED", "FLATTEN_SENT", "SKIPPED"):
            days[day]["executed"] += 1
        days[day]["pnl"] += estimate_trade_pnl(r)

    return sorted(days.items(), reverse=True)[:10]


def today_order_count(user_id: int) -> int:
    con = db()
    row = con.execute(
        """
        SELECT COUNT(*) AS n
        FROM trades
        WHERE user_id=?
        AND ts LIKE ?
        AND status IN ('SIMULATED','EXECUTED','SENT_TO_BROKER','FLATTEN_SENT','SKIPPED')
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
        raise Exception("Broker not connected. Go to Broker Connection and click Connect Broker first.")


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


def normalize_side(side: str) -> str:
    s = str(side or "").lower().strip()

    if s in ["buy", "long", "entry_long", "strategy.long"]:
        return "buy"

    if s in ["sell", "short", "entry_short", "strategy.short"]:
        return "sell"

    if s in ["flat", "flatten", "close", "exit", "close_all", "strategy.close", "strategy.exit"]:
        return "flatten"

    raise Exception(f"Unsupported side/action: {side}")


def clean_qty(qty: Any) -> int:
    try:
        if qty is None or qty == "":
            return 1
        return max(1, int(float(qty)))
    except Exception:
        return 1


def risk_check(user, auth: str, symbol: str, side: str, qty: int, request_id: str):
    if auth != user["webhook_secret"]:
        raise Exception("Invalid webhook secret.")

    if user["automation_status"] != "Running":
        raise Exception("Automation paused.")

    symbol = str(symbol or "").upper().strip()
    if not symbol:
        raise Exception("Missing symbol.")

    side = normalize_side(side)
    qty = clean_qty(qty)

    allowed = [item.strip().upper() for item in user["allowed_symbols"].split(",") if item.strip()]
    allow_all = "*" in allowed or "ALL" in allowed

    if not allow_all and symbol not in allowed:
        raise Exception(f"Symbol {symbol} not allowed.")

    if qty < 1 or qty > int(user["max_contracts"]):
        raise Exception(f"Qty violates max contract limit: {user['max_contracts']}.")

    if today_order_count(user["id"]) >= int(user["max_orders"]):
        raise Exception("Daily order limit reached.")

    emergency_risk_check(user["id"])
    check_duplicate(user, symbol, side, request_id)

    return symbol, side, qty


# ============================================================
# UI HELPERS
# ============================================================

def nav_item(active, key, href, icon, label):
    cls = "active" if active == key else ""
    return f'<a class="{cls}" href="{href}"><span>{icon}</span>{label}</a>'


def chart_svg(values):
    if not values:
        values = [0, 0]
    if len(values) == 1:
        values = [0, values[0]]

    width = 720
    height = 240
    padding = 18
    mn = min(values)
    mx = max(values)
    span = mx - mn if mx != mn else 1

    points = []
    for i, v in enumerate(values):
        x = padding + (i / (len(values) - 1)) * (width - padding * 2)
        y = height - padding - ((v - mn) / span) * (height - padding * 2)
        points.append(f"{x:.1f},{y:.1f}")

    polyline = " ".join(points)
    last = values[-1]

    return f"""
    <svg viewBox="0 0 {width} {height}" class="equity-svg" preserveAspectRatio="none">
      <defs>
        <linearGradient id="equityFill" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="#16a34a" stop-opacity="0.22"/>
          <stop offset="100%" stop-color="#16a34a" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path d="M {points[0]} L {polyline} L {width-padding},{height-padding} L {padding},{height-padding} Z" fill="url(#equityFill)"/>
      <polyline points="{polyline}" fill="none" stroke="#0f8f45" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="{points[-1].split(',')[0]}" cy="{points[-1].split(',')[1]}" r="6" fill="#0f8f45"/>
      <text x="{width-160}" y="35" fill="#111827" font-size="22" font-weight="800">${last:.2f}</text>
    </svg>
    """


def layout(content, user=None, active="dashboard"):
    email = user["email"] if user else "Guest"
    initials = email[:1].upper() if email else "K"
    status = user["automation_status"] if user else "Paused"
    mode = user["live_mode"].upper() if user else "SIMULATION"

    return f"""
<!DOCTYPE html>
<html>
<head>
<title>KhomaAPI</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
:root {{
  --green:#0f8f45;
  --green-dark:#086b34;
  --green-soft:#eaf7ef;
  --green-line:#cdebd8;
  --text:#111827;
  --muted:#6b7280;
  --line:#e5e7eb;
  --bg:#f8faf9;
  --card:#ffffff;
  --danger:#dc2626;
  --warning:#ca8a04;
  --shadow:0 18px 60px rgba(17,24,39,.06);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif; }}
.shell {{ display:flex; min-height:100vh; }}
.sidebar {{ width:288px; background:#fff; border-right:1px solid var(--line); padding:24px 16px; position:fixed; top:0; bottom:0; left:0; }}
.brand {{ display:flex; align-items:center; gap:12px; padding:4px 10px 34px; }}
.logo {{ width:42px; height:42px; border-radius:14px; background:linear-gradient(135deg,#16a34a,#064e2a); color:white; display:flex; align-items:center; justify-content:center; font-weight:950; box-shadow:0 12px 30px rgba(15,143,69,.25); }}
.brand h1 {{ font-size:26px; margin:0; letter-spacing:-1px; }}
.brand small {{ display:block; color:var(--muted); font-size:12px; margin-top:2px; }}
.nav a {{ display:flex; gap:12px; align-items:center; padding:13px 15px; color:#374151; text-decoration:none; border-radius:12px; margin-bottom:7px; font-size:14px; font-weight:700; }}
.nav a.active,.nav a:hover {{ background:var(--green-soft); color:var(--green-dark); }}
.sidebar-card {{ position:absolute; left:16px; right:16px; bottom:22px; background:linear-gradient(135deg,#f1fbf5,#fff); border:1px solid var(--green-line); border-radius:18px; padding:18px; }}
.sidebar-card b {{ color:var(--green-dark); }} .sidebar-card p {{ color:var(--muted); font-size:13px; line-height:1.5; margin:8px 0 0; }}
.main {{ margin-left:288px; flex:1; }}
.topbar {{ height:70px; border-bottom:1px solid var(--line); background:rgba(255,255,255,.85); backdrop-filter: blur(10px); display:flex; justify-content:space-between; align-items:center; padding:0 34px; position:sticky; top:0; z-index:10; }}
.top-left b {{ font-size:14px; }} .top-left span {{ color:var(--muted); font-size:13px; margin-left:8px; }}
.top-actions {{ display:flex; align-items:center; gap:12px; }}
.pill {{ display:inline-flex; align-items:center; gap:7px; padding:8px 11px; border-radius:999px; font-size:12px; font-weight:850; background:var(--green-soft); color:var(--green-dark); border:1px solid var(--green-line); }}
.pill.gray {{ background:#f3f4f6; color:#374151; border-color:#e5e7eb; }}
.avatar {{ width:42px; height:42px; border-radius:999px; background:#111827; color:white; display:flex; align-items:center; justify-content:center; font-weight:900; }}
.content {{ padding:34px 42px 60px; }}
.header {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-start; margin-bottom:24px; }}
.header h2 {{ margin:0; font-size:34px; letter-spacing:-1.3px; }} .header p {{ color:var(--muted); margin:9px 0 0; line-height:1.55; }}
.grid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:22px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:22px; padding:24px; box-shadow:var(--shadow); }}
.span3 {{ grid-column:span 3; }} .span4 {{ grid-column:span 4; }} .span5 {{ grid-column:span 5; }} .span6 {{ grid-column:span 6; }} .span7 {{ grid-column:span 7; }} .span8 {{ grid-column:span 8; }} .span12 {{ grid-column:span 12; }}
.card h3 {{ margin:0 0 8px; font-size:16px; letter-spacing:-.2px; }} .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
.metric {{ font-size:31px; font-weight:950; margin:13px 0 6px; letter-spacing:-1.2px; }}
.good {{ color:var(--green-dark)!important; }} .bad {{ color:var(--danger)!important; }} .warn {{ color:var(--warning)!important; }}
.btn,button {{ border:none; background:linear-gradient(135deg,#12a150,#087135); color:white; padding:12px 15px; border-radius:12px; text-decoration:none; font-weight:900; display:inline-flex; align-items:center; justify-content:center; gap:8px; cursor:pointer; margin:4px 6px 4px 0; box-shadow:0 12px 28px rgba(15,143,69,.18); }}
.btn.secondary,button.secondary {{ background:white; color:#374151; border:1px solid var(--line); box-shadow:none; }} .btn.danger,button.danger {{ background:var(--danger); }}
input,select,textarea {{ width:100%; padding:13px 14px; border:1px solid var(--line); border-radius:13px; margin:8px 0 14px; outline:none; background:#fff; font-size:14px; }}
input:focus,select:focus,textarea:focus {{ border-color:var(--green); box-shadow:0 0 0 4px var(--green-soft); }}
.formgrid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.keybox {{ border:1px solid var(--line); border-radius:14px; padding:16px; display:flex; justify-content:space-between; gap:12px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; overflow:auto; background:#fbfcfd; font-size:13px; }}
.codebox {{ background:#0b1220; color:#d1fae5; border-radius:18px; padding:20px; overflow:auto; font-size:13px; line-height:1.6; border:1px solid #1f2937; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:13px 8px; font-size:14px; vertical-align:top; }} th {{ font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:.08em; }}
.equity-wrap {{ height:270px; width:100%; }} .equity-svg {{ width:100%; height:100%; }}
.journal-day {{ display:flex; justify-content:space-between; align-items:center; padding:14px 0; border-bottom:1px solid var(--line); }}
.journal-day:last-child {{ border-bottom:none; }} .journal-day b {{ display:block; }} .journal-day small {{ color:var(--muted); }}
.copy-note {{ background:var(--green-soft); color:var(--green-dark); border:1px solid var(--green-line); padding:12px 14px; border-radius:14px; font-size:14px; font-weight:750; }}
.google-box {{ border:1px dashed var(--line); border-radius:18px; padding:20px; background:#fbfcfd; }}
@media(max-width:1100px) {{ .sidebar{{position:relative;width:100%;}} .main{{margin-left:0;}} .shell{{display:block;}} .span3,.span4,.span5,.span6,.span7,.span8{{grid-column:span 12;}} .formgrid{{grid-template-columns:1fr;}} .header{{display:block;}} }}
</style>
<script>
function copyText(id) {{
  const text = document.getElementById(id).innerText;
  navigator.clipboard.writeText(text);
  alert('Copied');
}}
function toggleProfileMenu() {{
    const menu = document.getElementById("profileMenu");
    menu.style.display = menu.style.display === "block" ? "none" : "block";
}}

document.addEventListener("click", function(event) {{
    const menu = document.getElementById("profileMenu");
    if (!event.target.closest(".avatar")) {{
        if(menu){{ menu.style.display = "none"; }}
    }}
}});
</script>

</head>
<body>
<div class="shell">
<aside class="sidebar">
  <div class="brand"><div class="logo">
  <img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:14px;">
</div><div><h1>KhomaAPI</h1><small>Execution Infrastructure</small></div></div>
  <div class="nav">
    {nav_item(active,'dashboard','/dashboard','⌁','Dashboard')}
    {nav_item(active,'broker','/broker','◇','Broker Connect')}
    {nav_item(active,'webhooks','/webhooks','⌘','Webhooks')}
    {nav_item(active,'logs','/logs','▥','Trade Logs')}
    {nav_item(active,'journal','/journal','◷','Journal')}
    {nav_item(active,'risk','/risk','☰','Risk Engine')}
    {nav_item(active,'settings','/settings','⚙','Settings')}
  </div>
  <div class="sidebar-card"><b>Cloud Execution Active</b><p>No VPS. No ngrok. No screen clicking. KhomaAPI routes TradingView signals directly through broker infrastructure.</p></div>
</aside>
<main class="main">
  <div class="topbar">
    <div class="top-left"><b>{email}</b><span>KhomaAlgorithms client workspace</span></div>
    <div class="top-actions"><span class="pill">● {status}</span><span class="pill gray">{mode}</span>
<div style="position:relative;">
  <div class="avatar" onclick="toggleProfileMenu()" style="cursor:pointer;">{initials}</div>

  <div id="profileMenu" style="
      display:none;
      position:absolute;
      right:0;
      top:55px;
      background:white;
      border:1px solid #e5e7eb;
      border-radius:14px;
      width:220px;
      box-shadow:0 20px 60px rgba(0,0,0,.08);
      overflow:hidden;
      z-index:9999;
  ">
      <a href="/settings" style="display:block;padding:14px 16px;text-decoration:none;color:#111827;font-weight:700;">Settings</a>
      <a href="/logout" style="display:block;padding:14px 16px;text-decoration:none;color:#dc2626;font-weight:700;border-top:1px solid #e5e7eb;">Logout</a>
  </div>
</div>
</div>
  </div>
  <div class="content">{content}</div>
</main>
</div>

<script>

const protocol = window.location.protocol === "https:" ? "wss" : "ws";

const socket = new WebSocket(
    protocol + "://" + window.location.host + "/ws"
);

socket.onopen = () => {{
    console.log("Live WebSocket connected");
}};

socket.onmessage = (event) => {{

    const data = JSON.parse(event.data);

    console.log("LIVE EVENT:", data);

    if(data.event === "trade") {{

        if(window.location.pathname === "/dashboard" ||
           window.location.pathname === "/logs" ||
           window.location.pathname === "/journal") {{

            location.reload();
        }}
    }}
}};

socket.onclose = () => {{
    console.log("WebSocket disconnected");
}};

</script>

</body>

</html>
"""


def login_layout(content):
    return f"""
<!DOCTYPE html><html><head><title>KhomaAPI Login</title><style>
body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;background:#f8faf9;color:#111827;}}
.wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 10% 10%,#dff5e7,transparent 30%),radial-gradient(circle at 90% 20%,#eefaf2,transparent 28%),#f8faf9;}}
.card{{width:470px;background:white;border:1px solid #e5e7eb;border-radius:24px;padding:34px;box-shadow:0 24px 90px rgba(17,24,39,.09);}}
.logo{{width:48px;height:48px;border-radius:15px;background:linear-gradient(135deg,#16a34a,#064e2a);color:white;display:flex;align-items:center;justify-content:center;font-weight:950;margin-bottom:20px;}}
h1{{letter-spacing:-1px;margin:0 0 8px;}} p{{color:#6b7280;line-height:1.55;}} input{{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:13px;margin:8px 0 14px;box-sizing:border-box;}}
button,.btn{{background:#0f8f45;color:white;border:none;padding:13px 16px;border-radius:13px;font-weight:900;text-decoration:none;display:inline-block;}} a{{color:#0f8f45;font-weight:850;}}
.google{{background:white;color:#111827;border:1px solid #e5e7eb;width:100%;margin-bottom:12px;}}
</style></head><body><div class="wrap"><div class="card">{content}</div></div>
<script>

const protocol = window.location.protocol === "https:" ? "wss" : "ws";

const socket = new WebSocket(
    protocol + "://" + window.location.host + "/ws"
);

socket.onopen = () => {{
    console.log("Live WebSocket connected");
}};

socket.onmessage = (event) => {{

    const data = JSON.parse(event.data);

    console.log("LIVE EVENT:", data);

    if(data.event === "trade") {{

        if(window.location.pathname === "/dashboard" ||
           window.location.pathname === "/logs" ||
           window.location.pathname === "/journal") {{

            location.reload();
        }}
    }}
}};

socket.onclose = () => {{
    console.log("WebSocket disconnected");
}};

</script>

</body>
</html>
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
    <div class="logo">
<img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
</div>
    <h1>Create your KhomaAPI account</h1>
    <p>Access cloud execution, broker connectivity, TradingView webhooks, and institutional risk controls.</p>
    <a class="btn google" href="/auth/google">Continue with Google</a>
    <form method="post" action="/signup">
      <input name="email" placeholder="Email" required>
      <input name="password" type="password" placeholder="Password" required>
      <button>Create Account</button>
    </form>
    <p>Already have an account? <a href="/login">Login</a></p>
    ''')

@app.post("/signup", response_class=HTMLResponse)
def signup(email: str = Form(...), password: str = Form(...)):

    if not valid_email(email):
        return login_layout("<h1>Invalid Email</h1><p>Please enter valid email.</p>")

    ok, message = strong_password(password)

    if not ok:
        return login_layout(f"<h1>Weak Password</h1><p>{message}</p>")
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
    <div class="logo">
<img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
</div>
    <h1>Welcome back</h1>
    <p>Login to your KhomaAPI execution workspace.</p>
    <a class="btn google" href="/auth/google">Continue with Google</a>
    <form method="post" action="/login">
      <input name="email" placeholder="Email" required>
      <input name="password" type="password" placeholder="Password" required>
      <button>Login</button>
    </form>
    <p><a href="/forgot-password">Forgot Password?</a></p>
    <p>New client? <a href="/signup">Create account</a></p>
    ''')


@app.post("/login", response_class=HTMLResponse)
def login(email: str = Form(...), password: str = Form(...)):
    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()
    con.close()

    if not user or not verify_password(password, user["password_hash"]):
        return login_layout("<h1>Invalid login</h1><p>Email or password is wrong.</p><a href='/login'>Try again</a>")

    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = user["id"]
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        "khoma_session",
        sid,
        httponly=True,
        secure=True,
        samesite="lax"
    )
    return response


@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("khoma_session")
    SESSIONS.pop(sid, None)
    response = RedirectResponse("/login")
    response.delete_cookie("khoma_session")
    return response


# ============================================================
# GOOGLE OAUTH LOGIN
# ============================================================

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://web-production-6ad48.up.railway.app/auth/google/callback"
)


@app.get("/auth/google")
def auth_google():
    if not GOOGLE_CLIENT_ID:
        return JSONResponse(status_code=500, content={"ok": False, "error": "GOOGLE_CLIENT_ID not configured in Railway variables."})

    google_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        "&scope=openid%20email%20profile"
        "&access_type=online"
        "&prompt=select_account"
    )

    return RedirectResponse(google_url)


@app.get("/auth/google/callback")
def auth_google_callback(code: str = ""):
    if not code:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Missing Google OAuth code."})

    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )

    token_data = token_response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Failed to get Google access token.", "details": token_data})

    google_user = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    ).json()

    email = google_user.get("email")

    if not email:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Google account email not received."})

    con = db()
    existing_user = con.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()

    if not existing_user:
        random_password = secrets.token_hex(24)
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at,allowed_symbols)
            VALUES(?,?,?,?,?,?)
            """,
            (
                email.lower().strip(),
                hash_password(random_password),
                "khoma_live_" + secrets.token_urlsafe(24),
                secrets.token_hex(20),
                datetime.now(timezone.utc).isoformat(),
                "*",
            ),
        )
        uid = cur.lastrowid
        cur.execute("INSERT INTO brokers(user_id) VALUES(?)", (uid,))
        con.commit()
        user = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    else:
        user = existing_user

    con.close()

    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = user["id"]
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        "khoma_session",
        sid,
        httponly=True,
        secure=True,
        samesite="lax"
    )
    return response


# ============================================================
# DASHBOARD ROUTES
# ============================================================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    broker = con.execute("SELECT * FROM brokers WHERE user_id=?", (user["id"],)).fetchone()
    trades = con.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
    con.close()

    m = dashboard_metrics(user["id"])
    journal = daily_journal(user["id"])

    trade_rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['status']}</td><td>{t['mode']}</td><td>{t['latency_ms']}ms</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='7'>No trades yet.</td></tr>"

    journal_rows = "".join([
        f"<div class='journal-day'><div><b>{day}</b><small>{vals['executed']} executed • {vals['rejected']} rejected</small></div><div><b>${vals['pnl']:.2f}</b><small>{vals['trades']} total logs</small></div></div>"
        for day, vals in journal
    ]) or "<p class='muted'>No journal data yet.</p>"

    broker_status = "Connected" if broker and broker["connected"] else "Disconnected"
    broker_class = "good" if broker and broker["connected"] else "bad"

    content = f'''
    <div class="header">
      <div><h2>Execution Dashboard</h2><p>Equity, live monitoring, journal, and risk visibility for your automated trading infrastructure.</p></div>
      <div><a class="btn" href="/start">Start Automation</a><a class="btn secondary" href="/pause">Pause</a></div>
    </div>

    <div class="grid">
      <div class="card span3"><h3>Total PnL</h3><div class="metric good">${m['total_pnl']}</div><p class="muted">Calculated from available trade data. Fill-based PnL can be added next.</p></div>
      <div class="card span3"><h3>Win Rate</h3><div class="metric">{m['win_rate']}%</div><p class="muted">{m['wins']} wins • {m['losses']} losses</p></div>
      <div class="card span3"><h3>Max Drawdown</h3><div class="metric warn">${m['max_drawdown']}</div><p class="muted">Based on stored PnL series.</p></div>
      <div class="card span3"><h3>Avg Latency</h3><div class="metric">{m['avg_latency']}ms</div><p class="muted">Cloud routing + broker response.</p></div>

      <div class="card span8"><h3>Equity Curve</h3><p class="muted">Builds automatically as trades are logged.</p><div class="equity-wrap">{chart_svg(m['equity'])}</div></div>
      <div class="card span4"><h3>Automation Health</h3><div class="metric {broker_class}">{broker_status}</div><p class="muted">Mode: <b>{user['live_mode'].upper()}</b><br>Status: <b>{user['automation_status']}</b><br>Orders today: <b>{today_order_count(user['id'])}</b></p><a class="btn secondary" href="/broker">Manage Broker</a></div>

      <div class="card span8"><h3>Live Trade Monitor</h3><p class="muted">Latest execution events from KhomaAPI.</p><table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Mode</th><th>Latency</th></tr>{trade_rows}</table></div>
      <div class="card span4"><h3>Trading Journal</h3><p class="muted">Trades grouped by day.</p>{journal_rows}<a class="btn secondary" href="/journal">Open Journal</a></div>
    </div>
    '''
    return layout(content, user, "dashboard")


@app.get("/broker", response_class=HTMLResponse)
def broker_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    broker = con.execute("SELECT * FROM brokers WHERE user_id=?", (user["id"],)).fetchone()
    con.close()

    content = f'''
    <div class="header"><div><h2>Broker Connection</h2><p>Client only enters Tradovate username and password. KhomaAPI handles all technical API fields automatically.</p></div></div>
    <div class="grid">
      <div class="card span5"><h3>Connection Status</h3><div class="metric {'good' if broker['connected'] else 'bad'}">{'Connected' if broker['connected'] else 'Disconnected'}</div><p class="muted">Last test: {broker['last_test'] or 'Not tested'}<br>Last error: {broker['last_error'] or 'None'}<br>Detected account: <b>{broker['account_spec'] or 'None yet'}</b></p><a class="btn secondary" href="/broker/test">Retest Connection</a></div>
      <div class="card span7"><h3>Connect Tradovate</h3><p class="muted">Enter Tradovate login. KhomaAPI auto-detects account ID and account spec from Tradovate.</p>
      <form method="post" action="/broker/connect"><div class="formgrid">
        <div><label>Environment</label><select name="env"><option value="demo" {'selected' if broker['env']=='demo' else ''}>Demo</option><option value="live" {'selected' if broker['env']=='live' else ''}>Live</option></select></div>
        <div><label>Tradovate Username</label><input name="username" placeholder="Tradovate username" required></div>
        <div><label>Tradovate Password</label><input name="password" type="password" placeholder="Tradovate password" required></div>
      </div><button>Connect Broker</button><a class="btn secondary" href="/auth/tradovate/connect">Test OAuth Connect</a></form>
      <p class="muted">Live API accounts must have API access enabled. Prop/evaluation accounts may require vendor approval.</p></div>
    </div>
    '''
    return layout(content, user, "broker")


@app.post("/broker/connect")
def broker_connect(request: Request, env: str = Form(...), username: str = Form(...), password: str = Form(...)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    try:
        token, cfg = tradovate_login_raw(env, username, password, user["id"])
        accounts = fetch_tradovate_accounts(env, token)
        account = accounts[0]

        account_id = str(account.get("id"))
        account_spec = str(account.get("name") or account.get("accountSpec") or account_id)

        con.execute(
            """
            UPDATE brokers
            SET env=?, username_enc=?, password_enc=?, app_id=?, app_version=?, cid=?, sec_enc=?,
                account_spec=?, account_id=?, device_id=?, access_token_enc=?, connected=1, last_error='', last_test=?
            WHERE user_id=?
            """,
            (
                env,
                enc(username),
                enc(password),
                cfg["app_id"],
                cfg["app_version"],
                cfg["cid"],
                enc(cfg["sec"]),
                account_spec,
                account_id,
                cfg["device_id"],
                enc(token),
                datetime.now(timezone.utc).isoformat(),
                user["id"],
            ),
        )
        con.commit()
    except Exception as e:
        con.execute(
            "UPDATE brokers SET connected=0,last_error=?,last_test=? WHERE user_id=?",
            (str(e), datetime.now(timezone.utc).isoformat(), user["id"]),
        )
        con.commit()
    finally:
        con.close()

    return RedirectResponse("/broker", status_code=302)


@app.get("/broker/test")
def broker_test(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    try:
        token, broker = tradovate_login(user["id"])
        con.execute(
            "UPDATE brokers SET connected=1,last_error='',last_test=?,access_token_enc=? WHERE user_id=?",
            (datetime.now(timezone.utc).isoformat(), enc(token), user["id"]),
        )
    except Exception as e:
        con.execute(
            "UPDATE brokers SET connected=0,last_error=?,last_test=? WHERE user_id=?",
            (str(e), datetime.now(timezone.utc).isoformat(), user["id"]),
        )
    con.commit()
    con.close()
    return RedirectResponse("/broker")


@app.get("/webhooks", response_class=HTMLResponse)
def webhooks_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    domain = str(request.base_url).rstrip("/")
    webhook_url = f"{domain}/webhook/trade"
    example = json.dumps({
        "client_id": user["email"],
        "auth": user["webhook_secret"],
        "symbol": "{{ticker}}",
        "side": "{{strategy.order.action}}",
        "qty": "{{strategy.order.contracts}}",
        "request_id": "{{strategy.order.id}}"
    }, indent=2)

    content = f'''
    <div class="header"><div><h2>TradingView Webhooks</h2><p>Copy this URL and dynamic JSON into your TradingView alert.</p></div></div>
    <div class="grid">
      <div class="card span12"><h3>Webhook URL</h3><div class="keybox"><span id="webhook-url">{webhook_url}</span><button onclick="copyText('webhook-url')">Copy</button></div></div>
      <div class="card span7"><h3>Dynamic TradingView JSON</h3><pre class="codebox" id="json-template">{example}</pre><button onclick="copyText('json-template')">Copy JSON</button></div>
      <div class="card span5"><h3>Setup Instructions</h3><p class="muted">1. Open TradingView alert.<br>2. Enable Webhook URL.<br>3. Paste webhook URL.<br>4. Paste dynamic JSON.<br>5. Your strategy controls buy/sell/qty/symbol automatically.</p><div class="copy-note">Each client uses the same endpoint, but unique client_id + secret. Accounts do not intersect.</div></div>
      <div class="card span12"><h3>Manual Alert Format</h3><p class="muted">For manual alerts, set side to buy, sell, or flatten. Symbol can be any exact Tradovate contract symbol like MNQM6, MESM6, MYMM6, etc.</p></div>
    </div>
    '''
    return layout(content, user, "webhooks")


@app.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    content = f'''
    <div class="header"><div><h2>Risk Engine</h2><p>Control max size, allowed symbols, daily order limits, duplicate locks, and execution mode.</p></div></div>
    <div class="card"><form method="post" action="/risk/save"><div class="formgrid">
      <div><label>Webhook Secret</label><input name="webhook_secret" value="{user['webhook_secret']}"></div>
      <div><label>Allowed Symbols</label><input name="allowed_symbols" value="{user['allowed_symbols']}"><p class="muted">Use * to allow any TradingView symbol.</p></div>
      <div><label>Max Contracts Per Order</label><input name="max_contracts" value="{user['max_contracts']}"></div>
      <div><label>Max Orders Per Day</label><input name="max_orders" value="{user['max_orders']}"></div>
      <div><label>Duplicate Lock Seconds</label><input name="duplicate_seconds" value="{user['duplicate_seconds']}"></div>
      <div><label>Max Rejections Per Day</label><input name="max_rejections_per_day" value="{user['max_rejections_per_day']}"></div>
      <div><label>Execution Mode</label><select name="live_mode"><option value="simulation" {'selected' if user['live_mode']=='simulation' else ''}>Simulation Only</option><option value="live" {'selected' if user['live_mode']=='live' else ''}>Live Broker Orders</option></select></div>
    </div><button>Save Risk Settings</button></form></div>
    '''
    return layout(content, user, "risk")


@app.post("/risk/save")
def risk_save(request: Request, webhook_secret: str = Form(...), allowed_symbols: str = Form(...), max_contracts: int = Form(...), max_orders: int = Form(...), duplicate_seconds: int = Form(...), max_rejections_per_day: int = Form(...), live_mode: str = Form(...)):
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


@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    trades = get_user_trades(user["id"], 200)
    rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['request_id']}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['mode']}</td><td>{t['status']}</td><td>{t['latency_ms']}ms</td><td>{t['message']}</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='9'>No logs.</td></tr>"

    content = f'''
    <div class="header"><div><h2>Trade Logs</h2><p>Full history of every accepted, rejected, simulated, and live broker request.</p></div></div>
    <div class="card"><table><tr><th>Time</th><th>Request ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Mode</th><th>Status</th><th>Latency</th><th>Message</th></tr>{rows}</table></div>
    '''
    return layout(content, user, "logs")


@app.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    journal = daily_journal(user["id"])
    journal_html = "".join([
        f"<div class='journal-day'><div><b>{day}</b><small>{vals['executed']} executed • {vals['rejected']} rejected</small></div><div><b>${vals['pnl']:.2f}</b><small>{vals['trades']} total logs</small></div></div>"
        for day, vals in journal
    ]) or "<p class='muted'>No journal data yet.</p>"

    content = f'''
    <div class="header"><div><h2>Trading Journal</h2><p>Daily breakdown of automated trading activity.</p></div></div>
    <div class="card">{journal_html}</div>
    '''
    return layout(content, user, "journal")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    content = f'''
    <div class="header"><div><h2>Settings</h2><p>Profile, authentication, and account security.</p></div></div>
    <div class="grid">
      
<div class="card span6">
<h3>Profile</h3>

<p class="muted">Current Email: <b>{user['email']}</b></p>
<p class="muted">Account created: {user['created_at']}</p>

<hr style="margin:20px 0;border:none;border-top:1px solid #e5e7eb;">

<form method="post" action="/change-email">
<label>New Email</label>
<input name="new_email" type="email" required>
<button>Change Email</button>
</form>

<hr style="margin:20px 0;border:none;border-top:1px solid #e5e7eb;">

<form method="post" action="/change-password">
<label>Current Password</label>
<input name="current_password" type="password" required>

<label>New Password</label>
<input name="new_password" type="password" required>

<button>Change Password</button>
</form>

</div>

      
<div class="card span6">
  <h3>Account Management</h3>

  <div class="google-box">

    <div style="margin-bottom:22px;">
      <p class="muted" style="margin-bottom:8px;"><b>Current Plan</b></p>
      <div class="pill">KhomaAPI Professional</div>
    </div>

    <div style="margin-bottom:22px;">
      <p class="muted" style="margin-bottom:8px;"><b>Subscription Status</b></p>
      <div class="pill good">Active</div>
    </div>

    <div style="margin-bottom:22px;">
      <p class="muted" style="margin-bottom:8px;"><b>Automation Status</b></p>
      <div class="pill">{user['automation_status']}</div>
    </div>

    <div style="display:flex;flex-direction:column;gap:10px;">

      <a class="btn secondary" href="/billing">
        Change Payment Method
      </a>

      <a class="btn secondary" href="/subscription/cancel">
        Cancel Subscription
      </a>

      <a class="btn secondary" href="/logout">
        Logout From Account
      </a>

    </div>

  </div>
</div>

    </div>
    '''
    return layout(content, user, "settings")


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
    <div class="header"><div><h2>Emergency Flatten</h2><p>Close the currently detected position for one contract symbol.</p></div></div>
    <div class="card"><form method="post" action="/flatten"><input name="symbol" placeholder="Example: MNQM6" required><button class="danger">Flatten Symbol</button></form></div>
    ''', user, "dashboard")


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


# ============================================================
# TRADOVATE OAUTH PLACEHOLDER ROUTES
# ============================================================

@app.get("/auth/tradovate/connect")
def tradovate_connect():
    client_id = os.getenv("TRADOVATE_OAUTH_CID", "13286")
    redirect_uri = os.getenv("TRADOVATE_OAUTH_REDIRECT", "https://web-production-6ad48.up.railway.app/auth/callback")
    oauth_url = (
        "https://trader.tradovate.com/oauth/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope=openid"
    )
    return RedirectResponse(oauth_url)


@app.get("/auth/callback")
def tradovate_callback(code: str = "", state: str = ""):
    return {
        "ok": True,
        "message": "OAuth callback received. Token exchange is not implemented yet.",
        "code": code,
        "state": state,
    }


# ============================================================
# WEBHOOK API
# ============================================================

class WebhookTrade(BaseModel):
    client_id: str
    auth: str
    symbol: str
    side: str
    qty: Optional[Any] = 1
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
        symbol, side, qty = risk_check(user, payload.auth, payload.symbol, payload.side, payload.qty, request_id)

        if user["live_mode"] == "live":
            broker_connection_check(user["id"])
            response, action = handle_trade_logic(user["id"], symbol, side, qty)
            mode = "live"
            if action == "SKIPPED_SAME_DIRECTION":
                status = "SKIPPED"
            elif action == "FLATTENED":
                status = "FLATTEN_SENT"
            else:
                status = "EXECUTED"
            message = action
        else:
            response = {"simulated": True, "symbol": symbol, "side": side, "qty": qty}
            action = "SIMULATED"
            mode = "simulation"
            status = "SIMULATED"
            message = "Simulation accepted. No real broker order sent."

        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, symbol, side, qty, mode, status, latency, message, response)

        return {
            "ok": True,
            "action": action,
            "status": status,
            "mode": mode,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "latency_ms": latency,
            "response": response,
        }

    except Exception as e:
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        safe_symbol = str(payload.symbol or "").upper()
        safe_side = str(payload.side or "")
        safe_qty = clean_qty(payload.qty)
        log_trade(user["id"], request_id, safe_symbol, safe_side, safe_qty, "rejected", "REJECTED", latency, str(e), {})
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
        return {"ok": True, "latency_ms": latency, "result": result}
    except Exception as e:
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, payload.symbol.upper(), "flatten", 0, "rejected", "REJECTED", latency, str(e), {})
        return {"ok": False, "error": str(e), "latency_ms": latency}


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": "KhomaAPI v5 Full SaaS Dashboard",
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/trades")
def api_trades(request: Request):
    user = require_user(request)
    if not user:
        return {"ok": False, "error": "not authenticated"}
    rows = get_user_trades(user["id"], 100)
    return [dict(row) for row in rows]


@app.post("/change-email")
def change_email(
    request: Request,
    new_email: str = Form(...)
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login")

    if not valid_email(new_email):
        return HTMLResponse("""
        <h1>Invalid Email</h1>
        <p>Please enter valid email.</p>
        """, status_code=400)

    send_email(
        new_email,
        "KhomaAPI Email Changed",
        "Your email was changed successfully."
    )

    con = db()

    con.execute(
        "UPDATE users SET email=? WHERE id=?",
        (new_email.lower().strip(), user["id"])
    )

    con.commit()
    con.close()

    return login_layout("""
    <div class="logo">
    <img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
    </div>

    <h1>Email Updated</h1>

    <p>Your email was changed successfully.</p>

    <a class="btn" href="/settings">Return To Settings</a>
    """)



@app.post("/change-password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...)
):
    user = require_user(request)

    if not user:
        return RedirectResponse("/login")

    if not verify_password(current_password, user["password_hash"]):
        return HTMLResponse("""
        <h1>Wrong Password</h1>
        <p>Current password incorrect.</p>
        """, status_code=400)

    ok, message = strong_password(new_password)

    if not ok:
        return HTMLResponse(f"""
        <h1>Weak Password</h1>
        <p>{message}</p>
        """, status_code=400)

    con = db()

    con.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (hash_password(new_password), user["id"])
    )

    con.commit()
    con.close()

    send_email(
        user["email"],
        "KhomaAPI Password Changed",
        "Your password was successfully changed."
    )

    return HTMLResponse("""
    <h1>Password Successfully Changed</h1>
    <p>Your password has been updated successfully.</p>
    <a href='/settings'>Return to Settings</a>
    """)





@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page():
    return login_layout("""
    <h1>Forgot Password</h1>

    <form method="post" action="/forgot-password">
        <input name="email" type="email" placeholder="Your Email" required>
        <button>Send Reset Link</button>
    </form>
    """)


@app.post("/forgot-password")
def forgot_password(email: str = Form(...)):

    con = db()

    user = con.execute(
        "SELECT * FROM users WHERE email=?",
        (email.lower().strip(),)
    ).fetchone()

    if not user:
        con.close()

        return HTMLResponse("""
        <h1>Email Not Found</h1>
        """)

    token = secrets.token_urlsafe(48)

    con.execute(
        """
        INSERT INTO password_resets(token,user_id,created_at)
        VALUES(?,?,?)
        """,
        (
            token,
            user["id"],
            datetime.now(timezone.utc).isoformat()
        )
    )

    con.commit()
    con.close()

    reset_link = f"{APP_URL}/reset-password/{token}"

    send_email(
        email,
        "Reset Your KhomaAPI Password",
        f"Click here to reset password:\n\n{reset_link}"
    )

    return login_layout("""
    <div class="logo">
    <img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
    </div>

    <h1>Reset Link Sent</h1>

    <p>We sent a secure password reset link to your email.</p>

    <p style="color:#6b7280;">
    Check spam/promotions folder if you don't see it.
    </p>

    <a class="btn" href="/login">Return To Login</a>
    """)


@app.get("/reset-password/{token}", response_class=HTMLResponse)
def reset_password_page(token: str):

    return login_layout(f"""
    <h1>Create New Password</h1>

    <form method="post" action="/reset-password/{token}">
        <input name="password" type="password" placeholder="New Password" required>
        <button>Reset Password</button>
    </form>
    """)


@app.post("/reset-password/{token}")
def reset_password(token: str, password: str = Form(...)):

    ok, message = strong_password(password)

    if not ok:
        return HTMLResponse(f"""
        <h1>Weak Password</h1>
        <p>{message}</p>
        """)

    con = db()

    row = con.execute(
        "SELECT * FROM password_resets WHERE token=?",
        (token,)
    ).fetchone()

    if not row:
        con.close()

        return HTMLResponse("""
        <h1>Invalid Token</h1>
        """)

    created = datetime.fromisoformat(row["created_at"])
    now = datetime.now(timezone.utc)

    if (now - created).total_seconds() > 3600:
        con.close()

        return HTMLResponse("""
        <h1>Reset Link Expired</h1>
        <p>Your reset link expired. Please request another one.</p>
        """)

    con.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (
            hash_password(password),
            row["user_id"]
        )
    )

    con.execute(
        "DELETE FROM password_resets WHERE token=?",
        (token,)
    )

    con.commit()
    con.close()

    return login_layout("""
    <div class="logo">
    <img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
    </div>

    <h1>Password Updated</h1>

    <p>Your password was successfully reset.</p>

    <a class="btn" href="/login">Login</a>
    """)


@app.get("/verify-email-change/{token}")
def verify_email_change(token: str):

    con = db()

    row = con.execute(
        "SELECT * FROM email_verifications WHERE token=?",
        (token,)
    ).fetchone()

    if not row:
        con.close()

        return HTMLResponse("""
        <h1>Invalid Verification Link</h1>
        """)

    created = datetime.fromisoformat(row["created_at"])
    now = datetime.now(timezone.utc)

    if (now - created).total_seconds() > 3600:
        con.close()

        return HTMLResponse("""
        <h1>Verification Link Expired</h1>
        """)

    con.execute(
        "UPDATE users SET email=? WHERE id=?",
        (
            row["new_email"],
            row["user_id"]
        )
    )

    con.execute(
        "DELETE FROM email_verifications WHERE token=?",
        (token,)
    )

    con.commit()
    con.close()

    return HTMLResponse("""
    <h1>Email Successfully Updated</h1>
    <p>Your new email is now active.</p>
    <a href='/settings'>Return to Settings</a>
    """)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):

    await manager.connect(websocket)

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(websocket)
