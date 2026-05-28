from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from cryptography.fernet import Fernet
import os
import httpx
import sqlite3
import hashlib
import os
import secrets
import json
import time

import requests

from app.tradovate_oauth import build_tradovate_login, exchange_code_for_token, fetch_accounts
from app import tradovate_oauth as tvo
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

# Allow overriding the DB location (e.g. a Railway persistent volume at /data)
# so data survives redeploys. Defaults to the bundled file for local dev.
DB_PATH = Path(os.getenv("KHOMA_DB_PATH", str(BASE_DIR / "khomaapi_v31.db")))
KEY_PATH = BASE_DIR / ".khoma_secret_v31"

if not KEY_PATH.exists():
    KEY_PATH.write_text(Fernet.generate_key().decode(), encoding="utf-8")
FERNET = Fernet(KEY_PATH.read_text(encoding="utf-8").strip().encode())

# Maps a short-lived OAuth `state` value -> user_id, so the Tradovate
# callback can be tied back to the user who started the connect flow.
OAUTH_STATES: Dict[str, int] = {}

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
    # Syntax-only: deliverability (DNS/MX) checks add a network round-trip per
    # signup and reject valid-but-MX-less domains, so they're disabled here.
    try:
        validate_email(email, check_deliverability=False)
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

    # Tradovate OAuth connections. One row per connected brokerage account
    # (a single OAuth login can expose several accounts: demo, live, prop).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS broker_accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        broker TEXT DEFAULT 'tradovate',
        env TEXT DEFAULT 'live',
        account_id TEXT,
        account_name TEXT,
        account_type TEXT,
        access_token_enc TEXT,
        token_expires_at TEXT,
        status TEXT DEFAULT 'connected',
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(user_id, account_id)
    )
    """)

    # Persistent login sessions so a Railway restart / redeploy does not log
    # everyone out (previously sessions lived in an in-memory dict).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions(
        sid TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at TEXT
    )
    """)

    # Per-trade journal notes (text + optional screenshot).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trade_notes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        trade_id INTEGER,
        note TEXT,
        image_path TEXT,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(user_id, trade_id)
    )
    """)

    # --- Additive column migrations (safe on existing DBs) ---
    def ensure_column(table, column, decl):
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except Exception:
            pass

    ensure_column("users", "is_verified", "INTEGER DEFAULT 1")
    ensure_column("email_verifications", "kind", "TEXT DEFAULT 'change_email'")
    ensure_column("email_verifications", "payload", "TEXT")
    ensure_column("broker_accounts", "in_copy_box", "INTEGER DEFAULT 0")
    ensure_column("trades", "fill_price", "REAL")
    ensure_column("trades", "pnl", "REAL")

    con.commit()
    con.close()


init_db()


def email_enabled() -> bool:
    """True only when outbound SMTP is actually configured."""
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))


UPLOADS_DIR = os.getenv("KHOMA_UPLOADS_DIR", str(BASE_DIR / "uploads"))
os.makedirs(UPLOADS_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


def create_email_token(user_id: int, kind: str, payload: str = "") -> str:
    """Create a single-use, time-limited token for an email-driven action."""
    token = secrets.token_urlsafe(48)
    con = db()
    con.execute(
        "INSERT INTO email_verifications(token,user_id,new_email,created_at,kind,payload) VALUES(?,?,?,?,?,?)",
        (token, user_id, payload, datetime.now(timezone.utc).isoformat(), kind, payload),
    )
    con.commit()
    con.close()
    return token


def consume_email_token(token: str, kind: Optional[str] = None, max_age: int = 3600):
    """Validate a token (kind + expiry), delete it, and return its row dict, or None."""
    con = db()
    row = con.execute("SELECT * FROM email_verifications WHERE token=?", (token,)).fetchone()
    if not row:
        con.close()
        return None
    row = dict(row)
    try:
        created = datetime.fromisoformat(row["created_at"])
    except Exception:
        created = datetime.now(timezone.utc) - timedelta(seconds=max_age + 1)
    expired = (datetime.now(timezone.utc) - created).total_seconds() > max_age
    wrong_kind = kind is not None and row.get("kind") != kind
    con.execute("DELETE FROM email_verifications WHERE token=?", (token,))
    con.commit()
    con.close()
    if expired or wrong_kind:
        return None
    return row


@app.get("/test")
def test():
    return {"working": True}


# ============================================================
# SESSION HELPERS
# ============================================================

def create_session(user_id: int) -> str:
    sid = secrets.token_urlsafe(32)
    con = db()
    con.execute(
        "INSERT INTO sessions(sid, user_id, created_at) VALUES(?,?,?)",
        (sid, user_id, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return sid


def get_session_user_id(sid: Optional[str]) -> Optional[int]:
    if not sid:
        return None
    con = db()
    row = con.execute("SELECT user_id FROM sessions WHERE sid=?", (sid,)).fetchone()
    con.close()
    return row["user_id"] if row else None


def destroy_session(sid: Optional[str]) -> None:
    if not sid:
        return
    con = db()
    con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
    con.commit()
    con.close()


def current_user(request: Request):
    sid = request.cookies.get("khoma_session")
    uid = get_session_user_id(sid)
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


def save_broker_account(user_id: int, env: str, account: Dict[str, Any], token: str, expires_at: str) -> None:
    """Insert or update one Tradovate account for a user (idempotent on account_id)."""
    account_id = str(account.get("id") or account.get("accountId") or "")
    account_name = str(account.get("name") or account.get("nickname") or account_id)
    account_type = str(account.get("accountType") or account.get("legalStatus") or "")
    now = datetime.now(timezone.utc).isoformat()

    con = db()
    con.execute(
        """
        INSERT INTO broker_accounts
            (user_id, broker, env, account_id, account_name, account_type,
             access_token_enc, token_expires_at, status, created_at, updated_at)
        VALUES (?, 'tradovate', ?, ?, ?, ?, ?, ?, 'connected', ?, ?)
        ON CONFLICT(user_id, account_id) DO UPDATE SET
            env=excluded.env,
            account_name=excluded.account_name,
            account_type=excluded.account_type,
            access_token_enc=excluded.access_token_enc,
            token_expires_at=excluded.token_expires_at,
            status='connected',
            updated_at=excluded.updated_at
        """,
        (user_id, env, account_id, account_name, account_type,
         enc(token), expires_at, now, now),
    )
    con.commit()
    con.close()


def get_broker_accounts(user_id: int, connected_only: bool = False):
    con = db()
    if connected_only:
        rows = con.execute(
            "SELECT * FROM broker_accounts WHERE user_id=? AND status='connected' ORDER BY id",
            (user_id,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM broker_accounts WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def disconnect_broker_account(user_id: int, row_id: int) -> bool:
    con = db()
    cur = con.execute(
        "DELETE FROM broker_accounts WHERE id=? AND user_id=?",
        (row_id, user_id),
    )
    con.commit()
    deleted = cur.rowcount > 0
    con.close()
    return deleted


def get_trade_note(user_id: int, trade_id: int):
    con = db()
    row = con.execute(
        "SELECT * FROM trade_notes WHERE user_id=? AND trade_id=?",
        (user_id, trade_id),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_trade_notes_map(user_id: int) -> Dict[int, dict]:
    con = db()
    rows = con.execute("SELECT * FROM trade_notes WHERE user_id=?", (user_id,)).fetchall()
    con.close()
    return {r["trade_id"]: dict(r) for r in rows}


def upsert_trade_note(user_id: int, trade_id: int, note: str, image_path: Optional[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    # When no new image is uploaded, keep the existing one.
    if image_path is None:
        existing = con.execute(
            "SELECT image_path FROM trade_notes WHERE user_id=? AND trade_id=?",
            (user_id, trade_id),
        ).fetchone()
        image_path = existing["image_path"] if existing else None
    con.execute(
        """
        INSERT INTO trade_notes(user_id, trade_id, note, image_path, created_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id, trade_id) DO UPDATE SET
            note=excluded.note,
            image_path=excluded.image_path,
            updated_at=excluded.updated_at
        """,
        (user_id, trade_id, note, image_path, now, now),
    )
    con.commit()
    con.close()


# ============================================================
# COPY TRADING (mirror orders across accounts in the box)
# ============================================================

def get_copy_accounts(user_id: int):
    """Connected accounts the user dragged into the Copy Trading box."""
    return [a for a in get_broker_accounts(user_id, connected_only=True) if a.get("in_copy_box")]


def set_copy_box(user_id: int, account_db_id: int, in_box: bool) -> None:
    con = db()
    con.execute(
        "UPDATE broker_accounts SET in_copy_box=? WHERE id=? AND user_id=?",
        (1 if in_box else 0, account_db_id, user_id),
    )
    con.commit()
    con.close()


def _order_ok(resp) -> bool:
    return (
        isinstance(resp, dict)
        and resp.get("orderId") is not None
        and not resp.get("failureReason")
        and not resp.get("failureText")
        and not resp.get("error")
    )


def place_order_on_account(account: dict, action: str, symbol: str, qty: int) -> dict:
    token = dec(account["access_token_enc"]) if account.get("access_token_enc") else ""
    env = account.get("env") or "live"
    if not token or not account.get("account_id"):
        return {"account": account.get("account_name"), "ok": False, "error": "Reconnect required"}
    resp = tvo.place_order(env, token, account.get("account_name"), account.get("account_id"), action, symbol, qty)
    return {"account": account.get("account_name"), "ok": _order_ok(resp), "response": resp}


def flatten_on_account(account: dict, symbol: str) -> list:
    """Close any open position matching `symbol` on one account."""
    token = dec(account["access_token_enc"]) if account.get("access_token_enc") else ""
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return [{"account": account.get("account_name"), "ok": False, "error": "Reconnect required"}]

    root = symbol_root(str(symbol).upper())
    results = []
    for p in (tvo.get_positions(env, token) or []):
        if str(p.get("accountId")) != str(acct_id):
            continue
        net = int(p.get("netPos") or 0)
        if net == 0:
            continue
        cname = ((tvo.get_contract(env, token, p.get("contractId")) or {}).get("name") or "").upper()
        if root and symbol_root(cname) != root:
            continue
        action = "Sell" if net > 0 else "Buy"
        resp = tvo.place_order(env, token, account.get("account_name"), acct_id, action, cname or symbol, abs(net))
        results.append({"account": account.get("account_name"), "ok": _order_ok(resp), "response": resp})
    return results


def copy_trade_execute(user_id: int, symbol: str, side: str, qty: int) -> dict:
    """Mirror a signal one-to-one to every account in the Copy Trading box."""
    boxed = get_copy_accounts(user_id)
    results = []
    for a in boxed:
        if side == "flatten":
            results += flatten_on_account(a, symbol)
        else:
            results.append(place_order_on_account(a, side, symbol, qty))
    placed = sum(1 for r in results if r.get("ok"))
    return {"results": results, "placed": placed, "total": len(results), "accounts": len(boxed)}


# ============================================================
# LIVE TRADE MONITOR (reads positions/fills/PnL from Tradovate)
# ============================================================

# Dollar value per 1.0 point move, per contract root. Used to turn fill price
# differences into realized PnL. Covers the CME index futures this app targets;
# unknown symbols fall back to 1.0.
CONTRACT_MULTIPLIERS = {
    "ES": 50.0, "MES": 5.0,
    "NQ": 20.0, "MNQ": 2.0,
    "YM": 5.0, "MYM": 0.5,
    "RTY": 50.0, "M2K": 5.0,
    "CL": 1000.0, "MCL": 100.0,
    "GC": 100.0, "MGC": 10.0,
    "SI": 5000.0,
    "6E": 125000.0, "6B": 62500.0, "6J": 12500000.0,
}

_MONTH_CODES = "FGHJKMNQUVXZ"


def symbol_root(name: str) -> str:
    """MNQM6 -> MNQ, ESZ4 -> ES. Returns the input if it doesn't match a futures code."""
    name = (name or "").upper().strip()
    m = re.match(r"^([A-Z0-9]+?)([" + _MONTH_CODES + r"])(\d{1,2})$", name)
    return m.group(1) if m else name


def contract_multiplier(name: str) -> float:
    return CONTRACT_MULTIPLIERS.get(symbol_root(name), 1.0)


def realized_pnl_from_fills(fills, name_for) -> float:
    """FIFO realized PnL across a set of fills. `name_for(contractId)` -> symbol name."""
    books: Dict[Any, list] = {}
    realized = 0.0

    for f in sorted(fills, key=lambda x: str(x.get("timestamp", ""))):
        cid = f.get("contractId")
        qty = abs(float(f.get("qty") or 0))
        price = float(f.get("price") or 0)
        action = str(f.get("action") or "").lower()
        if qty == 0:
            continue

        signed = qty if action == "buy" else -qty
        mult = contract_multiplier(name_for(cid))
        book = books.setdefault(cid, [])

        # Close against opposing open lots first (FIFO).
        while signed != 0 and book and (book[0][0] > 0) != (signed > 0):
            lot_qty, lot_price = book[0]
            match = min(abs(signed), abs(lot_qty))
            if lot_qty > 0:            # long lot closed by a sell
                realized += (price - lot_price) * match * mult
            else:                      # short lot closed by a buy
                realized += (lot_price - price) * match * mult
            if abs(lot_qty) == match:
                book.pop(0)
            else:
                book[0][0] = lot_qty - (match if lot_qty > 0 else -match)
            signed -= (match if signed > 0 else -match)

        if signed != 0:
            book.append([signed, price])

    return round(realized, 2)


def _snapshot_value(snap, keys):
    if isinstance(snap, dict):
        for k in keys:
            if snap.get(k) is not None:
                try:
                    return float(snap[k])
                except Exception:
                    pass
    return None


def live_account_monitor(user_id: int) -> Dict[str, Any]:
    """Aggregate live open positions + PnL across the user's connected accounts."""
    accounts = get_broker_accounts(user_id, connected_only=True)
    out_accounts = []
    token_cache: Dict[tuple, tuple] = {}
    contract_names: Dict[Any, str] = {}
    today = date.today().isoformat()

    for a in accounts:
        token = dec(a["access_token_enc"]) if a.get("access_token_enc") else ""
        env = a.get("env") or "live"
        acct_id = a.get("account_id")

        if not token or not acct_id:
            out_accounts.append({
                "name": a["account_name"], "env": env,
                "open_pnl": None, "realized_pnl": None, "total_cash": None,
                "positions": [], "error": "Reconnect required",
            })
            continue

        key = (token, env)
        if key not in token_cache:
            token_cache[key] = (tvo.get_positions(env, token), tvo.get_fills(env, token))
        positions_all, fills_all = token_cache[key]

        def name_for(cid):
            if cid in contract_names:
                return contract_names[cid]
            c = tvo.get_contract(env, token, cid) or {}
            nm = c.get("name") if isinstance(c, dict) else None
            contract_names[cid] = nm or f"#{cid}"
            return contract_names[cid]

        try:
            acct_id_int = int(acct_id)
        except Exception:
            acct_id_int = None

        def belongs(obj):
            aid = obj.get("accountId")
            return acct_id_int is None or aid == acct_id_int or str(aid) == str(acct_id)

        positions = []
        for p in (positions_all or []):
            if not belongs(p):
                continue
            net = float(p.get("netPos") or 0)
            if net == 0:
                continue
            positions.append({
                "symbol": name_for(p.get("contractId")),
                "side": "long" if net > 0 else "short",
                "qty": abs(int(net)),
                "avg_price": p.get("netPrice"),
            })

        acct_fills = [
            f for f in (fills_all or [])
            if belongs(f) and str(f.get("timestamp", ""))[:10] == today
        ]
        realized = realized_pnl_from_fills(acct_fills, name_for) if acct_fills else 0.0

        snap = tvo.get_cash_snapshot(env, token, acct_id)
        open_pnl = _snapshot_value(snap, ("openPnL", "openPnl", "unrealizedPnL", "totalPnL"))
        total_cash = _snapshot_value(snap, ("totalCashValue", "totalCashBalance", "cashBalance", "amount"))

        out_accounts.append({
            "name": a["account_name"], "env": env,
            "open_pnl": open_pnl, "realized_pnl": realized, "total_cash": total_cash,
            "positions": positions,
        })

    def total(field):
        vals = [acc[field] for acc in out_accounts if acc.get(field) is not None]
        return round(sum(vals), 2) if vals else None

    totals = {"open_pnl": total("open_pnl"), "realized_pnl": total("realized_pnl")}
    if totals["open_pnl"] is not None or totals["realized_pnl"] is not None:
        totals["total_pnl"] = round((totals["open_pnl"] or 0) + (totals["realized_pnl"] or 0), 2)
    else:
        totals["total_pnl"] = None

    return {
        "ok": True,
        "connected": len(accounts) > 0,
        "accounts": out_accounts,
        "totals": totals,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def build_round_trips(fills, name_for):
    """Reconstruct closed round-trips (entry/exit/PnL) and remaining open lots from raw fills."""
    books: Dict[Any, list] = {}
    trips = []

    for f in sorted(fills, key=lambda x: str(x.get("timestamp", ""))):
        cid = f.get("contractId")
        qty = abs(float(f.get("qty") or 0))
        price = float(f.get("price") or 0)
        action = str(f.get("action") or "").lower()
        ts = f.get("timestamp")
        if qty == 0:
            continue

        signed = qty if action == "buy" else -qty
        sym = name_for(cid)
        mult = contract_multiplier(sym)
        book = books.setdefault(cid, [])

        while signed != 0 and book and (book[0][0] > 0) != (signed > 0):
            lot_qty, lot_price, lot_ts = book[0]
            match = min(abs(signed), abs(lot_qty))
            if lot_qty > 0:
                pnl = (price - lot_price) * match * mult
                entry_side = "long"
            else:
                pnl = (lot_price - price) * match * mult
                entry_side = "short"
            trips.append({
                "symbol": sym, "side": entry_side, "qty": int(match),
                "entry_price": round(lot_price, 4), "exit_price": round(price, 4),
                "pnl": round(pnl, 2), "opened_at": lot_ts, "closed_at": ts,
            })
            if abs(lot_qty) == match:
                book.pop(0)
            else:
                book[0][0] = lot_qty - (match if lot_qty > 0 else -match)
            signed -= (match if signed > 0 else -match)

        if signed != 0:
            book.append([signed, price, ts])

    open_positions = []
    for cid, book in books.items():
        for lot_qty, lot_price, lot_ts in book:
            open_positions.append({
                "symbol": name_for(cid),
                "side": "long" if lot_qty > 0 else "short",
                "qty": abs(int(lot_qty)),
                "entry_price": round(lot_price, 4),
                "opened_at": lot_ts,
            })

    return trips, open_positions


def account_trade_history(user_id: int):
    """Closed round-trips + open positions across the user's connected accounts."""
    accounts = get_broker_accounts(user_id, connected_only=True)
    token_cache: Dict[tuple, list] = {}
    contract_names: Dict[Any, str] = {}
    all_trips, all_open = [], []

    for a in accounts:
        token = dec(a["access_token_enc"]) if a.get("access_token_enc") else ""
        env = a.get("env") or "live"
        acct_id = a.get("account_id")
        if not token or not acct_id:
            continue

        key = (token, env)
        if key not in token_cache:
            token_cache[key] = tvo.get_fills(env, token)
        fills_all = token_cache[key]

        try:
            acct_id_int = int(acct_id)
        except Exception:
            acct_id_int = None

        def name_for(cid):
            if cid in contract_names:
                return contract_names[cid]
            c = tvo.get_contract(env, token, cid) or {}
            nm = c.get("name") if isinstance(c, dict) else None
            contract_names[cid] = nm or f"#{cid}"
            return contract_names[cid]

        acct_fills = [
            f for f in (fills_all or [])
            if acct_id_int is None or f.get("accountId") == acct_id_int or str(f.get("accountId")) == str(acct_id)
        ]
        trips, openp = build_round_trips(acct_fills, name_for)
        for t in trips:
            t["account"] = a["account_name"]
        for o in openp:
            o["account"] = a["account_name"]
        all_trips += trips
        all_open += openp

    all_trips.sort(key=lambda x: str(x.get("closed_at", "")), reverse=True)
    return all_trips, all_open


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

    if not data:
        response = requests.get(
            f"{tradovate_base(env)}/account/deps",
            headers=tv_headers(token),
            timeout=15,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text}

    if (response.status_code >= 400 or not isinstance(data, list) or len(data) == 0):

        fallback = requests.get(
            f"{tradovate_base(env)}/account/deps",
            headers=tv_headers(token),
            timeout=15,
        )

        try:
            fallback_data = fallback.json()
        except Exception:
            fallback_data = []

        if isinstance(fallback_data, list) and len(fallback_data) > 0:
            data = fallback_data
        else:
            raise Exception(f"Could not fetch Tradovate accounts: {data}")

    active_accounts = [
        a for a in data
        if not a.get("closed")
        and not a.get("archived")
        and a.get("active", True)
    ]

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

    # Every symbol is allowed — KhomaAPI trades exactly what the TradingView
    # alert sends, at the quantity the alert specifies.
    if qty < 1:
        raise Exception("Invalid quantity from alert.")

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
      <input name="email" type="email" placeholder="Email" required>
      <input name="password" type="password" placeholder="Password" minlength="8" required>
      <p style="color:#6b7280;font-size:13px;margin:-6px 0 14px;">At least 8 characters, with an uppercase letter, a number, and a special character.</p>
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

    # If email is configured, accounts start unverified and must confirm.
    # Without SMTP, auto-verify so the app stays usable.
    verified = 0 if email_enabled() else 1

    con = db()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at,is_verified)
            VALUES(?,?,?,?,?,?)
            """,
            (
                email.lower().strip(),
                hash_password(password),
                "khoma_live_" + secrets.token_urlsafe(24),
                secrets.token_hex(20),
                datetime.now(timezone.utc).isoformat(),
                verified,
            ),
        )
        uid = cur.lastrowid
        cur.execute("INSERT INTO brokers(user_id) VALUES(?)", (uid,))
        con.commit()
    except Exception:
        con.close()
        return login_layout("<h1>Account already exists.</h1><p>That email is already registered.</p><a href='/login'>Go to login</a>")

    con.close()

    if email_enabled():
        token = create_email_token(uid, "signup")
        send_email(
            email.lower().strip(),
            "Verify your KhomaAPI account",
            f"Welcome to KhomaAPI. Confirm your email to activate your account:\n\n{APP_URL}/verify-email/{token}\n\nThis link expires in 1 hour.",
        )
        return login_layout("<h1>Check your email</h1><p>We sent a verification link to confirm your account. Click it, then log in.</p><a class='btn' href='/login'>Go to login</a>")

    return RedirectResponse("/login", status_code=302)


@app.get("/verify-email/{token}", response_class=HTMLResponse)
def verify_email(token: str):
    row = consume_email_token(token, kind="signup")
    if not row:
        return login_layout("<h1>Link invalid or expired</h1><p>Please sign up again or request a new link.</p><a href='/signup'>Sign up</a>")
    con = db()
    con.execute("UPDATE users SET is_verified=1 WHERE id=?", (row["user_id"],))
    con.commit()
    con.close()
    return login_layout("<h1>Email verified</h1><p>Your account is active. You can log in now.</p><a class='btn' href='/login'>Log in</a>")


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

    if email_enabled() and not user["is_verified"]:
        return login_layout("<h1>Verify your email</h1><p>Please click the verification link we emailed you before logging in.</p><a href='/login'>Back to login</a>")

    sid = create_session(user["id"])
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
    destroy_session(request.cookies.get("khoma_session"))
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

    sid = create_session(user["id"])
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

DASHBOARD_LIVE_SCRIPT = """
<script>
(function () {
  function fmt(v) {
    if (v === null || v === undefined) return "—";
    var n = Number(v);
    return (n < 0 ? "-$" : "$") + Math.abs(n).toFixed(2);
  }
  function cls(v) {
    if (v === null || v === undefined) return "metric";
    return "metric " + (Number(v) > 0 ? "good" : (Number(v) < 0 ? "bad" : ""));
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }
  function render(data) {
    var tp = document.getElementById("totalPnl");
    var lp = document.getElementById("livePnl");
    var lm = document.getElementById("liveMonitor");
    if (!data || !data.connected) {
      if (tp) tp.textContent = "—";
      if (lp) lp.innerHTML = "<p class='muted'>Connect a Tradovate account to see live PnL.</p>";
      if (lm) lm.innerHTML = "<p class='muted'>No connected account yet. Go to Broker Connect to link your Tradovate accounts.</p>";
      return;
    }
    if (tp && data.totals && data.totals.total_pnl !== null && data.totals.total_pnl !== undefined) {
      tp.textContent = fmt(data.totals.total_pnl);
      tp.className = cls(data.totals.total_pnl);
    }
    if (lp) {
      var h = "";
      (data.accounts || []).forEach(function (a) {
        h += "<div class='journal-day'><div><b>" + esc(a.name) + "</b><small>" + esc((a.env || "").toUpperCase()) + "</small></div>" +
             "<div style='text-align:right'><b class='" + (Number(a.open_pnl) < 0 ? "bad" : "good") + "'>Open " + fmt(a.open_pnl) + "</b>" +
             "<small>Realized " + fmt(a.realized_pnl) + "</small></div></div>";
      });
      lp.innerHTML = h || "<p class='muted'>No accounts.</p>";
    }
    if (lm) {
      var rows = "";
      (data.accounts || []).forEach(function (a) {
        (a.positions || []).forEach(function (p) {
          rows += "<tr><td><b>" + esc(p.symbol) + "</b></td><td>" + esc(a.name) + "</td><td>" +
                  esc(p.side) + "</td><td>" + esc(p.qty) + "</td><td>" +
                  (p.avg_price == null ? "—" : esc(p.avg_price)) + "</td></tr>";
        });
      });
      lm.innerHTML = rows
        ? "<table><tr><th>Symbol</th><th>Account</th><th>Side</th><th>Qty</th><th>Avg Price</th></tr>" + rows + "</table>"
        : "<p class='muted'>No open positions right now.</p>";
    }
  }
  function poll() {
    fetch("/api/live/monitor", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }
  poll();
  setInterval(poll, 6000);
})();
</script>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    trades = con.execute("SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
    con.close()

    m = dashboard_metrics(user["id"])
    journal = daily_journal(user["id"])

    running = user["automation_status"] == "Running"
    state_label = "ON" if running else "OFF"
    state_class = "good" if running else "bad"
    mode = user["live_mode"].upper()

    trade_rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['status']}</td><td>{t['mode']}</td><td>{t['latency_ms']}ms</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='7'>No execution events yet.</td></tr>"

    journal_rows = "".join([
        f"<div class='journal-day'><div><b>{day}</b><small>{vals['executed']} executed • {vals['rejected']} rejected</small></div><div><b>${vals['pnl']:.2f}</b><small>{vals['trades']} total logs</small></div></div>"
        for day, vals in journal
    ]) or "<p class='muted'>No journal data yet.</p>"

    start_btn = '<a class="btn secondary" href="/start">Start Automation</a>' if running else '<a class="btn" href="/start">Start Automation</a>'
    pause_btn = '<a class="btn" href="/pause">Pause</a>' if running else '<a class="btn secondary" href="/pause">Pause</a>'

    content = f'''
    <div class="header">
      <div><h2>Execution Dashboard</h2><p>System is <b class="{state_class}">{state_label}</b> · Mode: <b>{mode}</b> · Orders today: <b>{today_order_count(user['id'])}</b></p></div>
      <div>{start_btn}{pause_btn}</div>
    </div>

    <div class="grid">
      <div class="card span3"><h3>Total PnL (live)</h3><div class="metric good" id="totalPnl">—</div><p class="muted">Open + realized from connected accounts.</p></div>
      <div class="card span3"><h3>Win Rate</h3><div class="metric">{m['win_rate']}%</div><p class="muted">{m['wins']} wins • {m['losses']} losses (from trade log)</p></div>
      <div class="card span3"><h3>Max Drawdown</h3><div class="metric warn">${m['max_drawdown']}</div><p class="muted">From stored trade log.</p></div>
      <div class="card span3"><h3>Avg Latency</h3><div class="metric">{m['avg_latency']}ms</div><p class="muted">Cloud routing + broker response.</p></div>

      <div class="card span8"><h3>Live Trade Monitor</h3><p class="muted">Open positions from your connected Tradovate accounts. Refreshes automatically.</p><div id="liveMonitor"><p class="muted">Loading live positions…</p></div></div>
      <div class="card span4"><h3>Live Account PnL</h3><p class="muted">Per connected account.</p><div id="livePnl"><p class="muted">Loading…</p></div></div>

      <div class="card span8"><h3>Execution Log</h3><p class="muted">Recent order events routed by KhomaAPI.</p><table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Mode</th><th>Latency</th></tr>{trade_rows}</table></div>
      <div class="card span4"><h3>Trading Journal</h3><p class="muted">Trades grouped by day.</p>{journal_rows}<a class="btn secondary" href="/journal">Open Journal</a></div>
    </div>
    '''
    return layout(content + DASHBOARD_LIVE_SCRIPT, user, "dashboard")


BROKER_COPY_SCRIPT = """
<script>
(function () {
  function persist(id, box) {
    var fd = new FormData();
    fd.append("account_id", id);
    fd.append("in_box", box ? "1" : "0");
    fetch("/broker/copy/set", { method: "POST", body: fd, credentials: "same-origin" }).catch(function () {});
  }
  function refreshEmpty() {
    document.querySelectorAll(".dropzone").forEach(function (z) {
      var chips = z.querySelector(".chips");
      var empty = z.querySelector(".empty");
      if (empty) empty.style.display = (chips && chips.children.length) ? "none" : "block";
    });
  }
  var dragged = null;
  document.querySelectorAll(".acct-chip").forEach(function (c) {
    c.addEventListener("dragstart", function () { dragged = c; });
    c.addEventListener("dragend", function () { dragged = null; });
  });
  document.querySelectorAll(".dropzone").forEach(function (z) {
    z.addEventListener("dragover", function (e) { e.preventDefault(); z.style.background = "#eaf7ef"; });
    z.addEventListener("dragleave", function () { z.style.background = ""; });
    z.addEventListener("drop", function (e) {
      e.preventDefault();
      z.style.background = "";
      if (!dragged) return;
      z.querySelector(".chips").appendChild(dragged);
      persist(dragged.dataset.id, z.dataset.box === "1");
      refreshEmpty();
    });
  });
  refreshEmpty();
})();
</script>
"""


@app.get("/broker", response_class=HTMLResponse)
def broker_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    accounts = get_broker_accounts(user["id"])

    banner = ""
    if request.query_params.get("connected"):
        banner = "<div class='card span12'><p class='good'>Tradovate accounts connected successfully.</p></div>"
    elif request.query_params.get("error"):
        err = request.query_params.get("error")
        banner = f"<div class='card span12'><p class='bad'>OAuth error: {err}</p></div>"

    def chip(a):
        return (
            f'<div class="acct-chip" draggable="true" data-id="{a["id"]}" '
            f'style="display:flex;justify-content:space-between;align-items:center;gap:10px;'
            f'border:1px solid #e5e7eb;border-radius:14px;padding:12px 14px;margin:8px 0;background:#fff;cursor:grab;">'
            f'<div><b>{a["account_name"]}</b><br><small style="color:#6b7280">{(a["env"] or "").upper()} · {(a["account_type"] or "").upper() or "ACCOUNT"}</small></div>'
            f'<form method="post" action="/broker/disconnect/{a["id"]}" style="margin:0" '
            f'onsubmit="return confirm(\'Disconnect {a["account_name"]}?\')">'
            f'<button class="btn secondary" style="padding:6px 10px;margin:0">Disconnect</button></form>'
            f'</div>'
        )

    available_chips = "".join(chip(a) for a in accounts if not a.get("in_copy_box"))
    box_chips = "".join(chip(a) for a in accounts if a.get("in_copy_box"))
    connected_count = len(accounts)
    box_count = sum(1 for a in accounts if a.get("in_copy_box"))

    if not accounts:
        zones_html = "<p class='muted'>No accounts connected yet. Click \"Connect with Tradovate\" to log in and import your accounts.</p>"
    else:
        zones_html = f'''
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;">
          <div>
            <h4 style="margin:0 0 6px">Available Accounts</h4>
            <div id="zone-available" class="dropzone" data-box="0" style="min-height:120px;border:2px dashed #e5e7eb;border-radius:16px;padding:10px;">
              <div class="chips">{available_chips}</div>
              <p class="empty muted" style="text-align:center;padding:18px 0;">Drag accounts here to remove from copy trading.</p>
            </div>
          </div>
          <div>
            <h4 style="margin:0 0 6px">Copy Trading Box <span class="pill" style="background:#fee2e2;color:#b91c1c;border-color:#fecaca;">LIVE</span></h4>
            <div id="zone-box" class="dropzone" data-box="1" style="min-height:120px;border:2px dashed #cdebd8;border-radius:16px;padding:10px;background:#f7fdf9;">
              <div class="chips">{box_chips}</div>
              <p class="empty muted" style="text-align:center;padding:18px 0;">Drag accounts here. Every TradingView signal is mirrored 1:1 to these accounts as real market orders.</p>
            </div>
          </div>
        </div>
        '''

    content = f'''
    <div class="header"><div><h2>Broker Connection</h2><p>Log in with Tradovate to connect your cash, live, and prop firm accounts, then drag the ones you want traded into the Copy Trading box.</p></div></div>
    <div class="grid">
      {banner}
      <div class="card span5"><h3>Connection Status</h3>
        <div class="metric {'good' if connected_count else 'bad'}">{connected_count} Connected</div>
        <p class="muted">{box_count} account(s) in the Copy Trading box. Connect through Tradovate's secure login — KhomaAPI never sees your password.</p>
        <a class="btn" href="/auth/tradovate/connect">Connect with Tradovate</a>
      </div>
      <div class="card span7"><h3>Accounts &amp; Copy Trading</h3>
        <p class="muted">Accounts in the <b>LIVE</b> box receive real orders mirrored from your TradingView alerts. Empty box = simulation only.</p>
        {zones_html}
      </div>
    </div>
    '''
    return layout(content + BROKER_COPY_SCRIPT, user, "broker")


@app.post("/broker/copy/set")
def broker_copy_set(request: Request, account_id: int = Form(...), in_box: str = Form(...)):
    user = require_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Not logged in"})
    set_copy_box(user["id"], account_id, in_box in ("1", "true", "True", "on"))
    return {"ok": True}


@app.post("/broker/disconnect/{account_id}")
def broker_disconnect(request: Request, account_id: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    disconnect_broker_account(user["id"], account_id)
    return RedirectResponse("/broker", status_code=302)


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
    <div class="header"><div><h2>Risk Engine</h2><p>Daily order limits and duplicate protection. KhomaAPI trades exactly the symbol and quantity each TradingView alert sends — no symbol list or size cap to maintain.</p></div></div>
    <div class="card span6"><h3>Webhook Secret</h3><p class="muted">This is your account's signing key. It is included automatically in the Webhooks JSON you copy — you don't set it and can't change it.</p><div class="keybox"><span>{user['webhook_secret']}</span></div></div>
    <div class="card"><form method="post" action="/risk/save"><div class="formgrid">
      <div><label>Max Orders Per Day</label><input name="max_orders" value="{user['max_orders']}"><p class="muted">Hard daily cap across all symbols.</p></div>
      <div><label>Duplicate Lock Seconds</label><input name="duplicate_seconds" value="{user['duplicate_seconds']}"><p class="muted">Blocks identical repeat alerts within this window.</p></div>
      <div><label>Max Rejections Per Day</label><input name="max_rejections_per_day" value="{user['max_rejections_per_day']}"><p class="muted">Auto-locks automation after this many broker rejections.</p></div>
    </div><button>Save Risk Settings</button></form></div>
    '''
    return layout(content, user, "risk")


@app.post("/risk/save")
def risk_save(request: Request, max_orders: int = Form(...), duplicate_seconds: int = Form(...), max_rejections_per_day: int = Form(...)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    # webhook_secret, symbols, contract size and execution mode are intentionally
    # NOT user-editable: the secret is fixed, every symbol is allowed, and size
    # comes from the alert.
    con = db()
    con.execute(
        """
        UPDATE users
        SET max_orders=?, duplicate_seconds=?, max_rejections_per_day=?
        WHERE id=?
        """,
        (max(1, int(max_orders)), max(0, int(duplicate_seconds)), max(1, int(max_rejections_per_day)), user["id"]),
    )
    con.commit()
    con.close()
    return RedirectResponse("/risk", status_code=302)


@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    # Real fill-based trade history from the connected broker (entry/exit/PnL).
    history_html = ""
    try:
        trips, open_positions = account_trade_history(user["id"])
    except Exception:
        trips, open_positions = [], []

    if trips or open_positions:
        closed_rows = "".join([
            f"<tr><td>{(t.get('closed_at') or '')[:19]}</td><td><b>{t['symbol']}</b></td><td>{t.get('account','')}</td>"
            f"<td>{t['side']}</td><td>{t['qty']}</td><td>{t['entry_price']}</td><td>{t['exit_price']}</td>"
            f"<td class=\"{'good' if t['pnl']>0 else ('bad' if t['pnl']<0 else '')}\">${t['pnl']:.2f}</td></tr>"
            for t in trips
        ]) or "<tr><td colspan='8'>No closed trades yet.</td></tr>"

        open_rows = "".join([
            f"<tr><td><b>{o['symbol']}</b></td><td>{o.get('account','')}</td><td>{o['side']}</td><td>{o['qty']}</td><td>{o['entry_price']}</td></tr>"
            for o in open_positions
        ])
        open_block = (
            f"<div class='card span12'><h3>Open Positions</h3><table><tr><th>Symbol</th><th>Account</th><th>Side</th><th>Qty</th><th>Entry</th></tr>{open_rows}</table></div>"
            if open_rows else ""
        )

        history_html = f'''
        <div class="grid">
          {open_block}
          <div class="card span12"><h3>Closed Trades (from broker fills)</h3>
            <table><tr><th>Closed</th><th>Symbol</th><th>Account</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>PnL</th></tr>{closed_rows}</table>
          </div>
        </div>
        '''

    trades = get_user_trades(user["id"], 200)
    rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['request_id']}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['mode']}</td><td>{t['status']}</td><td>{t['latency_ms']}ms</td><td>{t['message']}</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='9'>No execution events yet.</td></tr>"

    content = f'''
    <div class="header"><div><h2>Trade Logs</h2><p>Exact entry, exit and realized PnL per trade (from broker fills), plus the raw execution log.</p></div></div>
    {history_html}
    <div class="card"><h3>Execution Log</h3><p class="muted">Every accepted, rejected, simulated, and live order request KhomaAPI routed.</p><table><tr><th>Time</th><th>Request ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Mode</th><th>Status</th><th>Latency</th><th>Message</th></tr>{rows}</table></div>
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

    trades = get_user_trades(user["id"], 60)
    notes = get_trade_notes_map(user["id"])
    trade_rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td><b>{t['symbol']}</b></td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['status']}</td>"
        f"<td>{'📝' if notes.get(t['id']) and notes[t['id']].get('note') else ''}{' 🖼' if notes.get(t['id']) and notes[t['id']].get('image_path') else ''}</td>"
        f"<td><a class='btn secondary' href='/journal/note/{t['id']}'>{'Edit Note' if notes.get(t['id']) else 'Add Note'}</a></td></tr>"
        for t in trades
    ]) or "<tr><td colspan='7'>No trades to journal yet.</td></tr>"

    content = f'''
    <div class="header"><div><h2>Trading Journal</h2><p>Daily breakdown plus per-trade notes — click a trade to write what happened and attach a screenshot.</p></div></div>
    <div class="grid">
      <div class="card span4"><h3>Daily Summary</h3>{journal_html}</div>
      <div class="card span8"><h3>Trades</h3><table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Notes</th><th></th></tr>{trade_rows}</table></div>
    </div>
    '''
    return layout(content, user, "journal")


ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@app.get("/journal/note/{trade_id}", response_class=HTMLResponse)
def journal_note_page(request: Request, trade_id: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    t = con.execute("SELECT * FROM trades WHERE id=? AND user_id=?", (trade_id, user["id"])).fetchone()
    con.close()
    if not t:
        return layout("<div class='card'><p class='muted'>Trade not found.</p><a class='btn secondary' href='/journal'>Back to Journal</a></div>", user, "journal")

    note = get_trade_note(user["id"], trade_id)
    note_text = (note or {}).get("note", "") or ""
    image_path = (note or {}).get("image_path")
    image_html = f"<p class='muted'>Current screenshot:</p><img src='{image_path}' style='max-width:100%;border-radius:14px;border:1px solid #e5e7eb;margin-bottom:14px;'>" if image_path else ""

    content = f'''
    <div class="header"><div><h2>Trade Note</h2><p>{t['ts'][:19]} · <b>{t['symbol']}</b> · {t['side']} · qty {t['qty']} · {t['status']}</p></div></div>
    <div class="card span8">
      <form method="post" action="/journal/note/{trade_id}" enctype="multipart/form-data">
        <label>Notes (what happened, mistakes, lessons)</label>
        <textarea name="note" rows="8" placeholder="Write your trade notes here...">{note_text}</textarea>
        {image_html}
        <label>Attach / replace screenshot (PNG, JPG, GIF, WEBP — max 5MB)</label>
        <input type="file" name="image" accept="image/*">
        <button>Save Note</button>
        <a class="btn secondary" href="/journal">Back to Journal</a>
      </form>
    </div>
    '''
    return layout(content, user, "journal")


@app.post("/journal/note/{trade_id}")
async def journal_note_save(request: Request, trade_id: int, note: str = Form(""), image: UploadFile = File(None)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    con = db()
    owns = con.execute("SELECT 1 FROM trades WHERE id=? AND user_id=?", (trade_id, user["id"])).fetchone()
    con.close()
    if not owns:
        return RedirectResponse("/journal", status_code=302)

    image_path = None
    if image is not None and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXT or not (image.content_type or "").startswith("image/"):
            return layout("<div class='card'><p class='bad'>Only image files (PNG/JPG/GIF/WEBP) are allowed.</p><a class='btn secondary' href='/journal/note/" + str(trade_id) + "'>Back</a></div>", user, "journal")
        data = await image.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return layout("<div class='card'><p class='bad'>Image is too large (max 5MB).</p><a class='btn secondary' href='/journal/note/" + str(trade_id) + "'>Back</a></div>", user, "journal")
        safe_name = f"u{user['id']}_t{trade_id}_{secrets.token_hex(6)}{ext}"
        with open(os.path.join(UPLOADS_DIR, safe_name), "wb") as fp:
            fp.write(data)
        image_path = f"/uploads/{safe_name}"

    upsert_trade_note(user["id"], trade_id, note.strip(), image_path)
    return RedirectResponse(f"/journal/note/{trade_id}", status_code=302)


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
  <p class="muted">You're signed in as <b>{user['email']}</b>.</p>
  <div style="margin-top:18px;">
    <a class="btn danger" href="/logout">Log Out</a>
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




@app.get("/auth/tradovate/connect")
def tradovate_connect(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    # Tie this OAuth attempt to the logged-in user via an unguessable state token.
    state = secrets.token_urlsafe(24)
    OAUTH_STATES[state] = user["id"]

    return RedirectResponse(build_tradovate_login(state))


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/broker?error={error}", status_code=302)

    if not code:
        return RedirectResponse("/broker?error=missing_code", status_code=302)

    # Resolve which user started this flow: state map first, session cookie as fallback.
    user_id = OAUTH_STATES.pop(state, None)
    if user_id is None:
        user = current_user(request)
        if user:
            user_id = user["id"]
    if user_id is None:
        return RedirectResponse("/login", status_code=302)

    token_result = exchange_code_for_token(code)
    if not token_result.get("ok"):
        return JSONResponse(status_code=400, content=token_result)

    access_token = token_result["access_token"]
    expires_in = token_result.get("expires_in", 0)
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        if expires_in else ""
    )

    accounts_result = fetch_accounts(access_token)
    accounts = accounts_result.get("accounts") or []
    env = accounts_result.get("env", "live")

    if not accounts:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Connected, but no accounts returned.",
                     "detail": accounts_result.get("error")},
        )

    for account in accounts:
        save_broker_account(user_id, env, account, access_token, expires_at)

    return RedirectResponse(url="/broker?connected=1", status_code=302)


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

        # Execution is driven by the Copy Trading box: accounts dragged in get
        # the order mirrored one-to-one. No boxed accounts => simulation only.
        boxed = get_copy_accounts(user["id"])
        if boxed:
            response = copy_trade_execute(user["id"], symbol, side, qty)
            mode = "live"
            action = "COPIED"
            status = "EXECUTED" if response["placed"] > 0 or response["total"] == 0 else "REJECTED"
            message = f"Copied to {response['placed']}/{response['total']} order(s) across {response['accounts']} account(s)."
        else:
            response = {"simulated": True, "symbol": symbol, "side": side, "qty": qty}
            action = "SIMULATED"
            mode = "simulation"
            status = "SIMULATED"
            message = "No accounts in the Copy Trading box — simulated only."

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
def change_email(request: Request, new_email: str = Form(...)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    new_email = new_email.lower().strip()
    if not valid_email(new_email):
        return login_layout("<h1>Invalid Email</h1><p>Please enter a valid email.</p><a href='/settings'>Back</a>")

    con = db()
    taken = con.execute("SELECT 1 FROM users WHERE email=?", (new_email,)).fetchone()
    con.close()
    if taken:
        return login_layout("<h1>Email in use</h1><p>That email is already registered.</p><a href='/settings'>Back</a>")

    if not email_enabled():
        # No SMTP: change directly (the logged-in session is the auth).
        con = db()
        con.execute("UPDATE users SET email=? WHERE id=?", (new_email, user["id"]))
        con.commit()
        con.close()
        return login_layout("<h1>Email Updated</h1><p>Email verification isn't configured yet, so the change was applied directly.</p><a class='btn' href='/settings'>Return to Settings</a>")

    # Step 1 of 2: confirm at the CURRENT email address.
    token = create_email_token(user["id"], "email_old", new_email)
    send_email(
        user["email"],
        "Confirm your KhomaAPI email change",
        f"You requested to change your email to {new_email}.\n\nConfirm from your current address:\n{APP_URL}/confirm-email-change/{token}\n\nIf this wasn't you, ignore this email.",
    )
    return login_layout("<h1>Confirm at your current email</h1><p>We sent a confirmation link to your current address. After you confirm, we'll email the new address to verify it.</p><a class='btn' href='/settings'>Back to Settings</a>")


@app.get("/confirm-email-change/{token}", response_class=HTMLResponse)
def confirm_email_change(token: str):
    row = consume_email_token(token, kind="email_old")
    if not row:
        return login_layout("<h1>Link invalid or expired</h1><a href='/settings'>Back</a>")
    new_email = row["payload"]
    # Step 2 of 2: verify at the NEW email address.
    token2 = create_email_token(row["user_id"], "email_new", new_email)
    send_email(
        new_email,
        "Verify your new KhomaAPI email",
        f"Verify this address to finish your KhomaAPI email change:\n\n{APP_URL}/verify-email-change/{token2}\n\nThis link expires in 1 hour.",
    )
    return login_layout("<h1>Now check your new email</h1><p>We've emailed the new address. Click that link to finish the change.</p>")


@app.get("/verify-email-change/{token}", response_class=HTMLResponse)
def verify_email_change(token: str):
    row = consume_email_token(token, kind="email_new")
    if not row:
        return login_layout("<h1>Link invalid or expired</h1><a href='/settings'>Back</a>")
    new_email = row["payload"]
    con = db()
    taken = con.execute("SELECT 1 FROM users WHERE email=? AND id<>?", (new_email, row["user_id"])).fetchone()
    if taken:
        con.close()
        return login_layout("<h1>Email in use</h1><p>That address was taken before you confirmed.</p><a href='/settings'>Back</a>")
    con.execute("UPDATE users SET email=? WHERE id=?", (new_email, row["user_id"]))
    con.commit()
    con.close()
    return login_layout("<h1>Email Updated</h1><p>Your new email is now active.</p><a class='btn' href='/login'>Log in</a>")


@app.post("/change-password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    if not verify_password(current_password, user["password_hash"]):
        return login_layout("<h1>Wrong Password</h1><p>Current password incorrect.</p><a href='/settings'>Back</a>")

    ok, message = strong_password(new_password)
    if not ok:
        return login_layout(f"<h1>Weak Password</h1><p>{message}</p><a href='/settings'>Back</a>")

    if not email_enabled():
        con = db()
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user["id"]))
        con.commit()
        con.close()
        return login_layout("<h1>Password Changed</h1><p>Your password has been updated.</p><a class='btn' href='/settings'>Return to Settings</a>")

    # Email confirmation required: store the pending hash in the token payload.
    token = create_email_token(user["id"], "password", hash_password(new_password))
    send_email(
        user["email"],
        "Confirm your KhomaAPI password change",
        f"Confirm your password change:\n\n{APP_URL}/confirm-password-change/{token}\n\nIf this wasn't you, change your password immediately and ignore this link.",
    )
    return login_layout("<h1>Confirm via email</h1><p>We emailed you a link to confirm the password change.</p><a class='btn' href='/settings'>Back to Settings</a>")


@app.get("/confirm-password-change/{token}", response_class=HTMLResponse)
def confirm_password_change(token: str):
    row = consume_email_token(token, kind="password")
    if not row:
        return login_layout("<h1>Link invalid or expired</h1><a href='/settings'>Back</a>")
    con = db()
    con.execute("UPDATE users SET password_hash=? WHERE id=?", (row["payload"], row["user_id"]))
    # Changing the password invalidates other sessions.
    con.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
    con.commit()
    con.close()
    return login_layout("<h1>Password Updated</h1><p>Your password is changed. Please log in again.</p><a class='btn' href='/login'>Log in</a>")


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page():
    return login_layout("""
    <h1>Forgot Password</h1>
    <p>Enter your account email and we'll send a reset link.</p>
    <form method="post" action="/forgot-password">
        <input name="email" type="email" placeholder="Your Email" required>
        <button>Send Reset Link</button>
    </form>
    <p><a href="/login">Back to login</a></p>
    """)


@app.post("/forgot-password")
def forgot_password(email: str = Form(...)):
    email = email.lower().strip()
    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    # Only act if the account exists, but never reveal whether it does.
    if user:
        token = secrets.token_urlsafe(48)
        con.execute(
            "INSERT INTO password_resets(token,user_id,created_at) VALUES(?,?,?)",
            (token, user["id"], datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        send_email(
            email,
            "Reset Your KhomaAPI Password",
            f"Reset your password (link expires in 1 hour):\n\n{APP_URL}/reset-password/{token}",
        )
    con.close()

    return login_layout("""
    <h1>Check your email</h1>
    <p>If an account exists for that email, a secure reset link is on its way. Check spam/promotions too.</p>
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


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):

    await manager.connect(websocket)

    try:
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get("/connected-accounts")
def connected_accounts(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    return RedirectResponse("/broker")


@app.get("/api/tradovate/accounts")
def api_tradovate_accounts(request: Request):
    user = require_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Not logged in"})

    accounts = get_broker_accounts(user["id"])
    return {
        "ok": True,
        "count": len(accounts),
        "accounts": [
            {
                "id": a["id"],
                "account_id": a["account_id"],
                "name": a["account_name"],
                "type": a["account_type"],
                "env": a["env"],
                "status": a["status"],
            }
            for a in accounts
        ],
    }


@app.get("/debug/accounts")
def debug_accounts(request: Request):
    user = require_user(request)
    if not user:
        return {"ok": False, "error": "Not logged in"}
    return {"ok": True, "accounts": get_broker_accounts(user["id"])}


@app.get("/api/live/monitor")
def api_live_monitor(request: Request):
    user = require_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Not logged in"})
    try:
        return live_account_monitor(user["id"])
    except Exception as e:
        return {"ok": False, "connected": False, "accounts": [], "totals": {}, "error": str(e)}


@app.get("/debug/tradovate/raw")
def debug_tradovate_raw(request: Request):
    """Raw Tradovate responses for the first connected account, to verify field mappings."""
    user = require_user(request)
    if not user:
        return {"ok": False, "error": "Not logged in"}
    accts = get_broker_accounts(user["id"], connected_only=True)
    if not accts:
        return {"ok": False, "error": "No connected account"}
    a = accts[0]
    token = dec(a["access_token_enc"]) if a.get("access_token_enc") else ""
    env = a.get("env") or "live"
    return {
        "ok": True,
        "account": a["account_name"],
        "env": env,
        "positions": tvo.get_positions(env, token),
        "fills": tvo.get_fills(env, token),
        "cash_snapshot": tvo.get_cash_snapshot(env, token, a.get("account_id")),
    }


@app.get("/create-broker-table")
def create_broker_table():
    # Safe + idempotent: ensures the table exists without ever dropping data.
    init_db()
    return {"ok": True, "table": "broker_accounts ensured"}


@app.get("/oauth-test")
def oauth_test(request: Request):
    user = require_user(request)
    if not user:
        return {"ok": False, "has_token": False, "error": "Not logged in"}

    accounts = get_broker_accounts(user["id"], connected_only=True)
    has_token = any(a.get("access_token_enc") for a in accounts)

    return {
        "ok": True,
        "has_token": has_token,
        "connected_accounts": len(accounts),
    }


@app.get("/debug/db-path")
def debug_db_path():
    try:
        con = db()
        count = con.execute("SELECT COUNT(*) AS c FROM broker_accounts").fetchone()["c"]
        con.close()
        return {
            "db_path": str(DB_PATH),
            "exists": Path(DB_PATH).exists(),
            "broker_accounts_rows": count,
        }
    except Exception as e:
        return {"db_path": str(DB_PATH), "error": str(e)}

