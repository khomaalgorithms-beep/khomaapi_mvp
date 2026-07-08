from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from typing import Optional, Dict, Any, Tuple
from pathlib import Path
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo
from cryptography.fernet import Fernet
import os
import httpx
import sqlite3
import hashlib
import os
import secrets
import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from app.tradovate_oauth import build_tradovate_login, exchange_code_for_token, fetch_accounts
from app import tradovate_oauth as tvo
from app import risk_engine as risk
from app import db as dbmod
from app import entitlements as ent
from app import whop as whopmod
from app import security as sec
import asyncio
import yaml
import re
import smtplib

from email.mime.text import MIMEText
from email_validator import validate_email, EmailNotValidError

import socket

# Railway containers have no IPv6 egress route. When a host (e.g. api.resend.com)
# resolves to an IPv6 address, requests/urllib3 tries IPv6 first and fails with
# "[Errno 101] Network is unreachable" instead of falling back to IPv4. Force
# IPv4-only DNS resolution for all outbound HTTP so email + APIs always connect.
try:
    import urllib3.util.connection as _urllib3_conn
    _urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass


def _ipv4_getaddrinfo(host, *args, **kwargs):
    """smtplib/stdlib socket helper: return only IPv4 results."""
    results = socket._orig_getaddrinfo(host, *args, **kwargs)
    return [r for r in results if r[0] == socket.AF_INET] or results


if not hasattr(socket, "_orig_getaddrinfo"):
    socket._orig_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = _ipv4_getaddrinfo




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

# CRITICAL: the encryption key MUST be stable across deploys/restarts, or every
# stored broker token becomes undecryptable and every client is forced to
# reconnect. On Railway the container filesystem is ephemeral, so a file-based key
# silently rotates on each restart. Pin it to the KHOMA_ENC_KEY env var (persistent
# config) as the source of truth; fall back to the on-disk key only for local dev.
def _load_fernet():
    env_key = (os.getenv("KHOMA_ENC_KEY") or "").strip()
    if env_key:
        try:
            return Fernet(env_key.encode()), "env"
        except Exception as e:
            print(f"KHOMA_ENC_KEY is set but invalid ({e}); falling back to key file")
    if not KEY_PATH.exists():
        KEY_PATH.write_text(Fernet.generate_key().decode(), encoding="utf-8")
    return Fernet(KEY_PATH.read_text(encoding="utf-8").strip().encode()), "file"


FERNET, _FERNET_SOURCE = _load_fernet()
print(f"FERNET key source: {_FERNET_SOURCE}")

# Maps a short-lived OAuth `state` value -> user_id, so the Tradovate
# callback can be tied back to the user who started the connect flow.
OAUTH_STATES: Dict[str, int] = {}

APP_URL = os.getenv("APP_URL", "https://khomaapi.com")
# Session cookies are Secure (HTTPS-only) by default — correct for production.
# Set COOKIE_SECURE=0 for local http testing.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") != "0"

# ---- Subscription gating (Whop) -----------------------------------------
# Master switch. Default OFF so deploying the gating code never locks anyone
# out before Whop is wired + memberships linked. Flip to 1 at go-live.
ENFORCE_SUBSCRIPTIONS = os.getenv("ENFORCE_SUBSCRIPTIONS", "0") == "1"
# Lets a `manual_plan` column simulate an active plan for testing BEFORE Whop is
# connected. Never overrides a real (linked) Whop membership, and is ignored
# entirely unless explicitly enabled. Turn OFF once Whop is the source of truth.
ALLOW_MANUAL_PLAN = os.getenv("ALLOW_MANUAL_PLAN", "0") == "1"
# Diagnostic/debug endpoints (e.g. /debug/*, /test, /create-broker-table) can
# leak internal data — disabled in production. Set DEBUG_ENDPOINTS=1 to enable.
DEBUG_ENDPOINTS = os.getenv("DEBUG_ENDPOINTS", "0") == "1"
# Where to send users who need to buy / upgrade (marketing pricing page).
PRICING_URL = os.getenv("PRICING_URL", "https://khomaapi.com/#pricing").strip()

# Our own sibling domains, trusted for cross-origin POSTs (e.g. the marketing
# site at khomaapi.com posting Sign In to app.khomaapi.com). CSRF still blocks
# every other origin. Override/extend via TRUSTED_ORIGINS (comma-separated).
from urllib.parse import urlparse as _urlparse  # noqa: E402
TRUSTED_ORIGIN_HOSTS = {h.strip().lower() for h in os.getenv("TRUSTED_ORIGINS", "").split(",") if h.strip()}
TRUSTED_ORIGIN_HOSTS |= {
    _urlparse(APP_URL).netloc.lower(),
    "app.khomaapi.com", "khomaapi.com", "www.khomaapi.com",
}

# Whop billing credentials (env only — never logged or returned).
WHOP_API_KEY = os.getenv("WHOP_API_KEY", "").strip()
WHOP_WEBHOOK_SECRET = os.getenv("WHOP_WEBHOOK_SECRET", "").strip()

# Public verified-results page: whose performance to publish, which account(s),
# and the display name. PUBLIC_TRACK_ACCOUNT_IDS (CSV of broker_accounts.id)
# scopes it to specific real accounts; empty = all of that user's accounts.
PUBLIC_TRACK_EMAIL = os.getenv("PUBLIC_TRACK_EMAIL", "khomadima89@gmail.com").strip().lower()
PUBLIC_TRACK_NAME = os.getenv("PUBLIC_TRACK_NAME", "KhomaAPI — Verified Results").strip()
PUBLIC_TRACK_ACCOUNT_IDS = [s.strip() for s in os.getenv("PUBLIC_TRACK_ACCOUNT_IDS", "").split(",") if s.strip()]

# Real resting exchange-side OCO brackets: when an entry alert carries sl/tp levels,
# place the stop + take-profit as WORKING orders at Tradovate (placeOSO) so exits fill
# at price-or-better and survive a server/VPS/internet outage. Instant kill-switch:
# set BRACKET_ORDERS=0 to revert to plain market entries without a redeploy.
BRACKET_ORDERS = os.getenv("BRACKET_ORDERS", "1") == "1"
# Canary rollout: if set (comma-separated emails), real OCO brackets run for ONLY these
# users; EVERY other client keeps plain market entries, byte-for-byte unchanged. Lets us
# demo-test on one account with zero client risk, then clear the var to enable for all.
BRACKET_ORDERS_ONLY_USERS = {e.strip().lower() for e in
                             os.getenv("BRACKET_ORDERS_ONLY_USERS", "").split(",") if e.strip()}


def _brackets_on_for(email: str) -> bool:
    """Are real OCO brackets enabled for this user? Global flag AND (canary empty OR
    the user is in the canary allow-list)."""
    if not BRACKET_ORDERS:
        return False
    if BRACKET_ORDERS_ONLY_USERS:
        return str(email or "").lower() in BRACKET_ORDERS_ONLY_USERS
    return True

# Tick size per futures root, for rounding sl/tp to a valid exchange price so a bracket
# is never rejected for an off-tick price (which would risk a naked entry).
_TICK_SIZE = {
    "ES": 0.25, "MES": 0.25, "NQ": 0.25, "MNQ": 0.25, "YM": 1.0, "MYM": 1.0,
    "RTY": 0.1, "M2K": 0.1, "CL": 0.01, "MCL": 0.01, "GC": 0.1, "MGC": 0.1,
    "SI": 0.005, "6E": 0.00005, "6B": 0.0001, "6J": 0.0000005,
}


def google_login_button() -> str:
    """Render the Google sign-in button ONLY when Google OAuth is configured,
    so launch never shows a button that 500s."""
    return ('<a class="btn google" href="/auth/google">Continue with Google</a>'
            if os.getenv("GOOGLE_CLIENT_ID") else "")

# Process start time, for the public /status uptime page.
APP_START_TIME = time.time()


def public_base_url(request: Request) -> str:
    """Public base URL for building copy/paste links (webhook URL, emails).

    Railway terminates TLS at its edge and forwards plain HTTP to the app, so
    request.base_url can come back as http://. TradingView posting to an http://
    URL gets 301-redirected to https, which downgrades POST -> GET and yields a
    405. Force https for any non-local host to prevent that."""
    if os.getenv("APP_URL"):
        return os.getenv("APP_URL").strip().rstrip("/")
    # Honor the proxy's forwarded protocol when present.
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    url = str(request.base_url).rstrip("/")
    host = request.headers.get("host", "")
    is_local = "localhost" in url or "127.0.0.1" in url
    if (proto == "https" or not is_local) and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


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
    # Dual-backend: SQLite by default, Postgres when DATABASE_URL is set
    # (see app/db.py). Call sites are unchanged — same ? placeholders + dict rows.
    return dbmod.connect(str(DB_PATH))


def enc(value: str) -> str:
    return FERNET.encrypt(value.encode()).decode() if value else ""


def dec(value: Optional[str]) -> str:
    # A token encrypted with a different/rotated key raises Fernet's InvalidToken,
    # whose message is BLANK — which previously surfaced as an empty 'REJECTED' with
    # no reason. Treat any decrypt failure as "no usable token" so the caller cleanly
    # asks the user to reconnect instead of throwing an unexplained error.
    if not value:
        return ""
    try:
        return FERNET.decrypt(value.encode()).decode()
    except Exception:
        return ""


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



# Last SMTP error, surfaced by /debug/email so the exact failure is visible.
LAST_EMAIL_ERROR = ""


def email_from() -> str:
    """The 'From' address. With Resend you can use onboarding@resend.dev to test
    immediately (delivers to your own account email), or a verified-domain
    address for production. Falls back to the SMTP user."""
    return (
        os.getenv("EMAIL_FROM")
        or os.getenv("SMTP_FROM")
        or os.getenv("SMTP_USER")
        or "KhomaAPI <onboarding@resend.dev>"
    )


def _send_via_resend(to_email, subject, body, html=None) -> bool:
    """Send through Resend's HTTP API. One API key, no SMTP, no app passwords."""
    global LAST_EMAIL_ERROR
    key = os.getenv("RESEND_API_KEY")
    if not key:
        return False
    payload = {"from": email_from(), "to": [to_email], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code < 300:
            LAST_EMAIL_ERROR = ""
            print("EMAIL SENT (resend) TO:", to_email)
            return True
        LAST_EMAIL_ERROR = f"Resend {r.status_code}: {r.text}"
        print("EMAIL ERROR:", LAST_EMAIL_ERROR)
    except Exception as e:
        LAST_EMAIL_ERROR = f"Resend error: {type(e).__name__}: {e}"
        print("EMAIL ERROR:", LAST_EMAIL_ERROR)
    return False


def _send_via_smtp(to_email, subject, body, html=None) -> bool:
    global LAST_EMAIL_ERROR
    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASS")
    if not host or not user or not password:
        return False

    if html:
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))
    else:
        msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_from()
    msg["To"] = to_email
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(user, password)
        server.sendmail(user, [to_email], msg.as_string())
        server.quit()
        LAST_EMAIL_ERROR = ""
        print("EMAIL SENT (smtp) TO:", to_email)
        return True
    except Exception as e:
        LAST_EMAIL_ERROR = f"{type(e).__name__}: {e}"
        print("EMAIL ERROR:", LAST_EMAIL_ERROR)
        return False


def send_email(to_email, subject, body, html=None):
    """Send an email via the configured provider. Tries Resend first (operator
    sets ONE api key — clients never configure anything), then SMTP."""
    global LAST_EMAIL_ERROR
    if os.getenv("RESEND_API_KEY"):
        if _send_via_resend(to_email, subject, body, html):
            return True
        # Resend failed — try SMTP if it's also configured.
    if _send_via_smtp(to_email, subject, body, html):
        return True
    if not email_enabled():
        LAST_EMAIL_ERROR = "No email provider configured — set RESEND_API_KEY (recommended) or SMTP_HOST/SMTP_USER/SMTP_PASS."
        print("EMAIL ERROR:", LAST_EMAIL_ERROR)
    return False


def email_html(heading: str, message_html: str, button_label: str = "", button_url: str = "") -> str:
    """Branded HTML email — logo banner, body, optional CTA button, footer."""
    logo = f"{APP_URL.rstrip('/')}/static/logo.png"
    button = ""
    if button_label and button_url:
        button = f"""
        <table cellpadding="0" cellspacing="0" border="0" style="margin:26px 0 8px;">
          <tr><td align="center" style="border-radius:12px;background-color:#0f8f45;">
            <a href="{button_url}" style="display:inline-block;padding:14px 30px;color:#ffffff;font-weight:700;font-size:15px;text-decoration:none;border-radius:12px;">{button_label}</a>
          </td></tr>
        </table>
        <p style="color:#9ca3af;font-size:13px;line-height:1.6;margin:6px 0 0;">Or paste this link into your browser:<br>
          <a href="{button_url}" style="color:#0f8f45;word-break:break-all;">{button_url}</a></p>
        """
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f6f5;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f6f5;padding:28px 12px;font-family:Arial,Helvetica,sans-serif;">
    <tr><td align="center">
      <table width="540" cellpadding="0" cellspacing="0" border="0" style="max-width:540px;width:100%;background:#ffffff;border:1px solid #e8eae9;border-radius:18px;overflow:hidden;">
        <tr><td style="background-color:#0f8f45;background:linear-gradient(135deg,#16a34a,#064e2a);padding:26px 32px;" align="left">
          <img src="{logo}" width="40" height="40" alt="KhomaAPI" style="border-radius:10px;vertical-align:middle;">
          <span style="color:#ffffff;font-size:21px;font-weight:800;letter-spacing:-0.4px;vertical-align:middle;margin-left:12px;">KhomaAPI</span>
        </td></tr>
        <tr><td style="padding:34px 36px 8px;">
          <h1 style="margin:0 0 14px;font-size:22px;line-height:1.3;color:#111827;">{heading}</h1>
          <div style="color:#4b5563;font-size:15px;line-height:1.65;">{message_html}</div>
          {button}
        </td></tr>
        <tr><td style="padding:22px 36px 28px;"></td></tr>
        <tr><td style="padding:20px 36px;border-top:1px solid #eef0ef;background:#fafbfa;">
          <p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.7;">
            <b style="color:#6b7280;">KhomaAlgorithms</b> — automated TradingView → broker execution.<br>
            Questions? <a href="mailto:support@khomaalgorithms.net" style="color:#0f8f45;text-decoration:none;">support@khomaalgorithms.net</a>
          </p>
        </td></tr>
      </table>
      <p style="color:#b6bbc0;font-size:11px;margin:16px 0 0;">© KhomaAlgorithms · You received this because this email was used on KhomaAPI.</p>
    </td></tr>
  </table>
</body></html>"""


def send_branded_email(to_email, subject, heading, message_html, button_label="", button_url="", text_fallback="") -> bool:
    """Send a branded HTML email (with plain-text fallback)."""
    html = email_html(heading, message_html, button_label, button_url)
    text = text_fallback or f"{heading}\n\n{button_url}".strip()
    return send_email(to_email, subject, text, html=html)



def init_db():
    # Retry the first connection — Railway's private network needs a few seconds
    # after boot before postgres.railway.internal is reachable.
    con = dbmod.wait_for_db(str(DB_PATH))
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
        max_orders INTEGER DEFAULT 200,
        duplicate_seconds INTEGER DEFAULT 8,
        max_rejections_per_day INTEGER DEFAULT 50,
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

    # KhomaTradingJournal: tags + notes + screenshot per CLOSED round-trip
    # (keyed by a stable hash of the trip so it survives re-reads from fills).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trip_journal(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        trip_key TEXT NOT NULL,
        tags TEXT DEFAULT '',
        note TEXT DEFAULT '',
        image_path TEXT,
        created_at TEXT,
        updated_at TEXT,
        UNIQUE(user_id, trip_key)
    )
    """)

    # Whop memberships seen by webhook BEFORE a matching KhomaAPI account exists
    # (buy-first-then-signup). Lets login link by a cheap local lookup instead of
    # scanning the Whop API on every login.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS whop_pending(
        email TEXT PRIMARY KEY,
        membership_id TEXT,
        updated_at TEXT
    )
    """)

    # Persistent daily PnL / equity snapshot per account. The 1s poller computes
    # penny-exact day_pnl (equity - netLiqSOD) but only in memory; we save it here
    # so the journal, calendar, equity curve, and email reports have a permanent
    # record independent of Tradovate's limited fill history.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_equity(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        account_name TEXT,
        trade_date TEXT NOT NULL,
        net_liq REAL,
        net_liq_sod REAL,
        day_pnl REAL,
        updated_at TEXT,
        UNIQUE(account_id, trade_date)
    )
    """)

    # Permanent per-trade log: each CLOSED round-trip saved once (dedup by
    # trip_key) so the verified page keeps a forever trade-by-trade record,
    # independent of Tradovate's limited fill history.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trade_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        account_id INTEGER,
        account_name TEXT,
        trip_key TEXT NOT NULL,
        side TEXT, symbol TEXT, qty INTEGER,
        entry_price REAL, exit_price REAL, pnl REAL,
        opened_at TEXT, closed_at TEXT, created_at TEXT,
        UNIQUE(user_id, trip_key)
    )
    """)

    # --- Additive column migrations (safe on existing DBs) ---
    def ensure_column(table, column, decl):
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except Exception:
            pass

    ensure_column("users", "is_verified", "INTEGER DEFAULT 1")
    # Performance email digests (opt-in) + last-sent period markers.
    ensure_column("users", "digest_daily", "INTEGER DEFAULT 0")
    ensure_column("users", "digest_weekly", "INTEGER DEFAULT 0")
    ensure_column("users", "digest_monthly", "INTEGER DEFAULT 0")
    ensure_column("users", "digest_daily_sent", "TEXT")
    ensure_column("users", "digest_weekly_sent", "TEXT")
    ensure_column("users", "digest_monthly_sent", "TEXT")
    ensure_column("email_verifications", "kind", "TEXT DEFAULT 'change_email'")
    ensure_column("email_verifications", "payload", "TEXT")
    ensure_column("broker_accounts", "in_copy_box", "INTEGER DEFAULT 0")
    # Account grouping: 'independent' (Available — traded per-account) or
    # 'copy' (Copy Trading — every master signal mirrored across the group).
    ensure_column("broker_accounts", "group_type", "TEXT DEFAULT 'independent'")
    ensure_column("trades", "fill_price", "REAL")
    ensure_column("trades", "pnl", "REAL")
    # Whop billing link + subscription state (Whop is the source of truth).
    ensure_column("users", "whop_user_id", "TEXT")
    ensure_column("users", "whop_membership_id", "TEXT")
    ensure_column("users", "whop_plan_id", "TEXT")
    ensure_column("users", "subscription_status", "TEXT")
    ensure_column("users", "current_period_end", "TEXT")
    # Testing-only plan override (gated by ALLOW_MANUAL_PLAN; never beats Whop).
    ensure_column("users", "manual_plan", "TEXT")

    # Backfill group_type from the legacy in_copy_box flag, then normalize nulls.
    cur.execute("UPDATE broker_accounts SET group_type='copy' WHERE in_copy_box=1 AND (group_type IS NULL OR group_type='independent')")
    cur.execute("UPDATE broker_accounts SET group_type='independent' WHERE group_type IS NULL OR group_type=''")

    # Watchdog / heartbeat state per connected account.
    ensure_column("broker_accounts", "last_heartbeat", "TEXT")
    ensure_column("broker_accounts", "connectivity", "TEXT DEFAULT 'unknown'")
    # Per-account position sizing: fixed contracts to trade on this account.
    # NULL = N/A = use the quantity from the TradingView alert.
    ensure_column("broker_accounts", "contract_qty", "INTEGER")

    # Per-account risk configuration + runtime state (the Risk Engine).
    cur.execute("""
    CREATE TABLE IF NOT EXISTS account_risk_config(
        account_id INTEGER PRIMARY KEY,            -- broker_accounts.id
        user_id INTEGER NOT NULL,
        enabled INTEGER DEFAULT 1,
        daily_loss_limit REAL,
        trailing_dd REAL,
        trailing_basis TEXT DEFAULT 'intraday',    -- 'intraday' | 'closed'
        profit_target REAL,
        max_position INTEGER,
        max_contracts_per_order INTEGER,
        max_open_positions INTEGER,
        daily_trade_cap INTEGER,
        hours_start TEXT,                          -- 'HH:MM'
        hours_end TEXT,
        tz TEXT DEFAULT 'America/New_York',
        reset_hour INTEGER DEFAULT 17,             -- session reset (local hour)
        -- runtime state (persisted so locks survive restarts):
        locked INTEGER DEFAULT 0,
        locked_reason TEXT DEFAULT '',
        lock_expires_at TEXT,
        high_water_mark REAL,
        hwm_day_anchor TEXT,                        -- session start the HWM belongs to
        updated_at TEXT
    )
    """)

    # Prop-firm evaluation -> funded lifecycle fields.
    ensure_column("account_risk_config", "prop_firm", "TEXT DEFAULT ''")
    ensure_column("account_risk_config", "account_phase", "TEXT DEFAULT 'evaluation'")  # evaluation | funded
    ensure_column("account_risk_config", "profit_goal", "REAL")          # eval profit target ($)
    ensure_column("account_risk_config", "profit_factor_target", "REAL")  # tracked performance metric
    ensure_column("account_risk_config", "eval_start_balance", "REAL")    # baseline equity for progress
    ensure_column("account_risk_config", "eval_passed", "INTEGER DEFAULT 0")
    ensure_column("account_risk_config", "eval_passed_at", "TEXT")
    ensure_column("account_risk_config", "buffer_zone", "REAL")           # funded cushion before max loss
    ensure_column("account_risk_config", "funded_daily_loss", "REAL")
    ensure_column("account_risk_config", "funded_max_loss", "REAL")
    # Intraday give-back limit (separate from trailing DD) + consistency rule (%).
    ensure_column("account_risk_config", "intraday_dd", "REAL")
    ensure_column("account_risk_config", "consistency_pct", "REAL")
    # 'manual' (one-click block) vs 'auto' (materialized from a recurring rule).
    ensure_column("news_windows", "source", "TEXT DEFAULT 'manual'")
    ensure_column("trade_log", "account_name", "TEXT")
    # Accounts that were defaulted to 'evaluation' but have no prop goal set are
    # really standard accounts -> stop the misleading EVALUATION badge.
    cur.execute("""UPDATE account_risk_config SET account_phase='standard'
                   WHERE account_phase='evaluation'
                   AND (profit_goal IS NULL OR profit_goal=0)
                   AND (eval_passed IS NULL OR eval_passed=0)""")

    # User-defined news / manual lockout windows.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS news_windows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        account_id INTEGER,                         -- NULL = all accounts
        starts_at TEXT NOT NULL,                    -- ISO UTC
        ends_at TEXT NOT NULL,
        label TEXT DEFAULT '',
        source TEXT DEFAULT 'manual',               -- 'manual' | 'auto' (from a rule)
        created_at TEXT
    )
    """)

    # Recurring news lockout RULES: "never trade CPI/FOMC/PPI…". Matched against
    # the live calendar each cycle to auto-create news_windows (source='auto').
    cur.execute("""
    CREATE TABLE IF NOT EXISTS news_rules(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        account_id INTEGER,                         -- NULL = all accounts
        keyword TEXT NOT NULL,                      -- case-insensitive substring of event title
        minutes_before INTEGER DEFAULT 15,
        minutes_after INTEGER DEFAULT 15,
        enabled INTEGER DEFAULT 1,
        created_at TEXT
    )
    """)

    con.commit()
    con.close()
    # Concurrent background poller + request threads -> WAL reduces write locking.
    try:
        c2 = db()
        c2.execute("PRAGMA journal_mode=WAL")
        c2.close()
    except Exception:
        pass


init_db()


def email_enabled() -> bool:
    """True when ANY outbound email provider is configured — Resend (one API
    key, no SMTP) or classic SMTP."""
    return bool(
        os.getenv("RESEND_API_KEY")
        or (os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS"))
    )


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


# ============================================================
# SUBSCRIPTION / ENTITLEMENT ENFORCEMENT (Whop)
# ============================================================

class Entitlement:
    """A user's resolved live access: active?, which tier, and where it came
    from. Feature/cap answers delegate to the pure `entitlements` module."""
    __slots__ = ("active", "tier", "source")

    def __init__(self, active, tier, source):
        self.active = bool(active)
        self.tier = tier
        self.source = source  # "whop" | "manual" | "none"

    @property
    def max_accounts(self):
        return ent.max_accounts(self.tier) if self.active else 0

    def has(self, feature):
        return self.active and ent.has_feature(self.tier, feature)


def _ucol(user, key):
    """Read a column from a sqlite Row / psycopg dict / plain dict, safely."""
    if user is None:
        return None
    try:
        return user[key]
    except (KeyError, IndexError, TypeError):
        getter = getattr(user, "get", None)
        return getter(key) if getter else None


def user_entitlements(user) -> Entitlement:
    """Resolve a user's entitlement. Whop is authoritative: if the account is
    linked to a membership, only Whop's status counts. The manual test flag
    applies ONLY when there is no Whop linkage AND ALLOW_MANUAL_PLAN is on — so
    a local flag can never grant access Whop says is inactive."""
    if user is None:
        return Entitlement(False, None, "none")

    membership = _ucol(user, "whop_membership_id")
    if membership:
        tier = ent.tier_for_plan_id(_ucol(user, "whop_plan_id"))
        active = ent.subscription_active(
            _ucol(user, "subscription_status"),
            _ucol(user, "current_period_end"),
        ) and tier is not None
        return Entitlement(active, tier if active else None, "whop")

    if ALLOW_MANUAL_PLAN:
        mp = ent.normalize_tier(_ucol(user, "manual_plan"))
        if mp:
            return Entitlement(True, mp, "manual")

    return Entitlement(False, None, "none")


_FEATURE_LABELS = {
    ent.COPY_TRADING: "Copy Trading",
    ent.EVAL_FUNDED: "Eval → Funded tracking & prop presets",
    ent.EMAIL_DIGESTS: "Performance email digests",
}


def _is_api_request(request: Request) -> bool:
    p = request.url.path
    if p.startswith("/api") or p.startswith("/webhook"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def whop_checkout_url(plan_id: str) -> str:
    """Whop hosted checkout for a plan id. Falls back to the marketing page."""
    pid = (plan_id or "").strip()
    return f"https://whop.com/checkout/{pid}" if pid else PRICING_URL


# Display catalog. Plan ids come from env (never hardcoded); checkout URLs are
# derived from them. Order = display order.
_PLAN_CARDS = [
    {"name": "Solo", "accounts": "2 connected accounts",
     "feats": ["Real-time risk engine", "Economic calendar", "Journal + live PnL"],
     "missing": ["Copy trading", "Eval → funded + presets", "Email digests"],
     "buys": [("WHOP_PLAN_SOLO_M", "$49 / mo"), ("WHOP_PLAN_SOLO_Y", "$490 / yr")]},
    {"name": "Pro", "accounts": "10 connected accounts", "highlight": True,
     "feats": ["Everything in Solo", "Copy trading", "Eval → funded + all presets",
               "Performance email digests"],
     "missing": [],
     "buys": [("WHOP_PLAN_PRO_M", "$99 / mo"), ("WHOP_PLAN_PRO_Y", "$990 / yr")]},
    {"name": "Elite", "accounts": "Unlimited connected accounts",
     "feats": ["Everything in Pro", "Unlimited accounts", "Priority execution"],
     "missing": [],
     "buys": [("WHOP_PLAN_ELITE_M", "$199 / mo"), ("WHOP_PLAN_ELITE_Y", "$1,990 / yr")]},
    {"name": "Founder", "accounts": "10 accounts · first 100 members",
     "feats": ["Everything in Pro", "Locked-in founder pricing"],
     "missing": [],
     "buys": [("WHOP_PLAN_FOUNDER", "$990 / yr")]},
]


def plans_html(banner: str = "") -> str:
    """Self-contained, responsive pricing page used for subscribe/upgrade."""
    cards = []
    for c in _PLAN_CARDS:
        buys = "".join(
            f'<a class="pbtn" href="{whop_checkout_url(os.getenv(k, ""))}">{label}</a>'
            for k, label in c["buys"] if os.getenv(k, "").strip()
        )
        feats = "".join(f'<li class="ok">{f}</li>' for f in c["feats"])
        feats += "".join(f'<li class="no">{f}</li>' for f in c.get("missing", []))
        cls = "plan hot" if c.get("highlight") else "plan"
        tag = '<div class="tag">Most popular</div>' if c.get("highlight") else ""
        cards.append(f'''<div class="{cls}">{tag}
          <h3>{c["name"]}</h3><div class="acc">{c["accounts"]}</div>
          <ul>{feats}</ul><div class="buys">{buys}</div></div>''')
    grid = "".join(cards)
    banner_html = f'<div class="banner">{banner}</div>' if banner else ""
    return f"""<!DOCTYPE html><html><head><title>KhomaAPI — Plans</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{{box-sizing:border-box;}}
body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;background:#f8faf9;color:#111827;}}
.wrap{{max-width:1100px;margin:0 auto;padding:48px 20px 70px;}}
.head{{text-align:center;margin-bottom:30px;}}
.head h1{{font-size:34px;letter-spacing:-1.2px;margin:0 0 8px;}}
.head p{{color:#6b7280;margin:0;font-size:16px;}}
.banner{{background:#eaf7ef;border:1px solid #cdebd8;color:#086b34;border-radius:14px;padding:14px 18px;font-weight:700;margin-bottom:24px;text-align:center;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:18px;}}
.plan{{background:#fff;border:1px solid #e5e7eb;border-radius:20px;padding:24px;display:flex;flex-direction:column;position:relative;box-shadow:0 18px 60px rgba(17,24,39,.05);}}
.plan.hot{{border-color:#0f8f45;box-shadow:0 24px 70px rgba(15,143,69,.16);}}
.tag{{position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:#0f8f45;color:#fff;font-size:12px;font-weight:800;padding:5px 12px;border-radius:999px;}}
.plan h3{{font-size:22px;margin:0;letter-spacing:-.4px;}}
.acc{{color:#6b7280;font-size:13px;margin:4px 0 14px;}}
.plan ul{{list-style:none;padding:0;margin:0 0 18px;font-size:14px;line-height:1.95;flex:1;}}
.plan li.ok::before{{content:"✓ ";color:#0f8f45;font-weight:900;}}
.plan li.no{{color:#9ca3af;}} .plan li.no::before{{content:"— ";}}
.buys{{display:flex;flex-direction:column;gap:8px;}}
.pbtn{{display:block;text-align:center;background:linear-gradient(135deg,#12a150,#087135);color:#fff;text-decoration:none;font-weight:800;padding:12px;border-radius:12px;box-shadow:0 12px 28px rgba(15,143,69,.18);}}
.pbtn:hover{{filter:brightness(1.06);}}
.foot{{text-align:center;margin-top:28px;color:#6b7280;font-size:14px;}}
.foot a{{color:#0f8f45;font-weight:800;}}
</style></head><body><div class="wrap">
  <div class="head"><h1>Choose your KhomaAPI plan</h1><p>Paid from day one · cancel anytime · access runs to period end</p></div>
  {banner_html}
  <div class="grid">{grid}</div>
  <p class="foot">Already bought with a different email? <a href="/settings">Link your Whop account</a> · <a href="/login">Log in</a> · <a href="/logout">Log out</a></p>
</div></body></html>"""


def deny_response(request: Request, kind: str, feature: str = None, api: bool = None):
    """kind: 'auth' (not logged in) | 'inactive' (no active sub) | 'feature'."""
    if api is None:
        api = _is_api_request(request)
    if kind == "auth":
        if api:
            return JSONResponse(status_code=401, content={"ok": False, "error": "not authenticated"})
        return RedirectResponse("/login", status_code=302)
    if kind == "inactive":
        if api:
            return JSONResponse(status_code=402, content={
                "ok": False, "error": "no active subscription", "subscribe": PRICING_URL})
        return RedirectResponse("/subscribe", status_code=302)
    # feature upgrade required
    if api:
        return JSONResponse(status_code=403, content={
            "ok": False, "error": f"plan upgrade required: {feature}", "subscribe": PRICING_URL})
    return RedirectResponse(f"/subscribe?upgrade={feature or '1'}", status_code=302)


def gate(request: Request, feature: str = None, api: bool = None):
    """Per-route guard. Returns (user, entitlement, deny_response_or_None).

    When ENFORCE_SUBSCRIPTIONS is off it only enforces login (preserving current
    behavior for a safe rollout). When on, it also requires an active
    subscription and, if `feature` is given, that the plan includes it.
    Pass api=True for JSON/XHR endpoints so denials come back as JSON, not HTML."""
    user = current_user(request)
    if not user:
        return None, None, deny_response(request, "auth", api=api)
    e = user_entitlements(user)
    if ENFORCE_SUBSCRIPTIONS:
        if not e.active:
            return user, e, deny_response(request, "inactive", api=api)
        if feature and not e.has(feature):
            return user, e, deny_response(request, "feature", feature=feature, api=api)
    return user, e, None


def account_cap_remaining(user, e: Entitlement):
    """How many more accounts this user may connect. None = unlimited.
    No-op (unlimited) when enforcement is off, so nothing breaks pre-go-live."""
    if not ENFORCE_SUBSCRIPTIONS:
        return None
    cap = e.max_accounts
    if cap is None:
        return None
    current = len(get_broker_accounts(user["id"], connected_only=True))
    return max(0, cap - current)


def webhook_subscription_ok(user) -> bool:
    """Active-subscription check for the trade webhook (auth is by payload, not
    session). No-op when enforcement is off."""
    if not ENFORCE_SUBSCRIPTIONS:
        return True
    return user_entitlements(user).active


def _apply_membership_state(user_id, st):
    con = db()
    con.execute(
        "UPDATE users SET whop_user_id=?, whop_membership_id=?, whop_plan_id=?, "
        "subscription_status=?, current_period_end=? WHERE id=?",
        (st["user_id"], st["membership_id"], st["plan_id"], st["status"],
         st["period_end"], user_id),
    )
    con.commit()
    con.close()


def link_membership_to_user(membership, by_user_id=None):
    """Persist a Whop membership's state onto the matching KhomaAPI user. Matches
    by explicit user id, then by membership id (so renewals/revokes find the same
    account even if emails differ), then by email (first link). Only memberships
    on one of OUR plans are linked — the separate manual-signals product is
    ignored."""
    st = whopmod.membership_state(membership)
    if st["plan_id"] and ent.tier_for_plan_id(st["plan_id"]) is None:
        return None
    con = db()
    row = None
    if by_user_id is not None:
        row = con.execute("SELECT id FROM users WHERE id=?", (by_user_id,)).fetchone()
    if not row and st["membership_id"]:
        row = con.execute("SELECT id FROM users WHERE whop_membership_id=?",
                          (st["membership_id"],)).fetchone()
    if not row and st["email"]:
        row = con.execute("SELECT id FROM users WHERE email=?", (st["email"],)).fetchone()
    con.close()
    if not row:
        return None
    _apply_membership_state(row["id"], st)
    return row["id"]


def try_link_whop(user):
    """Cheap auto-link on login: if the account isn't linked and the webhook
    previously recorded a pending membership for this email, attach it. A normal
    login by a non-customer costs one indexed SELECT (no Whop API call). Never
    raises into the login flow."""
    try:
        if not WHOP_API_KEY or _ucol(user, "whop_membership_id"):
            return
        email = (_ucol(user, "email") or "").lower().strip()
        if not email:
            return
        con = db()
        row = con.execute("SELECT membership_id FROM whop_pending WHERE email=?", (email,)).fetchone()
        con.close()
        if not row:
            return
        m = whopmod.fetch_membership(row["membership_id"], WHOP_API_KEY)
        if m and ent.tier_for_plan_id(m.get("plan")):
            link_membership_to_user(m, by_user_id=user["id"])
        con = db()
        con.execute("DELETE FROM whop_pending WHERE email=?", (email,))
        con.commit()
        con.close()
    except Exception as e:
        print(f"whop auto-link skipped: {e}")


def whop_membership_for_email(email):
    """Return an ACTIVE Whop membership (on one of our plans) for this email, or
    None. Checks the local pending table first (recorded by the webhook), then
    the Whop API. Used to gate account creation to real buyers."""
    email = (email or "").lower().strip()
    if not email or not WHOP_API_KEY:
        return None
    try:
        con = db()
        row = con.execute("SELECT membership_id FROM whop_pending WHERE email=?", (email,)).fetchone()
        con.close()
        if row:
            m = whopmod.fetch_membership(row["membership_id"], WHOP_API_KEY)
            if m and m.get("valid") and ent.tier_for_plan_id(m.get("plan")):
                return m
        m = whopmod.find_membership_by_email(
            email, WHOP_API_KEY, allowed_plan_ids=list(ent.plan_env_map().keys()))
        if m and m.get("valid"):
            return m
    except Exception as e:
        print(f"whop membership lookup failed: {e}")
    return None


# Central guard so an active subscription is enforced on EVERY protected HTTP
# request, not just routes that remember to check. No-op until enforcement is
# switched on. Public/auth/asset/webhook paths are allow-listed; feature-level
# gates (copy trading, eval→funded, digests) and account caps live in the
# individual routes.
_PUBLIC_PATHS = {
    "/", "/login", "/signup", "/logout", "/health", "/status", "/api/status",
    "/subscribe", "/upgrade", "/test", "/debug-static", "/results",
    "/favicon.ico", "/favicon.png",
}
_PUBLIC_PREFIXES = (
    "/static", "/uploads", "/auth/google", "/verify-email",
    "/confirm-email-change", "/verify-email-change", "/confirm-password-change",
    "/forgot-password", "/reset-password", "/oauth/callback", "/webhook",
    "/whop", "/verified",
)
# Diagnostic endpoints that must 404 in production (DEBUG_ENDPOINTS off).
_DEBUG_PATHS = ("/debug", "/test", "/debug-static", "/create-broker-table", "/oauth-test")


# Rate limiter (per-process, in-memory). Limits are generous flood/brute-force
# guards, not strict throttles — sized not to drop legitimate traffic.
LIMITER = sec.RateLimiter()
# Webhooks authenticate by token, not cookies, and are legitimately cross-origin
# (TradingView / Whop), so they're exempt from the same-origin CSRF check.
_CSRF_EXEMPT = {"/webhook/trade", "/webhook/flatten", "/whop/webhook"}
# Pre-auth entry endpoints — may be posted from the marketing site; they create
# (not mutate) a session, so the same-origin requirement is relaxed for them.
_AUTH_CSRF_EXEMPT = {"/login", "/signup", "/forgot-password"}


def _rate_rule(request: Request):
    """(limit, window_seconds) for sensitive POSTs, else None."""
    if request.method != "POST":
        return None
    p = request.url.path
    if p == "/login":
        return (30, 300)
    if p == "/signup":
        return (10, 3600)
    if p == "/forgot-password":
        return (8, 3600)
    if p.startswith("/reset-password"):
        return (20, 3600)
    if p in ("/webhook/trade", "/webhook/flatten"):
        return (1200, 60)   # flood guard only; per-user throttle is dedup+max_orders
    if p == "/whop/webhook":
        return (600, 60)
    return None


@app.middleware("http")
async def edge_security(request: Request, call_next):
    # 0) Diagnostic endpoints are 404 in production (prevent data leakage).
    if not DEBUG_ENDPOINTS:
        p = request.url.path
        if p == "/test" or p.startswith(_DEBUG_PATHS):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
    # 1) Rate-limit sensitive endpoints (per real client IP via Cloudflare).
    rule = _rate_rule(request)
    if rule:
        ip = sec.client_ip(request)
        allowed, retry = LIMITER.hit(f"rl:{request.url.path}:{ip}", rule[0], rule[1])
        if not allowed:
            return JSONResponse(status_code=429, content={"ok": False, "error": "rate limit exceeded"},
                                headers={"Retry-After": str(retry)})
    # 2) CSRF: cookie-authenticated state changes must come from us. Pre-auth
    #    entry endpoints (login/signup/password-reset) are exempt — they may be
    #    posted from the marketing site and establish (not mutate) a session;
    #    SameSite=lax still protects the session afterward. Our sibling domains
    #    are trusted; every other origin is blocked.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        p = request.url.path
        exempt = (p in _CSRF_EXEMPT or p in _AUTH_CSRF_EXEMPT
                  or p.startswith("/reset-password"))
        if not exempt and not sec.is_same_origin(request, TRUSTED_ORIGIN_HOSTS):
            return JSONResponse(status_code=403, content={"ok": False, "error": "cross-origin request blocked"})
    # 3) Process, then attach security headers to every response.
    resp = await call_next(request)
    for k, v in sec.SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    # Never let the browser/CDN cache dynamic app pages — prevents a stale error
    # (e.g. an old 403) from being replayed. Static assets keep normal caching.
    p = request.url.path
    if not (p.startswith("/static") or p.startswith("/uploads")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.middleware("http")
async def subscription_guard(request: Request, call_next):
    # Diagnostic endpoints 404 in production (checked first so a lingering
    # inactive session can't turn a /debug hit into a 402 before this).
    if not DEBUG_ENDPOINTS:
        dp = request.url.path
        if dp == "/test" or dp.startswith(_DEBUG_PATHS):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if not ENFORCE_SUBSCRIPTIONS:
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)
    # Run the cookie+DB lookup off the event loop.
    from starlette.concurrency import run_in_threadpool
    user = await run_in_threadpool(current_user, request)
    if user is None:
        # Not logged in → let the route's existing /login redirect handle it.
        return await call_next(request)
    if not user_entitlements(user).active:
        return deny_response(request, "inactive")
    return await call_next(request)


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
    """Connected accounts in the Copy Trading group (group_type='copy').

    Every master signal is mirrored 1:1 across all of these accounts.
    """
    return [a for a in get_broker_accounts(user_id, connected_only=True)
            if (a.get("group_type") or "independent") == "copy"]


def get_independent_accounts(user_id: int):
    """Connected accounts in the Available group (group_type='independent').

    These trade only signals explicitly routed to them by account name, so
    different strategies can run on different accounts independently.
    """
    return [a for a in get_broker_accounts(user_id, connected_only=True)
            if (a.get("group_type") or "independent") != "copy"]


def set_account_group(user_id: int, account_db_id: int, group_type: str) -> None:
    """Move one account into 'independent' (Available) or 'copy' (Copy Trading)."""
    group_type = "copy" if str(group_type).lower() == "copy" else "independent"
    con = db()
    # Keep the legacy in_copy_box flag in sync so older reads stay consistent.
    con.execute(
        "UPDATE broker_accounts SET group_type=?, in_copy_box=? WHERE id=? AND user_id=?",
        (group_type, 1 if group_type == "copy" else 0, account_db_id, user_id),
    )
    con.commit()
    con.close()


# ============================================================
# RISK ENGINE — per-account config, live state, enforcement, kill-switch
# ============================================================

RISK_FIELDS = (
    "daily_loss_limit", "trailing_dd", "trailing_basis", "intraday_dd",
    "consistency_pct", "profit_target",
    "max_position", "max_contracts_per_order", "max_open_positions",
    "daily_trade_cap", "hours_start", "hours_end", "tz", "reset_hour", "enabled",
    # Prop-firm evaluation -> funded lifecycle.
    "prop_firm", "account_phase", "profit_goal", "profit_factor_target",
    "buffer_zone", "funded_daily_loss", "funded_max_loss",
)

# Recent live state per account_id -> (state, monotonic-ish ts). Written by the
# background poller and by account_live_state(); read by the pre-order gate so
# order placement stays fast (no extra broker round-trip per alert).
ACCOUNT_STATE_CACHE: Dict[int, tuple] = {}
STATE_CACHE_TTL = 8  # seconds before the gate refetches synchronously

# Lightweight per-account cache for the 1-second fast breach loop: the realized
# day-PnL component (only changes on a fill) + whether a position is open.
_ACCT_FAST: Dict[int, dict] = {}


def get_risk_config(account_id: int) -> dict:
    con = db()
    row = con.execute("SELECT * FROM account_risk_config WHERE account_id=?", (account_id,)).fetchone()
    con.close()
    return dict(row) if row else {}


def ensure_risk_config(account_id: int, user_id: int) -> dict:
    cfg = get_risk_config(account_id)
    if cfg:
        return cfg
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO account_risk_config(account_id,user_id,account_phase,updated_at) VALUES(?,?,'standard',?)",
        (account_id, user_id, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return get_risk_config(account_id)


def save_risk_config(account_id: int, user_id: int, values: dict) -> None:
    ensure_risk_config(account_id, user_id)
    cols = [k for k in RISK_FIELDS if k in values]
    if not cols:
        return
    sets = ", ".join(f"{c}=?" for c in cols) + ", updated_at=?"
    params = [values[c] for c in cols] + [datetime.now(timezone.utc).isoformat(), account_id, user_id]
    con = db()
    con.execute(f"UPDATE account_risk_config SET {sets} WHERE account_id=? AND user_id=?", params)
    con.commit()
    con.close()


def set_account_lock(account_id: int, locked: bool, reason: str = "", expires_at: str = "") -> None:
    con = db()
    con.execute(
        "UPDATE account_risk_config SET locked=?, locked_reason=?, lock_expires_at=?, updated_at=? WHERE account_id=?",
        (1 if locked else 0, reason, expires_at, datetime.now(timezone.utc).isoformat(), account_id),
    )
    con.commit()
    con.close()


def _maybe_auto_unlock(cfg: dict) -> dict:
    """Locks auto-clear at the next session reset (new trading day)."""
    if cfg.get("locked") and cfg.get("lock_expires_at"):
        try:
            expires = datetime.fromisoformat(str(cfg["lock_expires_at"]).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expires:
                set_account_lock(cfg["account_id"], False, "", "")
                cfg = get_risk_config(cfg["account_id"])
        except Exception:
            pass
    return cfg


def effective_risk_cfg(cfg: dict) -> dict:
    """Resolve the daily-loss / trailing-DD limits actually in force given the
    account phase. In 'funded' mode the funded_* limits apply, and buffer_zone
    locks that many dollars BEFORE the firm's hard max loss (a safety cushion)."""
    eff = dict(cfg)
    dll, tdd, ptarget = risk.resolve_phase_limits(cfg)
    eff["daily_loss_limit"] = dll
    eff["trailing_dd"] = tdd
    eff["profit_target"] = ptarget
    return eff


def eval_profit_value(cfg: dict, state: dict):
    """Profit toward the evaluation goal: current equity minus the baseline
    captured when tracking started."""
    eq = state.get("equity")
    base = cfg.get("eval_start_balance")
    if eq is None or base is None:
        return None
    return round(eq - base, 2)


def profit_factor_from_trips(trips: list):
    wins = sum(float(t.get("pnl") or 0) for t in trips if (t.get("pnl") or 0) > 0)
    losses = -sum(float(t.get("pnl") or 0) for t in trips if (t.get("pnl") or 0) < 0)
    if losses <= 0:
        return None  # undefined (no losing trades yet)
    return round(wins / losses, 2)


def check_eval_pass(a: dict, cfg: dict, state: dict):
    """If an account in evaluation reaches its profit goal, mark it passed, email
    the client, and return a broadcast event. Baseline is captured on first sight."""
    if (cfg.get("account_phase") or "evaluation") != "evaluation":
        return None
    if cfg.get("eval_passed"):
        return None
    goal = risk._num(cfg.get("profit_goal"))
    if not goal:
        return None
    eq = state.get("equity")
    if eq is None:
        return None
    if cfg.get("eval_start_balance") is None:
        con = db()
        con.execute("UPDATE account_risk_config SET eval_start_balance=?, updated_at=? WHERE account_id=?",
                    (eq, datetime.now(timezone.utc).isoformat(), a["id"]))
        con.commit()
        con.close()
        return None
    profit = eq - cfg["eval_start_balance"]
    if profit < goal:
        return None
    # Passed!
    now_iso = datetime.now(timezone.utc).isoformat()
    con = db()
    con.execute("UPDATE account_risk_config SET eval_passed=1, eval_passed_at=?, updated_at=? WHERE account_id=?",
                (now_iso, now_iso, a["id"]))
    user = con.execute("SELECT email FROM users WHERE id=?", (a["user_id"],)).fetchone()
    con.commit()
    con.close()
    try:
        if user and user["email"]:
            firm = cfg.get("prop_firm") or "your prop firm"
            send_branded_email(
                user["email"],
                "🎉 You passed your evaluation",
                "Congratulations — evaluation passed!",
                f"Your account <b>{a.get('account_name')}</b> just hit its profit goal of "
                f"<b>${goal:,.0f}</b> for {firm}. 🎯<br><br>"
                f"Your funded-account stage is now unlocked in KhomaAPI — set your buffer zone, "
                f"max loss and daily loss for the funded phase and keep KhomaAPI guarding every trade.",
                button_label="Set up my funded account",
                button_url=f"{APP_URL}/risk",
                text_fallback=f"Congratulations! {a.get('account_name')} reached its ${goal:,.0f} profit goal. Open KhomaAPI to set up your funded account.",
            )
    except Exception:
        pass
    return {"event": "risk", "type": "eval_passed", "account_id": a["id"],
            "account": a.get("account_name"), "user_id": a.get("user_id"),
            "reason": f"Evaluation passed — ${goal:,.0f} goal reached"}


def news_windows_for(user_id: int, account_id: int):
    """Active/future news windows (as UTC datetime tuples) for an account."""
    con = db()
    rows = con.execute(
        "SELECT starts_at, ends_at FROM news_windows WHERE user_id=? AND (account_id IS NULL OR account_id=?)",
        (user_id, account_id),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        try:
            s = datetime.fromisoformat(str(r["starts_at"]).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(r["ends_at"]).replace("Z", "+00:00"))
            if s.tzinfo is None:
                s = s.replace(tzinfo=timezone.utc)
            if e.tzinfo is None:
                e = e.replace(tzinfo=timezone.utc)
            out.append((s, e))
        except Exception:
            continue
    return out


def account_live_state(account: dict, cfg: dict, now_utc: datetime = None) -> dict:
    """Fetch live equity / day-PnL / positions for one account from Tradovate.

    Equity = totalCashValue + openPnL (net liquidation). Day PnL = realized since
    the session anchor (from fills) + open PnL. Snapshot fields are detected
    broadly; fills are the fallback so a missing field never silently zeroes risk.
    Returns {ok, equity, open_pnl, day_pnl, open_symbols, day_trade_count, flat, ...}.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return {"ok": False, "error": "Reconnect required"}

    try:
        acct_id_int = int(acct_id)
    except Exception:
        acct_id_int = None

    def belongs(o):
        aid = o.get("accountId")
        return acct_id_int is None or aid == acct_id_int or str(aid) == str(acct_id)

    positions = tvo.get_positions(env, token)
    fills = tvo.get_fills(env, token)
    snap = tvo.get_cash_snapshot(env, token, acct_id)
    # A None snapshot AND empty positions/fills => treat as broker unreachable.
    broker_ok = snap is not None or positions or fills

    open_pnl = _snapshot_value(snap, ("openPnL", "openPnl", "unrealizedPnL"))
    # netLiq = real account equity (cash + open PnL, commissions already netted).
    net_liq = _snapshot_value(snap, ("netLiq", "netLiquidatingValue"))
    # netLiqSOD = equity at Start Of Day -> penny-exact daily PnL baseline.
    net_liq_sod = _snapshot_value(snap, ("netLiqSOD", "totalCashValueSOD", "cashSODUSD"))
    cash = _snapshot_value(snap, ("totalCashValue", "totalCashBalance", "cashBalance", "amount"))
    total_pnl = _snapshot_value(snap, ("totalPnL", "totalPnl", "netPnL"))

    # Open positions for this account -> roots + flat flag.
    open_symbols, contract_names = [], {}

    def name_for(cid):
        if cid in contract_names:
            return contract_names[cid]
        c = tvo.get_contract(env, token, cid) or {}
        nm = c.get("name") if isinstance(c, dict) else None
        contract_names[cid] = nm or ""
        return contract_names[cid]

    net_by_root = {}
    for p in (positions or []):
        if not belongs(p):
            continue
        net = int(p.get("netPos") or 0)
        if net == 0:
            continue
        root = symbol_root(name_for(p.get("contractId")).upper())
        open_symbols.append(root)
        net_by_root[root] = net_by_root.get(root, 0) + net
    flat = len(open_symbols) == 0

    # Realized PnL since the session anchor, from this account's fills.
    anchor = risk.session_anchor(now_utc, int(cfg.get("reset_hour") or 17), cfg.get("tz") or "America/New_York")
    day_fills = []
    for f in (fills or []):
        if not belongs(f):
            continue
        ts = str(f.get("timestamp", ""))
        try:
            fts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if fts.tzinfo is None:
                fts = fts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if fts >= anchor:
            day_fills.append(f)
    day_realized = realized_pnl_from_fills(day_fills, name_for) if day_fills else 0.0
    day_trade_count = len(day_fills)

    # Equity: prefer the broker's net liquidation value (penny-exact).
    if net_liq is not None:
        equity = net_liq
    elif cash is not None:
        equity = round(cash + (open_pnl or 0), 2)
    else:
        equity = None

    # Daily PnL — PENNY-EXACT straight from Tradovate: current equity minus
    # start-of-day equity (netLiqSOD already nets commissions and resets at the
    # broker session boundary). Falls back to fills-derived realized + open PnL.
    if equity is not None and net_liq_sod is not None:
        day_pnl = round(equity - net_liq_sod, 2)
        day_realized = round(day_pnl - (open_pnl or 0), 2)
    elif open_pnl is not None or day_fills:
        day_pnl = round((day_realized or 0) + (open_pnl or 0), 2)
    elif total_pnl is not None:
        day_pnl = total_pnl
    else:
        day_pnl = None

    state = {
        "ok": bool(broker_ok),
        "equity": equity,
        "open_pnl": open_pnl,
        "cash": cash,
        "day_realized": round(day_realized or 0, 2),
        "day_pnl": day_pnl,
        "open_symbols": list(set(s for s in open_symbols if s)),
        "net_by_root": net_by_root,
        "day_trade_count": day_trade_count,
        "flat": flat,
        "session_anchor": anchor.isoformat(),
        "news_windows": news_windows_for(account.get("user_id"), account.get("id")),
        "high_water_mark": cfg.get("high_water_mark"),
        "fetched_at": now_utc.isoformat(),
    }
    ACCOUNT_STATE_CACHE[account.get("id")] = (state, time.time())
    # Cache the realized component + position flag for the 1s fast breach loop.
    _ACCT_FAST[account.get("id")] = {
        "day_realized": round(day_realized or 0, 2),
        "anchor": anchor.isoformat(),
        "in_position": not flat,
    }
    return state


def update_account_hwm(account: dict, cfg: dict, state: dict, now_utc: datetime = None):
    """Persist the trailing high-water-mark, resetting it at each new session."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    if state.get("equity") is None:
        return cfg.get("high_water_mark")
    anchor_iso = state.get("session_anchor")
    hwm = cfg.get("high_water_mark")
    # New trading session -> reset HWM to current equity.
    if cfg.get("hwm_day_anchor") != anchor_iso:
        hwm = state["equity"]
    hwm = risk.update_high_water_mark(hwm, state["equity"], cfg.get("trailing_basis") or "intraday", state.get("flat", True))
    con = db()
    con.execute(
        "UPDATE account_risk_config SET high_water_mark=?, hwm_day_anchor=?, updated_at=? WHERE account_id=?",
        (hwm, anchor_iso, datetime.now(timezone.utc).isoformat(), account.get("id")),
    )
    con.commit()
    con.close()
    return hwm


def flatten_and_lock_account(account: dict, reason: str, now_utc: datetime = None) -> dict:
    """Kill-switch: cancel working orders, flatten all positions on the account,
    then LOCK it until the next session reset. Idempotent."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    results = {"cancelled": 0, "flattened": []}
    if token and acct_id:
        try:
            orders = tvo.get_orders(env, token)
            for oid in tvo.working_order_ids(orders, acct_id):
                tvo.cancel_order(env, token, oid)
                results["cancelled"] += 1
        except Exception:
            pass
        # Flatten every open position on the account.
        try:
            for p in (tvo.get_positions(env, token) or []):
                if str(p.get("accountId")) != str(acct_id):
                    continue
                net = int(p.get("netPos") or 0)
                if net == 0:
                    continue
                cname = ((tvo.get_contract(env, token, p.get("contractId")) or {}).get("name") or "").upper()
                action = "Sell" if net > 0 else "Buy"
                resp = tvo.place_order(env, token, account.get("account_name"), acct_id, action, cname, abs(net))
                results["flattened"].append({"symbol": cname, "qty": abs(net), "ok": _order_ok(resp)})
        except Exception as e:
            results["flatten_error"] = str(e)

    expires = risk.next_session_anchor(
        now_utc,
        int((get_risk_config(account.get("id")).get("reset_hour")) or 17),
        get_risk_config(account.get("id")).get("tz") or "America/New_York",
    ).isoformat()
    set_account_lock(account.get("id"), True, reason, expires)
    # Log the kill-switch event for the audit trail.
    try:
        log_trade(account.get("user_id"), f"risk-lock-{account.get('id')}", "*", "flatten", 0,
                  "live", "RISK_LOCK", 0, f"RISK LOCK [{account.get('account_name')}]: {reason}", results)
    except Exception:
        pass
    return results


def quick_snapshot_state(account: dict, cfg: dict) -> dict:
    """ONE cash-snapshot call -> equity + penny-exact day PnL. Used on the order
    hot path so the risk gate never blocks on a heavy multi-call fetch (the 1s
    poller keeps the full cache warm; this only covers a brand-new account)."""
    token = ensure_fresh_token(account)
    if not token or not account.get("account_id"):
        return {"ok": False}
    snap = tvo.get_cash_snapshot(account.get("env") or "live", token, account.get("account_id"))
    if snap is None:
        return {"ok": False}
    open_pnl = _snapshot_value(snap, ("openPnL", "openPnl", "unrealizedPnL"))
    net_liq = _snapshot_value(snap, ("netLiq", "netLiquidatingValue"))
    sod = _snapshot_value(snap, ("netLiqSOD", "totalCashValueSOD", "cashSODUSD"))
    cash = _snapshot_value(snap, ("totalCashValue", "totalCashBalance", "cashBalance", "amount"))
    equity = net_liq if net_liq is not None else (round(cash + (open_pnl or 0), 2) if cash is not None else None)
    day_pnl = round(equity - sod, 2) if (equity is not None and sod is not None) else None
    state = {"ok": True, "equity": equity, "open_pnl": open_pnl, "day_pnl": day_pnl,
             "net_by_root": {}, "open_symbols": [], "flat": True,
             "high_water_mark": cfg.get("high_water_mark")}
    ACCOUNT_STATE_CACHE[account.get("id")] = (state, time.time())
    return state


def _cached_state(account: dict, cfg: dict):
    """Live state for the order gate. Uses the poller-warmed cache (always 1–10s
    fresh for active accounts) so the hot path adds ~0 latency. Falls back to a
    single snapshot only when there is no cache yet — never the 4-call fetch."""
    cached = ACCOUNT_STATE_CACHE.get(account.get("id"))
    if cached:
        return cached[0]
    try:
        return quick_snapshot_state(account, cfg)
    except Exception:
        return {"ok": False}


def risk_gate(account: dict, side: str, qty: int, resolved_symbol: str):
    """Server-side pre-order risk gate for ONE account. Returns
    (allowed: bool, reason: str, breach: bool). On a hard-limit breach it fires
    the kill-switch (flatten + lock) and blocks the order.

    Hard locks are read from the DB (authoritative, no broker call); numeric caps
    use recent cached live state to keep order placement fast.
    """
    cfg = ensure_risk_config(account.get("id"), account.get("user_id"))
    cfg = _maybe_auto_unlock(cfg)

    # Closing/reducing is always allowed (even when locked).
    if side in ("flatten", "close"):
        return True, "", False

    # Fast path: locked -> reject without any broker call.
    if cfg.get("locked"):
        return False, cfg.get("locked_reason") or "Account locked", False

    # No rules configured -> allow without fetching live state (keeps placement fast).
    if not _risk_active(cfg):
        return True, "", False

    state = _cached_state(account, cfg)
    root = symbol_root(str(resolved_symbol).upper())
    cur_net = (state.get("net_by_root") or {}).get(root, 0)
    if side == "buy":
        resulting = cur_net + qty
    elif side == "sell":
        resulting = cur_net - qty
    elif side == "reverse":
        resulting = -cur_net + (qty if cur_net <= 0 else -qty)
    else:
        resulting = cur_net
    intent = {"side": side, "qty": qty, "symbol_root": root, "resulting_net": resulting}

    decision = risk.evaluate_order(effective_risk_cfg(cfg), state, intent, datetime.now(timezone.utc))
    if decision.action == risk.Decision.BREACH:
        flatten_and_lock_account(account, decision.reason)
        return False, decision.reason, True
    if decision.action == risk.Decision.REJECT:
        return False, decision.reason, False
    return True, "", False


# ============================================================
# WATCHDOG / HEARTBEAT + BACKGROUND RISK POLLER
# ============================================================

HEARTBEAT_STALE = 30       # seconds without a good poll -> DISCONNECTED


def set_heartbeat(account_id: int, ok: bool) -> None:
    con = db()
    con.execute(
        "UPDATE broker_accounts SET last_heartbeat=?, connectivity=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), "connected" if ok else "disconnected", account_id),
    )
    con.commit()
    con.close()


def account_connectivity(account: dict) -> str:
    """CONNECTED / DISCONNECTED based on the last successful heartbeat."""
    hb = _parse_iso(account.get("last_heartbeat") or "")
    if hb is None:
        return (account.get("connectivity") or "unknown")
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - hb).total_seconds() > HEARTBEAT_STALE:
        return "disconnected"
    return account.get("connectivity") or "unknown"


def _has_hard_limits(cfg: dict) -> bool:
    eff = effective_risk_cfg(cfg)
    return any(risk._num(eff.get(k)) for k in
               ("daily_loss_limit", "trailing_dd", "profit_target", "intraday_dd", "consistency_pct"))


def _risk_active(cfg: dict) -> bool:
    """True if this account has ANY enforceable rule or tracked goal configured —
    used to avoid polling live state for accounts with nothing to enforce."""
    if str(cfg.get("enabled", 1)) in ("0", "False", "false"):
        return False
    if cfg.get("locked"):
        return True
    if _has_hard_limits(cfg):
        return True
    # Profit goal needs polling so the evaluation progress bar updates.
    if risk._num(cfg.get("profit_goal")) and not cfg.get("eval_passed"):
        return True
    numeric = ("max_position", "max_contracts_per_order", "max_open_positions", "daily_trade_cap")
    if any(risk._num(cfg.get(k)) for k in numeric):
        return True
    return bool(cfg.get("hours_start") and cfg.get("hours_end"))


def _breach_and_lock(a: dict, cfg: dict, state: dict):
    """Shared: evaluate hard limits and fire the kill-switch. Returns a broadcast
    event dict if the account was just locked, else None."""
    if cfg.get("locked") or not _has_hard_limits(cfg):
        return None
    eff = effective_risk_cfg(cfg)
    reason = risk.evaluate_breach(eff, {**state, "high_water_mark": cfg.get("high_water_mark")})
    if not reason:
        return None
    flatten_and_lock_account(a, reason)
    return {"event": "risk", "type": "lock", "account_id": a.get("id"),
            "account": a.get("account_name"), "user_id": a.get("user_id"), "reason": reason}


def fast_breach_check(a: dict, cfg: dict):
    """1-second hot path: ONE cash-snapshot call -> equity + open PnL -> breach.
    Realized day-PnL is taken from the fast cache (only changes on a fill, which
    the full refresh picks up). This is what gives near-real-time drawdown/daily-
    loss enforcement without hammering the broker."""
    token = ensure_fresh_token(a)
    env = a.get("env") or "live"
    acct_id = a.get("account_id")
    if not token or not acct_id:
        set_heartbeat(a["id"], False)
        return None
    snap = tvo.get_cash_snapshot(env, token, acct_id)
    if snap is None:
        set_heartbeat(a["id"], False)
        return None
    set_heartbeat(a["id"], True)

    open_pnl = _snapshot_value(snap, ("openPnL", "openPnl", "unrealizedPnL"))
    net_liq = _snapshot_value(snap, ("netLiq", "netLiquidatingValue"))
    net_liq_sod = _snapshot_value(snap, ("netLiqSOD", "totalCashValueSOD", "cashSODUSD"))
    cash = _snapshot_value(snap, ("totalCashValue", "totalCashBalance", "cashBalance", "amount"))
    fast = _ACCT_FAST.get(a["id"], {})
    dr = fast.get("day_realized", 0) or 0
    anchor = fast.get("anchor")
    equity = net_liq if net_liq is not None else (round(cash + (open_pnl or 0), 2) if cash is not None else None)
    # Penny-exact, fully live: equity - start-of-day equity (incl. fees).
    if equity is not None and net_liq_sod is not None:
        day_pnl = round(equity - net_liq_sod, 2)
    else:
        day_pnl = round(dr + (open_pnl or 0), 2)
    state = {"ok": True, "equity": equity, "open_pnl": open_pnl, "cash": cash,
             "day_realized": dr, "day_pnl": day_pnl, "flat": False,
             "session_anchor": anchor, "high_water_mark": cfg.get("high_water_mark"),
             "intraday_peak": intraday_peak_for(a, cfg, equity)}
    # Keep the shared cache warm so the order gate + dashboard see live numbers.
    prev = ACCOUNT_STATE_CACHE.get(a["id"], ({},))[0]
    merged = {**prev, **state}
    ACCOUNT_STATE_CACHE[a["id"]] = (merged, time.time())
    if anchor:
        update_account_hwm(a, cfg, merged)
        cfg = get_risk_config(a["id"])
    return _breach_and_lock(a, cfg, merged)


# Adaptive watchdog cadence.
FAST_INTERVAL = 1          # seconds — hot loop tick
FULL_REFRESH_TICKS = 10    # full state (fills/positions) every N ticks (~10s)
_WATCH_TICK = 0


WATCH_WORKERS = 16  # bounded concurrency for the per-account poll (rate-limit safe)


def _poll_one(a: dict, tick: int) -> list:
    """Risk/heartbeat work for ONE account in a tick. Returns lock/pass events."""
    out = []
    try:
        cfg = _maybe_auto_unlock(ensure_risk_config(a["id"], a["user_id"]))
        active = _risk_active(cfg)
        hard = _has_hard_limits(cfg)
        fast = _ACCT_FAST.get(a["id"], {})
        in_pos = fast.get("in_position", True)  # assume in-position until first full refresh
        need_full = active and ((tick % FULL_REFRESH_TICKS == 0) or a["id"] not in _ACCT_FAST)

        if need_full:
            state = account_live_state(a, cfg)
            set_heartbeat(a["id"], bool(state.get("ok")))
            if state.get("ok"):
                # Intraday give-back peak + all-time profit for the new rules.
                state["intraday_peak"] = intraday_peak_for(a, cfg, state.get("equity"))
                state["total_profit"] = account_total_profit(a["id"])
                update_account_hwm(a, cfg, state)
                cfg = get_risk_config(a["id"])
                ev_pass = check_eval_pass(a, cfg, state)  # eval -> congrats/funded unlock
                if ev_pass:
                    out.append(ev_pass)
                    cfg = get_risk_config(a["id"])
                ev = _breach_and_lock(a, cfg, state)
                if ev:
                    out.append(ev)
                record_daily_equity(a, state)  # persist daily PnL history
        elif active and hard and in_pos:
            ev = fast_breach_check(a, cfg)
            if ev:
                out.append(ev)
            record_daily_equity(a, ACCOUNT_STATE_CACHE.get(a["id"], ({},))[0])
        elif tick % FULL_REFRESH_TICKS == 0:
            # Even with no risk rules, snapshot equity so the journal/reports work.
            token = ensure_fresh_token(a)
            snap = tvo.get_cash_snapshot(a.get("env") or "live", token, a.get("account_id")) if token else None
            set_heartbeat(a["id"], snap is not None)
            if snap is not None:
                st = _snapshot_to_state(snap)
                ACCOUNT_STATE_CACHE[a["id"]] = ({**ACCOUNT_STATE_CACHE.get(a["id"], ({},))[0], **st}, time.time())
                record_daily_equity(a, st)
    except Exception:
        pass
    return out


def poll_tick():
    """One watchdog tick — runs all connected accounts in PARALLEL (bounded) so a
    single worker handles 1,000+ accounts without serializing broker calls."""
    global _WATCH_TICK
    _WATCH_TICK += 1
    tick = _WATCH_TICK
    con = db()
    rows = con.execute("SELECT * FROM broker_accounts WHERE status='connected'").fetchall()
    con.close()
    accts = [dict(r) for r in rows]
    if not accts:
        return []
    events = []
    with ThreadPoolExecutor(max_workers=min(WATCH_WORKERS, len(accts))) as pool:
        for evs in pool.map(lambda a: _poll_one(a, tick), accts):
            events += evs
    return events


# ---- single-worker leader election (so 2+ instances don't double-poll) ----
_LEADER_CONN = None
_LEADER_LOCK_KEY = 778201


def try_become_leader() -> bool:
    """Only ONE instance should run the background loops. On SQLite (single
    instance) always True. On Postgres, grab a session-level advisory lock — the
    instance that holds it is the worker; if it dies the lock frees and another
    instance takes over automatically."""
    global _LEADER_CONN
    if not dbmod.IS_PG:
        return True
    if _LEADER_CONN is not None:
        return True
    try:
        import psycopg
        conn = psycopg.connect(dbmod.DATABASE_URL, autocommit=True)
        got = conn.execute("SELECT pg_try_advisory_lock(%s)", (_LEADER_LOCK_KEY,)).fetchone()[0]
        if got:
            _LEADER_CONN = conn  # hold for the process lifetime
            print("RISK WORKER: acquired leader lock")
            return True
        conn.close()
        return False
    except Exception as e:
        print("LEADER LOCK ERROR:", e)
        return False


async def risk_watchdog_loop():
    print("RISK WATCHDOG started — fast breach loop @ %ss, full refresh @ %ss"
          % (FAST_INTERVAL, FAST_INTERVAL * FULL_REFRESH_TICKS))
    loop = asyncio.get_event_loop()
    while True:
        if not await loop.run_in_executor(None, try_become_leader):
            await asyncio.sleep(15)  # not the worker instance — re-check for failover
            continue
        try:
            events = await loop.run_in_executor(None, poll_tick)
            for ev in events or []:
                try:
                    await manager.broadcast(ev)
                except Exception:
                    pass
        except Exception as e:
            print("WATCHDOG ERROR:", e)
        await asyncio.sleep(FAST_INTERVAL)


# ============================================================
# PERFORMANCE EMAIL DIGESTS (daily / weekly / monthly)
# ============================================================

def _snapshot_to_state(snap):
    """Equity + penny-exact day PnL from a raw cash snapshot (mirrors the risk
    loop), so accounts WITHOUT risk rules still get their daily PnL recorded."""
    if snap is None:
        return {"ok": False}
    open_pnl = _snapshot_value(snap, ("openPnL", "openPnl", "unrealizedPnL"))
    net_liq = _snapshot_value(snap, ("netLiq", "netLiquidatingValue"))
    sod = _snapshot_value(snap, ("netLiqSOD", "totalCashValueSOD", "cashSODUSD"))
    cash = _snapshot_value(snap, ("totalCashValue", "totalCashBalance", "cashBalance", "amount"))
    equity = net_liq if net_liq is not None else (round(cash + (open_pnl or 0), 2) if cash is not None else None)
    day_pnl = round(equity - sod, 2) if (equity is not None and sod is not None) else None
    return {"ok": True, "equity": equity, "open_pnl": open_pnl, "day_pnl": day_pnl}


_LAST_EQUITY_SNAP: Dict[Any, tuple] = {}   # account_id -> (trade_date, last_write_ts)
_EQUITY_SNAP_MIN_INTERVAL = 120            # throttle DB writes to once / 2 min per account


def record_daily_equity(account: dict, state: dict):
    """Persist today's equity + day-PnL for one account (throttled). This is the
    permanent record the journal, calendar, equity curve, and email reports read
    from — independent of Tradovate's limited fill history."""
    try:
        if not state or not state.get("ok"):
            return
        day_pnl = state.get("day_pnl")
        net_liq = state.get("equity")
        if day_pnl is None and net_liq is None:
            return
        now_utc = datetime.now(timezone.utc)
        trade_date = now_utc.astimezone(ZoneInfo(_ET)).strftime("%Y-%m-%d")
        aid = account.get("id")
        prev = _LAST_EQUITY_SNAP.get(aid)
        nowts = time.time()
        # Always write when the day rolls over; otherwise throttle.
        if prev and prev[0] == trade_date and (nowts - prev[1]) < _EQUITY_SNAP_MIN_INTERVAL:
            return
        net_liq_sod = round(net_liq - day_pnl, 2) if (net_liq is not None and day_pnl is not None) else None
        con = db()
        con.execute(
            "INSERT INTO daily_equity(user_id,account_id,account_name,trade_date,net_liq,net_liq_sod,day_pnl,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id,trade_date) DO UPDATE SET "
            "net_liq=excluded.net_liq, net_liq_sod=excluded.net_liq_sod, "
            "day_pnl=excluded.day_pnl, updated_at=excluded.updated_at",
            (account.get("user_id"), aid, account.get("account_name"), trade_date,
             net_liq, net_liq_sod, day_pnl, now_utc.isoformat()),
        )
        con.commit()
        con.close()
        _LAST_EQUITY_SNAP[aid] = (trade_date, nowts)
    except Exception as e:
        print(f"record_daily_equity error acct={account.get('id')}: {e}")


def daily_pnl_map(user_id: int, start_date: str = None, end_date: str = None, only_account_id=None) -> dict:
    """{ 'YYYY-MM-DD': summed day_pnl across the user's accounts } from persisted
    snapshots. Dates are ET 'YYYY-MM-DD' strings, inclusive."""
    q = "SELECT trade_date, SUM(day_pnl) AS pnl FROM daily_equity WHERE user_id=?"
    params = [user_id]
    if only_account_id not in (None, "", "all"):
        q += " AND account_id=?"
        params.append(only_account_id)
    if start_date:
        q += " AND trade_date>=?"
        params.append(start_date)
    if end_date:
        q += " AND trade_date<=?"
        params.append(end_date)
    q += " GROUP BY trade_date"
    con = db()
    rows = con.execute(q, tuple(params)).fetchall()
    con.close()
    return {r["trade_date"]: (r["pnl"] or 0) for r in rows}


def ledger_daily_map(user_id: int, start_date: str = None, end_date: str = None, only_account_id=None) -> dict:
    """{ 'YYYY-MM-DD'(ET): summed realized P&L } from the permanent trade_log ledger
    for ONE user — the authoritative realized-P&L source (same data the public
    results page uses), so the journal calendar matches the trade history instead of
    relying on the unreliable daily_equity equity snapshots. ET dates, inclusive."""
    q = ("SELECT account_id, symbol, side, qty, entry_price, exit_price, pnl, closed_at "
         "FROM trade_log WHERE user_id=?")
    params = [user_id]
    if only_account_id not in (None, "", "all"):
        try:
            q += " AND account_id=?"
            params.append(int(only_account_id))
        except (TypeError, ValueError):
            pass
    con = db()
    rows = con.execute(q, tuple(params)).fetchall()
    con.close()
    out, seen = {}, set()
    for r in rows:
        ident = (r["account_id"], r["symbol"], r["side"], r["qty"],
                 r["entry_price"], r["exit_price"], str(r["closed_at"]))
        if ident in seen:            # fold away any legacy duplicate rows
            continue
        seen.add(ident)
        day = _et_day(r["closed_at"])
        if not day:
            continue
        if (start_date and day < start_date) or (end_date and day > end_date):
            continue
        out[day] = round(out.get(day, 0.0) + float(r["pnl"] or 0), 2)
    return out


def period_pnl_stats(user_id: int, start_date: str, end_date: str, only_account_id=None) -> dict:
    """Net + per-day stats over a date range, from persisted daily PnL."""
    m = daily_pnl_map(user_id, start_date, end_date, only_account_id)
    days = sorted(m.items())
    net = round(sum(v for _, v in days), 2)
    green = sum(1 for _, v in days if v > 0)
    red = sum(1 for _, v in days if v < 0)
    best = max(days, key=lambda x: x[1]) if days else None
    worst = min(days, key=lambda x: x[1]) if days else None
    return {"net": net, "green_days": green, "red_days": red,
            "best_day": best, "worst_day": worst, "active_days": len(days)}


_ACCT_INTRADAY_PEAK: Dict[Any, tuple] = {}   # account_id -> (session_anchor_iso, peak_equity)


def intraday_peak_for(account: dict, cfg: dict, equity, now_utc=None):
    """Highest live equity since this session's start (resets each session), for
    the intraday give-back limit. Kept in memory — resets daily anyway."""
    aid = account.get("id")
    if equity is None:
        prev = _ACCT_INTRADAY_PEAK.get(aid)
        return prev[1] if prev else None
    now_utc = now_utc or datetime.now(timezone.utc)
    anchor = risk.session_anchor(now_utc, int(cfg.get("reset_hour") or 17),
                                 cfg.get("tz") or "America/New_York").isoformat()
    prev = _ACCT_INTRADAY_PEAK.get(aid)
    peak = max(prev[1], equity) if (prev and prev[0] == anchor) else equity
    _ACCT_INTRADAY_PEAK[aid] = (anchor, peak)
    return peak


def account_total_profit(account_id) -> float:
    """All-time realized profit for one account, from persisted daily PnL — the
    denominator for the consistency rule."""
    con = db()
    row = con.execute("SELECT SUM(day_pnl) AS t FROM daily_equity WHERE account_id=?", (account_id,)).fetchone()
    con.close()
    return round((row["t"] if row and row["t"] is not None else 0) or 0, 2)


def apply_persisted_pnl(s: dict, user_id: int, start_date: str, end_date: str, only_account_id=None) -> dict:
    """Override a journal_analytics() result's daily PnL, net, day stats, and equity
    curve with the permanent trade ledger — REALIZED closed round-trips only, the
    exact same source as the trade list and /results. Trip-level stats (win rate,
    profit factor, per-trade) are left as-is.

    Deliberately does NOT use the daily_equity snapshot table: those snapshots record
    netLiq day P&L (realized + UNREALIZED open positions), which would put a phantom
    value on the calendar for a day that has an open position but no closed trade
    (e.g. an open short shows '+$307' while the trade list shows nothing). Ledger-only
    keeps the calendar perfectly consistent with the trades."""
    ledger = ledger_daily_map(user_id, start_date, end_date, only_account_id)   # authoritative trade_log
    if not ledger:
        return s
    # LIVE trips (freshest broker record) win for any day they cover; the ledger
    # backfills days whose fills have aged out of Tradovate's window. Never summed.
    trip_daily = s.get("daily", {})
    daily = {**ledger, **trip_daily}
    vals = list(daily.values())
    s["daily"] = daily
    s["net"] = round(sum(vals), 2)
    s["green_days"] = sum(1 for v in vals if v > 0)
    s["red_days"] = sum(1 for v in vals if v < 0)
    s["best_day"] = max(daily.items(), key=lambda x: x[1]) if daily else None
    s["worst_day"] = min(daily.items(), key=lambda x: x[1]) if daily else None
    gr = s["green_days"] + s["red_days"]
    s["day_win_rate"] = round(s["green_days"] / gr * 100, 1) if gr else 0.0
    s["avg_daily"] = round(sum(vals) / len(vals), 2) if vals else 0.0
    # Equity curve = cumulative daily PnL, chronological (days/months/years).
    eq, run = [], 0.0
    for d in sorted(daily.keys()):
        run += daily[d]
        eq.append(round(run, 2))
    s["equity"] = eq
    return s


def _trips_in_range(trips, start_utc, end_utc):
    out = []
    for t in trips:
        d = _trip_dt(t, "closed_at")
        if d and start_utc <= d <= end_utc:
            out.append(t)
    return out


def digest_email_html(period_label: str, s: dict) -> str:
    """Compact branded stats grid for a performance digest email."""
    pf = s["profit_factor"]
    pf_disp = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
    best = (f"{s['best_day'][0]} ({_money(s['best_day'][1],0)})" if s["best_day"] else "—")
    worst = (f"{s['worst_day'][0]} ({_money(s['worst_day'][1],0)})" if s["worst_day"] else "—")
    net_color = "#0f8f45" if s["net"] >= 0 else "#b91c1c"

    def row(label, value, color="#111827"):
        return (f"<tr><td style='padding:9px 0;color:#6b7280;font-size:14px;'>{label}</td>"
                f"<td style='padding:9px 0;text-align:right;font-weight:800;color:{color};font-size:14px;'>{value}</td></tr>")

    return (
        f"<p style='margin:0 0 14px;'>Here's your <b>{period_label}</b> trading performance from KhomaAPI — straight from your broker fills.</p>"
        f"<div style='background:{net_color};border-radius:14px;padding:18px 20px;color:#fff;margin:0 0 16px;'>"
        f"<div style='font-size:13px;opacity:.85;'>Net P&amp;L</div>"
        f"<div style='font-size:30px;font-weight:850;'>{_money(s['net'])}</div></div>"
        f"<table style='width:100%;border-collapse:collapse;'>"
        + row("Trades", s["n"])
        + row("Win rate", f"{s['win_rate']}%  ({s['wins']}W / {s['losses']}L)")
        + row("Profit factor", pf_disp)
        + row("Expectancy / trade", _money(s["expectancy"]), "#0f8f45" if s["expectancy"] >= 0 else "#b91c1c")
        + row("Avg win / loss", f"{_money(s['avg_win'],0)} / {_money(-s['avg_loss'],0)}")
        + row("Best day", best, "#0f8f45")
        + row("Worst day", worst, "#b91c1c")
        + row("Green / red days", f"{s['green_days']} / {s['red_days']}")
        + "</table>"
    )


def _send_one_digest(user_row: dict, period_label: str, subject: str, trips: list,
                     start_date: str = None, end_date: str = None) -> bool:
    s = journal_analytics(trips or [])
    # Live broker fills are authoritative; persisted daily P&L only fills OLDER
    # days Tradovate no longer returns (never overrides real, recent P&L).
    if start_date:
        s = apply_persisted_pnl(s, user_row["id"], start_date, end_date)
    html_body = digest_email_html(period_label, s)
    text = f"{period_label} performance — Net {_money(s['net'])}, win rate {s['win_rate']}%, {s['n']} trades."
    return send_branded_email(
        user_row["email"], subject, f"Your {period_label} performance",
        html_body, button_label="Open KhomaTradingJournal", button_url=f"{APP_URL}/journal",
        text_fallback=text,
    )


def _mark_digest_sent(user_id: int, col: str, period_id: str):
    con = db()
    con.execute(f"UPDATE users SET {col}=? WHERE id=?", (period_id, user_id))
    con.commit()
    con.close()


def digest_tick():
    """Check every enabled user and send any due daily/weekly/monthly digest."""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo(_ET))
    con = db()
    users = con.execute(
        "SELECT * FROM users WHERE digest_daily=1 OR digest_weekly=1 OR digest_monthly=1"
    ).fetchall()
    con.close()
    for u in users:
        u = dict(u)
        if not u.get("email"):
            continue
        # Plan gate: digests are a Pro/Elite/Founder feature and require an
        # active subscription. No-op when enforcement is off.
        if ENFORCE_SUBSCRIPTIONS and not user_entitlements(u).has(ent.EMAIL_DIGESTS):
            continue
        try:
            trips, _o = account_trade_history(u["id"])
        except Exception:
            trips = []

        # DAILY — after the futures session close (>= 17:00 ET), once per day.
        if u.get("digest_daily") and now_et.hour >= 17:
            pid = now_et.strftime("%Y-%m-%d")
            if u.get("digest_daily_sent") != pid:
                start = risk.session_anchor(now_utc)
                if _send_one_digest(u, "daily", "Your KhomaAPI daily performance",
                                    _trips_in_range(trips, start, now_utc), start_date=pid, end_date=pid):
                    _mark_digest_sent(u["id"], "digest_daily_sent", pid)

        # WEEKLY — Saturday morning, covering the last 7 days.
        if u.get("digest_weekly") and now_et.weekday() == 5 and now_et.hour >= 9:
            pid = now_et.strftime("%G-W%V")
            if u.get("digest_weekly_sent") != pid:
                start = now_utc - timedelta(days=7)
                d0 = (now_et - timedelta(days=7)).strftime("%Y-%m-%d")
                if _send_one_digest(u, "weekly", "Your KhomaAPI weekly performance",
                                    _trips_in_range(trips, start, now_utc), start_date=d0, end_date=now_et.strftime("%Y-%m-%d")):
                    _mark_digest_sent(u["id"], "digest_weekly_sent", pid)

        # MONTHLY — on the 1st, covering the previous calendar month.
        if u.get("digest_monthly") and now_et.day == 1 and now_et.hour >= 9:
            first_this = now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            last_prev = first_this - timedelta(days=1)
            first_prev = last_prev.replace(day=1)
            pid = first_prev.strftime("%Y-%m")
            if u.get("digest_monthly_sent") != pid:
                start = first_prev.astimezone(timezone.utc)
                end = first_this.astimezone(timezone.utc)
                if _send_one_digest(u, first_prev.strftime("%B %Y"), f"Your KhomaAPI {first_prev.strftime('%B')} performance",
                                    _trips_in_range(trips, start, end),
                                    start_date=first_prev.strftime("%Y-%m-%d"), end_date=last_prev.strftime("%Y-%m-%d")):
                    _mark_digest_sent(u["id"], "digest_monthly_sent", pid)


def _prewarm_calendar():
    """Keep the economic-calendar cache warm + current so it stays up to date
    automatically (incl. the weekly roll-over) without waiting for a page view."""
    try:
        fetch_calendar_events("this", datetime.now(timezone.utc))
    except Exception:
        pass


def sync_recurring_news_locks():
    """Materialize auto news_windows from each user's recurring rules + the live
    calendar, so 'never trade CPI' blocks every CPI automatically. Idempotent:
    rebuilds future auto windows each run; manual one-click blocks are untouched."""
    try:
        now = datetime.now(timezone.utc)
        events, _multi = fetch_calendar_events("this", now)
        con = db()
        rules = [dict(r) for r in con.execute("SELECT * FROM news_rules WHERE enabled=1").fetchall()]
        con.close()
        if not rules:
            return
        con = db()
        # Rebuild: drop future auto windows, then re-create from current rules.
        con.execute("DELETE FROM news_windows WHERE source='auto' AND ends_at>=?", (now.isoformat(),))
        made = 0
        for r in rules:
            kw = (r.get("keyword") or "").lower().strip()
            if not kw:
                continue
            before = int(r.get("minutes_before") or 15)
            after = int(r.get("minutes_after") or 15)
            for e in (events or []):
                dt = e.get("dt")
                if not dt or kw not in (e.get("title") or "").lower():
                    continue
                end = dt + timedelta(minutes=after)
                if end < now:
                    continue  # already over
                con.execute(
                    "INSERT INTO news_windows(user_id,account_id,starts_at,ends_at,label,source,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (r["user_id"], r.get("account_id"), (dt - timedelta(minutes=before)).isoformat(),
                     end.isoformat(), f"Auto: {e.get('title')}", "auto", now.isoformat()))
                made += 1
        con.commit()
        con.close()
        if made:
            print(f"news rules: materialized {made} auto lockout window(s)")
    except Exception as ex:
        print("news rule sync error:", ex)


def whop_reverify_tick():
    """Safety net: re-check linked ACTIVE users against the Whop API and revoke
    any whose membership is no longer valid. This makes revocation independent of
    the webhook firing — a missed cancel/refund webhook is still caught here
    within one cycle. Comp accounts (manual_plan, no whop_membership_id) are
    untouched."""
    if not WHOP_API_KEY:
        return
    con = db()
    rows = con.execute(
        "SELECT id, whop_membership_id FROM users "
        "WHERE whop_membership_id IS NOT NULL AND whop_membership_id <> '' "
        "AND subscription_status = 'active'"
    ).fetchall()
    con.close()
    for r in rows:
        try:
            m = whopmod.fetch_membership(r["whop_membership_id"], WHOP_API_KEY)
            if m is whopmod.GONE or m == whopmod.GONE:
                # Membership deleted in Whop → revoke.
                con = db()
                con.execute("UPDATE users SET subscription_status='revoked' WHERE id=?", (r["id"],))
                con.commit()
                con.close()
                continue
            if not m:
                continue  # transient API error → leave as-is, retry next cycle
            st = whopmod.membership_state(m)
            con = db()
            con.execute(
                "UPDATE users SET subscription_status=?, whop_plan_id=?, current_period_end=? WHERE id=?",
                (st["status"], st["plan_id"], st["period_end"], r["id"]),
            )
            con.commit()
            con.close()
        except Exception as e:
            print(f"whop reverify error uid={r['id']}: {e}")


async def digest_loop():
    print("DIGEST scheduler started (10-min cadence)")
    loop = asyncio.get_event_loop()
    while True:
        try:
            if await loop.run_in_executor(None, try_become_leader):  # worker instance only
                await loop.run_in_executor(None, digest_tick)
                await loop.run_in_executor(None, whop_reverify_tick)
                await loop.run_in_executor(None, _prewarm_calendar)
                await loop.run_in_executor(None, sync_recurring_news_locks)
                await loop.run_in_executor(None, persist_track_trades)
        except Exception as e:
            print("DIGEST ERROR:", e)
        await asyncio.sleep(600)


async def trade_ledger_loop():
    """Capture EVERY connected client's closed round-trips into the permanent ledger
    on a tight cadence, so realized P&L is recorded while broker tokens are valid and
    fills are fresh — and stays visible on every client's dashboard/journal after the
    fills age out. Leader-only; resilient per user."""
    print("TRADE-LEDGER scheduler started (2-min cadence)")
    loop = asyncio.get_event_loop()
    while True:
        try:
            if await loop.run_in_executor(None, try_become_leader):
                await loop.run_in_executor(None, persist_all_account_trades)
        except Exception as e:
            print("TRADE-LEDGER ERROR:", e)
        await asyncio.sleep(120)


@app.on_event("startup")
async def _start_watchdog():
    # Disable with KHOMA_DISABLE_WATCHDOG=1 (e.g. in tests / CI).
    if os.getenv("KHOMA_DISABLE_WATCHDOG") == "1":
        return
    asyncio.create_task(risk_watchdog_loop())
    asyncio.create_task(digest_loop())
    asyncio.create_task(trade_ledger_loop())


def _order_ok(resp) -> bool:
    return (
        isinstance(resp, dict)
        and resp.get("orderId") is not None
        and not resp.get("failureReason")
        and not resp.get("failureText")
        and not resp.get("error")
    )


def _broker_error_text(resp) -> str:
    """Human-readable reason an order was not placed. Detects auth failures so
    the user is told to reconnect."""
    if not isinstance(resp, dict):
        return "No broker response."
    for k in ("failureText", "failureReason", "errorText", "error", "message"):
        v = resp.get(k)
        if v:
            text = str(v)
            low = text.lower()
            if "auth" in low or "token" in low or "unauthor" in low or "401" in low or "access" in low:
                return f"Tradovate authorization expired — reconnect on the Broker page. ({text})"
            return text
    return "Order rejected by Tradovate."


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def ensure_fresh_token(account: dict) -> str:
    """Return a valid decrypted access token for the account, renewing it with
    Tradovate when it is near expiry and persisting the new token. Returns "" if
    there is no token. A renewal failure (e.g. already expired) returns the
    existing token so the caller surfaces a clear reconnect error."""
    token = dec(account["access_token_enc"]) if account.get("access_token_enc") else ""
    if not token:
        return ""
    env = account.get("env") or "live"

    exp_dt = _parse_iso(account.get("token_expires_at") or "")
    if exp_dt is not None:
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        # Not near expiry yet -> reuse as-is.
        if exp_dt - datetime.now(timezone.utc) > timedelta(minutes=10):
            return token

    res = tvo.renew_access_token(env, token)
    if not res.get("ok"):
        return token  # renewal failed; caller will get an auth error -> reconnect

    new_token = res["access_token"]
    new_exp = res.get("expiration") or ""
    con = db()
    con.execute(
        "UPDATE broker_accounts SET access_token_enc=?, token_expires_at=?, updated_at=? WHERE id=?",
        (enc(new_token), new_exp, datetime.now(timezone.utc).isoformat(), account.get("id")),
    )
    con.commit()
    con.close()
    # Keep the in-memory dict consistent for the rest of this request.
    account["access_token_enc"] = enc(new_token)
    account["token_expires_at"] = new_exp
    return new_token


def place_order_on_account(account: dict, action: str, symbol: str, qty: int) -> dict:
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    if not token or not account.get("account_id"):
        return {"account": account.get("account_name"), "ok": False, "error": "Reconnect required"}
    resp = tvo.place_order(env, token, account.get("account_name"), account.get("account_id"), action, symbol, qty)
    ok = _order_ok(resp)
    out = {"account": account.get("account_name"), "ok": ok, "response": resp}
    if not ok:
        out["error"] = _broker_error_text(resp)
    return out


def flatten_on_account(account: dict, symbol: str) -> list:
    """Close any open position matching `symbol` on one account."""
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return [{"account": account.get("account_name"), "ok": False, "error": "Reconnect required"}]

    root = symbol_root(str(symbol).upper()) if symbol else ""   # no symbol -> flatten ALL
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


def reverse_on_account(account: dict, symbol: str, qty: int) -> dict:
    """Flip the current position on one account to the opposite side in a single
    order: a long becomes short (and vice-versa) with `qty` contracts. If the
    account is flat there is nothing to reverse, so this is a no-op."""
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return {"account": account.get("account_name"), "ok": False, "error": "Reconnect required"}

    root = symbol_root(str(symbol).upper())
    for p in (tvo.get_positions(env, token) or []):
        if str(p.get("accountId")) != str(acct_id):
            continue
        net = int(p.get("netPos") or 0)
        if net == 0:
            continue
        cname = ((tvo.get_contract(env, token, p.get("contractId")) or {}).get("name") or "").upper()
        if root and symbol_root(cname) != root:
            continue
        # Close the existing position AND open the same qty the opposite way.
        action = "Sell" if net > 0 else "Buy"
        order_qty = abs(net) + max(1, int(qty))
        resp = tvo.place_order(env, token, account.get("account_name"), acct_id, action, cname or symbol, order_qty)
        return {"account": account.get("account_name"), "ok": _order_ok(resp), "response": resp}

    return {"account": account.get("account_name"), "ok": False, "skipped": True,
            "error": "No open position to reverse."}


# ============================================================
# EXCHANGE-SIDE OCO BRACKETS (real resting stop + take-profit)
# ============================================================

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tick_for(symbol) -> float:
    return _TICK_SIZE.get(symbol_root(str(symbol).upper()), 0.25)


def _round_tick(price, tick: float):
    """Snap a price to the nearest valid exchange tick (or None)."""
    p = _num(price)
    if p is None:
        return None
    if not tick or tick <= 0:
        return round(p, 6)
    return round(round(p / tick) * tick, 6)


def _cancel_ok(resp) -> bool:
    """A cancel actually succeeded (not a swallowed transport failure or a 200-with-
    failure-body broker reject). None -> failed; dict with a failure field -> failed."""
    if not isinstance(resp, dict):
        return False
    return not (resp.get("failureReason") or resp.get("failureText") or resp.get("error"))


_CONTRACT_NAME_CACHE: dict = {}   # contract_id -> name (immutable per id; safe to cache forever)


def _contract_name(env, token, contract_id) -> str:
    """Safe contract-name lookup — never raises even if the broker returns a non-dict
    (a bare string / array), which would blow up a naive `(get_contract(...) or {}).get`.
    Caches by contract_id and retries once: a BLANK name is a symbol-scoping HAZARD — the
    guards below fail closed on an empty name, so a transient 429/timeout must not blank it
    (that was the bug where a rate-limited lookup misfired an exit/move_stop onto the wrong
    contract)."""
    if contract_id is None:
        return ""
    cached = _CONTRACT_NAME_CACHE.get(contract_id)
    if cached:
        return cached
    for _ in range(2):                     # one retry — don't let a transient blip blank the name
        try:
            c = tvo.get_contract(env, token, contract_id)
            if isinstance(c, dict):
                name = str(c.get("name") or "").upper()
                if name:
                    _CONTRACT_NAME_CACHE[contract_id] = name
                    return name
        except Exception:
            pass
    return ""


def _net_position_for(account, resolved_symbol) -> int:
    """Signed net position for a contract root on one account (0 if flat / unknown)."""
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return 0
    root = symbol_root(str(resolved_symbol).upper()) if resolved_symbol else ""
    net = 0
    for p in (tvo.get_positions(env, token) or []):
        if str(p.get("accountId")) != str(acct_id):
            continue
        n = int(p.get("netPos") or 0)
        if n == 0:
            continue
        cname = _contract_name(env, token, p.get("contractId"))
        if root and cname and symbol_root(cname) != root:
            continue
        net += n
    return net


def _resting_stop_count(account, resolved_symbol) -> int:
    """How many WORKING stop orders currently rest for this contract on the account."""
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return 0
    root = symbol_root(str(resolved_symbol).upper()) if resolved_symbol else ""
    orders = tvo.get_orders(env, token)
    versions = tvo.get_order_versions(env, token)
    n = 0
    for o in tvo.working_orders_for(orders, versions, acct_id, order_type="Stop"):
        cname = _contract_name(env, token, o.get("contractId"))
        if not root or (cname and symbol_root(cname) == root):
            n += 1
    return n


# Retry window while the placeOSO bracket legs register at Tradovate before we decide a
# stop is genuinely missing (env-tunable so a slow broker doesn't cause a false flatten).
_STOP_CONFIRM_TRIES = int(os.getenv("BRACKET_CONFIRM_TRIES", "5"))
_STOP_CONFIRM_DELAY = float(os.getenv("BRACKET_CONFIRM_DELAY", "0.5"))


def _ensure_protected_or_flatten(account, resolved_symbol, expected_stops: int):
    """CRITICAL prop-safety guard. Tradovate's placeOSO is NON-ATOMIC: the market entry
    can fill while a stop bracket leg is rejected (e.g. price ticks through a tight stop
    at the instant of the fill), leaving a funded position with no resting stop while the
    order response still looks 'ok'. After placing the bracket(s), confirm that the
    expected number of protective stops actually rest for the contract. If an open
    position remains under-protected, FLATTEN it (cancel working orders + close) so the
    account is never left naked or partially-naked. Returns a dict to merge into the leg
    results when it had to intervene, else None."""
    if expected_stops <= 0:
        return None
    for i in range(max(1, _STOP_CONFIRM_TRIES)):
        if _net_position_for(account, resolved_symbol) == 0:
            return None                                   # already flat -> nothing to protect
        if _resting_stop_count(account, resolved_symbol) >= expected_stops:
            return None                                   # fully protected
        if i < _STOP_CONFIRM_TRIES - 1:
            time.sleep(_STOP_CONFIRM_DELAY)
    if _net_position_for(account, resolved_symbol) == 0:
        return None
    # Open position with too few resting stops -> never hold it unprotected. Cancel any
    # resting legs, then flatten race-safely (server-side liquidation).
    try:
        cancel_working_orders_for(account, resolved_symbol)
    except Exception:
        pass
    try:
        _liquidate_positions_for(account, resolved_symbol)
    except Exception:
        pass
    return {"ok": False, "unprotected_flattened": True,
            "error": "Protective stop did not rest at the exchange (placeOSO partial reject) — "
                     "position flattened for safety"}


def _bracket_side_ok(side: str, stop, limit) -> bool:
    """Reject a wrong-side protective bracket so we NEVER place a stop that would
    trigger the instant it rests. buy: stop below the target; sell: stop above it."""
    s = str(side).lower()
    st, lm = _num(stop), _num(limit)
    if st is not None and lm is not None:
        if s == "buy" and not (st < lm):
            return False
        if s == "sell" and not (st > lm):
            return False
    return True


def entry_legs_from_alert(side: str, qty: int, extras: dict):
    """Bracket legs (qty, stop, limit) from a TradingView entry alert:
      scale-out (qtyScale+qtyRunner+tp1+tp2) -> TWO brackets (scale=tp1, runner=tp2);
      simple (sl+tp)                          -> ONE bracket;
      sl only                                 -> a stop-only protective order;
      no sl                                   -> a single plain market leg (unchanged)."""
    sl = _num(extras.get("sl"))
    tp = _num(extras.get("tp"))
    tp1 = _num(extras.get("tp1"))
    tp2 = _num(extras.get("tp2"))
    qscale = extras.get("qtyScale")
    qrunner = extras.get("qtyRunner")
    try:
        qscale = int(qscale) if qscale is not None else None
        qrunner = int(qrunner) if qrunner is not None else None
    except (TypeError, ValueError):
        qscale = qrunner = None

    if sl is not None and tp1 is not None and tp2 is not None and qscale and qrunner:
        return [(qscale, sl, tp1), (qrunner, sl, tp2)]      # scale-out: two OCO brackets
    tgt = tp if tp is not None else tp1
    if sl is not None:
        return [(int(qty), sl, tgt)]                        # one bracket (or stop-only if tgt None)
    return [(int(qty), None, None)]                         # plain market entry


def execute_bracket_to_accounts(accounts: list, symbol: str, side: str, legs: list) -> dict:
    """Place resting OCO bracket(s) on every account in parallel (same instant, same
    price). `legs` = [(qty, stop, limit), ...]. Risk-gated per account; off-tick prices
    are snapped; a wrong-side bracket is refused rather than placed unprotected."""
    accounts = list(accounts or [])
    if not accounts:
        return {"results": [], "placed": 0, "total": 0, "accounts": 0}

    for a in accounts:                       # refresh tokens up front (serialized)
        try:
            ensure_fresh_token(a)
        except Exception:
            pass

    resolved = symbol                        # resolve the contract once
    for a in accounts:
        tok = dec(a["access_token_enc"]) if a.get("access_token_enc") else ""
        if tok:
            try:
                resolved = tvo.resolve_contract(a.get("env") or "live", tok, symbol)
                break
            except Exception:
                pass
    tick = _tick_for(resolved)

    def run(a):
        try:
            token = ensure_fresh_token(a)
            env = a.get("env") or "live"
            acct_id = a.get("account_id")
            if not token or not acct_id:
                return [{"account": a.get("account_name"), "ok": False, "error": "Reconnect required"}]
            # Gate the WHOLE entry ONCE against the TOTAL intended qty. Per-leg gating
            # read a stale cached net position, so a 2-leg scale-out could let the
            # account exceed max_position (both legs saw net=0 and both passed).
            total_qty = sum(max(1, int(q)) for (q, _s, _l) in legs)
            allowed, reason, breach = risk_gate(a, side, total_qty, resolved)
            if not allowed:
                return [{"account": a.get("account_name"), "ok": False,
                         "error": "Risk: " + reason, "risk_blocked": True, "breach": breach}]
            # Fresh-entry reset: a bracket entry opens ONE new position. If a prior one
            # is still open (its bracket hasn't filled yet, or a re-entry raced the exit
            # alert), flatten it + cancel its stale orders FIRST so the new bracket can
            # never stack on a leftover. No-op when already flat (the common case: one
            # get_positions check).
            try:
                if _net_position_for(a, resolved) != 0:
                    cancel_working_orders_for(a, resolved)
                    flatten_on_account(a, resolved)
            except Exception as e:
                print(f"execute_bracket_to_accounts: pre-entry reset failed (acct {a.get('id')}): {e}")
            out = []
            expected_stops = 0
            for (lqty, stop, limit) in legs:
                lqty = max(1, int(lqty))
                st = _round_tick(stop, tick)
                lm = _round_tick(limit, tick)
                if not _bracket_side_ok(side, st, lm):
                    # Bad data -> refuse (never leave a naked prop position on a wrong-side stop).
                    out.append({"account": a.get("account_name"), "ok": False,
                                "error": f"Bracket refused: wrong-side stop/target (stop={st}, tp={lm})"})
                    continue
                resp = tvo.place_bracket_order(env, token, a.get("account_name"), acct_id,
                                               side, resolved, lqty, st, lm)
                ok = _order_ok(resp)
                if ok and st is not None:
                    expected_stops += 1        # we expect a real resting stop for this leg
                out.append({"account": a.get("account_name"), "ok": ok,
                            "response": resp, "stop": st, "limit": lm, "qty": lqty})
            # CRITICAL: placeOSO is non-atomic — verify the protective stops actually rest;
            # if a filled position is left under-protected, flatten it for safety.
            remedy = _ensure_protected_or_flatten(a, resolved, expected_stops)
            if remedy:
                for r in out:
                    r.update(remedy)
            return out
        except Exception as e:
            return [{"account": a.get("account_name"), "ok": False, "error": str(e)}]

    results = []
    with ThreadPoolExecutor(max_workers=min(len(accounts), 16)) as pool:
        for r in pool.map(run, accounts):
            results += r
    placed = sum(1 for r in results if r.get("ok"))
    return {"results": results, "placed": placed, "total": len(results),
            "accounts": len(accounts), "contract": resolved}


def cancel_working_orders_for(account: dict, symbol: str) -> int:
    """Cancel every WORKING order (resting stop/target) for a symbol on one account, so
    a bracket can't orphan into a naked position after the position is flattened. Only
    counts a cancel that ACTUALLY succeeded (a swallowed transport failure or a broker
    reject must not be reported as cancelled)."""
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return 0
    orders = tvo.get_orders(env, token)
    versions = tvo.get_order_versions(env, token)
    root = symbol_root(str(symbol).upper()) if symbol else ""   # no symbol -> cancel ALL
    n = 0
    for o in tvo.working_orders_for(orders, versions, acct_id):
        cname = _contract_name(env, token, o.get("contractId"))
        if root and (not cname or symbol_root(cname) != root):   # fail closed: never cancel an unidentifiable order
            continue
        if _cancel_ok(tvo.cancel_order(env, token, o.get("id"))):
            n += 1
    return n


def _liquidate_positions_for(account, symbol) -> int:
    """Race-safely flatten every open position for a symbol via /order/liquidatePosition
    (server-side: cancels the position's resting orders AND closes it atomically; no-op
    when flat). Returns how many positions were liquidated."""
    token = ensure_fresh_token(account)
    env = account.get("env") or "live"
    acct_id = account.get("account_id")
    if not token or not acct_id:
        return 0
    root = symbol_root(str(symbol).upper()) if symbol else ""
    liq = 0
    for p in (tvo.get_positions(env, token) or []):
        if str(p.get("accountId")) != str(acct_id):
            continue
        if int(p.get("netPos") or 0) == 0:
            continue
        cname = _contract_name(env, token, p.get("contractId"))
        if root and (not cname or symbol_root(cname) != root):   # fail closed: never liquidate an unidentifiable position
            continue
        tvo.liquidate_position(env, token, acct_id, p.get("contractId"))
        liq += 1
    return liq


def exit_from_accounts(accounts: list, symbol: str) -> dict:
    """Idempotent exit for a bracketed strategy. Per account, in THREE independent phases
    so one failure never skips the flatten:
      1. cancel resting working orders (best-effort, verified);
      2. ALWAYS flatten open positions race-safely via /order/liquidatePosition;
      3. re-cancel any survivors so an orphan leg can never re-open a naked position.
    A no-op (ok:True, flat) when already flat."""
    accounts = list(accounts or [])
    results = []
    for a in accounts:
        r = {"account": a.get("account_name")}
        token = ensure_fresh_token(a)
        if not token or not a.get("account_id"):
            results.append({**r, "ok": False, "error": "Reconnect required"})
            continue
        cancelled = 0
        try:                                            # phase 1 — cancel (own scope)
            cancelled = cancel_working_orders_for(a, symbol)
        except Exception as e:
            r["cancel_error"] = str(e)
        try:                                            # phase 2 — flatten ALWAYS runs
            r["liquidated"] = _liquidate_positions_for(a, symbol)
        except Exception as e:
            r["flatten_error"] = str(e)
        try:                                            # phase 3 — sweep orphan survivors
            cancelled += cancel_working_orders_for(a, symbol)
        except Exception:
            pass
        r["cancelled"] = cancelled
        r["flat"] = True
        r["ok"] = "flatten_error" not in r
        results.append(r)
    placed = sum(1 for r in results if r.get("ok"))
    return {"results": results, "placed": placed, "total": len(results), "accounts": len(accounts)}


def move_stops_to_accounts(accounts: list, symbol: str, new_stop) -> dict:
    """Move the working STOP order(s) for a symbol to a new price (move_stop -> breakeven).
    Never opens or adds to a position. Best-effort: if no stop is found the original stop
    simply stays in place (still protective), which is safe."""
    accounts = list(accounts or [])
    results = []
    for a in accounts:
        try:
            token = ensure_fresh_token(a)
            env = a.get("env") or "live"
            acct_id = a.get("account_id")
            if not token or not acct_id:
                results.append({"account": a.get("account_name"), "ok": False, "error": "Reconnect required"})
                continue
            orders = tvo.get_orders(env, token)
            versions = tvo.get_order_versions(env, token)
            root = symbol_root(str(symbol).upper()) if symbol else ""
            ns = _round_tick(new_stop, _tick_for(symbol))
            moved = 0
            for o in tvo.working_orders_for(orders, versions, acct_id, order_type="Stop"):
                cname = _contract_name(env, token, o.get("contractId"))
                if root and (not cname or symbol_root(cname) != root):   # fail closed: never move an unidentifiable stop
                    continue
                tvo.modify_stop_price(env, token, o.get("id"), ns)
                moved += 1
            results.append({"account": a.get("account_name"), "ok": True, "moved": moved, "stop": ns})
        except Exception as e:
            results.append({"account": a.get("account_name"), "ok": False, "error": str(e)})
    placed = sum(1 for r in results if r.get("ok"))
    return {"results": results, "placed": placed, "total": len(results), "accounts": len(accounts)}


def replace_stops_to_accounts(accounts: list, symbol: str, position_side: str, new_stop) -> dict:
    """ADD-ONLY handler for the `replace_stop` event: CANCEL the working protective stop for
    `symbol` and PLACE a fresh stop for whatever qty is still open (the runner) at `new_stop`.
    NEVER opens, adds to, or reverses a position. `position_side` is the ENTRY/position side
    ('buy'/'sell'); the protective stop CLOSES the position, so its action is the OPPOSITE
    (short position -> BUY stop, long position -> SELL stop). Reuses the existing cancel /
    position / stop helpers and changes nothing that already works."""
    close_action = "buy" if str(position_side).lower() == "sell" else "sell"
    accounts = list(accounts or [])
    results = []
    for a in accounts:
        try:
            token = ensure_fresh_token(a)
            env = a.get("env") or "live"
            acct_id = a.get("account_id")
            name = a.get("account_name")
            if not token or not acct_id:
                results.append({"account": name, "ok": False, "error": "Reconnect required"})
                continue
            resolved = symbol
            try:
                resolved = tvo.resolve_contract(env, token, symbol)
            except Exception:
                pass
            root = symbol_root(str(resolved or symbol).upper())
            # remaining_runner = whatever is still open after the first take-profit scaled out
            open_qty = abs(_net_position_for(a, resolved))
            if open_qty <= 0:
                results.append({"account": name, "ok": False, "error": "No open position to protect"})
                continue
            # 1) cancel the existing working protective STOP(s) for THIS symbol only (fail closed)
            orders = tvo.get_orders(env, token)
            versions = tvo.get_order_versions(env, token)
            cancelled = 0
            for o in tvo.working_orders_for(orders, versions, acct_id, order_type="Stop"):
                cname = _contract_name(env, token, o.get("contractId"))
                if root and (not cname or symbol_root(cname) != root):
                    continue
                if _cancel_ok(tvo.cancel_order(env, token, o.get("id"))):
                    cancelled += 1
            # 2) place a fresh protective stop for the runner qty at the new (breakeven) price
            ns = _round_tick(new_stop, _tick_for(resolved))
            resp = tvo.place_stop_order(env, token, name, acct_id, close_action, resolved, int(open_qty), ns)
            results.append({"account": name, "ok": _order_ok(resp), "cancelled": cancelled,
                            "qty": int(open_qty), "stop": ns, "action": close_action, "response": resp})
        except Exception as e:
            results.append({"account": a.get("account_name"), "ok": False, "error": str(e)})
    placed = sum(1 for r in results if r.get("ok"))
    return {"results": results, "placed": placed, "total": len(results), "accounts": len(accounts)}


def execute_to_accounts(accounts: list, symbol: str, side: str, qty: int) -> dict:
    """Copy-trade engine: place the SAME order on every account simultaneously.

    For top-tier copy trading every account must hit the market at the same
    instant so they fill at (essentially) the same price. We therefore:
      1. Refresh tokens sequentially first (renewal is not thread-safe).
      2. Resolve the contract ONCE so every account trades the identical symbol.
      3. Fan the orders out in parallel threads — all submitted together.
    """
    accounts = list(accounts or [])
    if not accounts:
        return {"results": [], "placed": 0, "total": 0, "accounts": 0}

    # 1. Refresh every token up front (serialized) so the parallel phase below
    #    never triggers concurrent token renewals.
    for a in accounts:
        try:
            ensure_fresh_token(a)
        except Exception:
            pass

    # 2. Resolve the contract a single time so all accounts trade the same
    #    contract month (e.g. MNQ1! -> MNQM6) — no per-account drift.
    resolved = symbol
    if side != "flatten":
        for a in accounts:
            tok = dec(a["access_token_enc"]) if a.get("access_token_enc") else ""
            if tok:
                try:
                    resolved = tvo.resolve_contract(a.get("env") or "live", tok, symbol)
                    break
                except Exception:
                    pass

    # 3. Submit to all accounts in parallel — same instant, same price.
    def run(a):
        try:
            # Per-account position sizing: a fixed contract size on the account
            # overrides the alert qty (N/A -> use the alert's qty).
            aqty = account_qty(a, qty)
            # SERVER-SIDE RISK GATE — runs per account before any order reaches
            # Tradovate. Flatten/close is never blocked (it reduces exposure).
            if side != "flatten":
                allowed, reason, breach = risk_gate(a, side, aqty, resolved)
                if not allowed:
                    return [{"account": a.get("account_name"), "ok": False,
                             "error": ("Risk: " + reason), "risk_blocked": True, "breach": breach}]
            if side == "flatten":
                return flatten_on_account(a, symbol)
            elif side == "reverse":
                return [reverse_on_account(a, resolved, aqty)]
            else:
                return [place_order_on_account(a, side, resolved, aqty)]
        except Exception as e:
            return [{"account": a.get("account_name"), "ok": False, "error": str(e)}]

    results = []
    with ThreadPoolExecutor(max_workers=min(len(accounts), 16)) as pool:
        for r in pool.map(run, accounts):
            results += r

    placed = sum(1 for r in results if r.get("ok"))
    return {
        "results": results,
        "placed": placed,
        "total": len(results),
        "accounts": len(accounts),
        "contract": resolved,
    }


def account_qty(account: dict, alert_qty: int) -> int:
    """Contracts to trade on this account. A per-account fixed size overrides the
    alert quantity; N/A (NULL/blank) falls back to the size from the alert. This
    lets a single master signal trade different contract counts per account."""
    cq = account.get("contract_qty")
    try:
        if cq is not None and str(cq) != "" and int(cq) > 0:
            return int(cq)
    except (TypeError, ValueError):
        pass
    return max(1, int(alert_qty))


def set_account_qty(user_id: int, account_db_id: int, qty) -> None:
    """Persist per-account contracts. qty None/'' -> N/A (use the alert size)."""
    try:
        val = int(qty) if (qty not in (None, "") and int(qty) > 0) else None
    except (TypeError, ValueError):
        val = None
    con = db()
    con.execute("UPDATE broker_accounts SET contract_qty=? WHERE id=? AND user_id=?",
                (val, account_db_id, user_id))
    con.commit()
    con.close()


def find_connected_account(user_id: int, account_name: str):
    """Resolve a connected account by its (case-insensitive) Tradovate name."""
    name = (account_name or "").strip().lower()
    for a in get_broker_accounts(user_id, connected_only=True):
        if (a.get("account_name") or "").strip().lower() == name:
            return a
    return None


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


def _orders_account_map(orders) -> Dict[Any, Any]:
    """orderId -> accountId, so fills (which only carry orderId) can be tied
    back to the account that placed them."""
    m: Dict[Any, Any] = {}
    for o in orders or []:
        oid = o.get("id")
        aid = o.get("accountId")
        if oid is not None and aid is not None:
            m[str(oid)] = str(aid)
    return m


def _fills_for_account(fills_all, orders_map, acct_id, single_account: bool = False):
    """Return the fills belonging to `acct_id`. Tradovate Fill objects don't
    include accountId, so we resolve via the order -> account map. When the user
    has a single connected account, unattributable fills default to it."""
    aid = str(acct_id)
    out = []
    for f in fills_all or []:
        fa = f.get("accountId")
        if fa is not None and str(fa) == aid:
            out.append(f)
            continue
        oid = f.get("orderId")
        mapped = orders_map.get(str(oid)) if oid is not None else None
        if mapped is not None:
            if mapped == aid:
                out.append(f)
        elif single_account:
            out.append(f)
    return out


def live_account_monitor(user_id: int, only_account_id=None) -> Dict[str, Any]:
    """Aggregate live open positions + PnL across the user's connected accounts.
    Pass only_account_id (broker_accounts.id) to scope to a single account."""
    accounts = get_broker_accounts(user_id, connected_only=True)
    if only_account_id not in (None, "", "all"):
        accounts = [a for a in accounts if str(a["id"]) == str(only_account_id)]
    out_accounts = []
    all_trips: list = []
    token_cache: Dict[tuple, tuple] = {}
    contract_names: Dict[Any, str] = {}
    today = date.today().isoformat()

    for a in accounts:
        token = ensure_fresh_token(a)
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
            token_cache[key] = (tvo.get_positions(env, token), tvo.get_fills(env, token), tvo.get_orders(env, token))
        positions_all, fills_all, orders_all = token_cache[key]
        orders_map = _orders_account_map(orders_all)
        single_account = len(accounts) == 1

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

        # Resilience: if the realtime position endpoint returns nothing (it can
        # be briefly empty or flaky right after a fill), reconstruct open
        # positions from this account's fills instead.
        # All closed round-trips for this account from broker fills. Used for
        # win rate / drawdown / journal and as a realized-PnL fallback. Fills
        # are attributed to the account via the order map (fills lack accountId).
        acct_all_fills = _fills_for_account(fills_all, orders_map, acct_id, single_account)
        trips_acct, open_acct = build_round_trips(acct_all_fills, name_for)
        all_trips.extend(trips_acct)

        # If the realtime position endpoint is briefly empty after a fill, show
        # the open positions reconstructed from fills instead.
        if not positions:
            for o in open_acct:
                positions.append({
                    "symbol": o["symbol"],
                    "side": o["side"],
                    "qty": o["qty"],
                    "avg_price": o.get("entry_price"),
                })

        # PnL pulled DIRECTLY from Tradovate's account snapshot so KhomaAPI
        # mirrors exactly what the Tradovate panel shows (Open P/L, Total P/L).
        snap = tvo.get_cash_snapshot(env, token, acct_id)
        open_pnl = _snapshot_value(snap, ("openPnL", "openPnl", "unrealizedPnL"))
        total_pnl = _snapshot_value(snap, ("totalPnL", "totalPnl", "netPnL"))
        snap_realized = _snapshot_value(snap, ("realizedPnL", "realizedPnl", "weekRealizedPnL", "dayRealizedPnL"))
        total_cash = _snapshot_value(snap, ("totalCashValue", "netLiquidatingValue", "totalCashBalance", "cashBalance", "amount"))

        fills_realized = round(sum(float(t.get("pnl") or 0) for t in trips_acct), 2)
        if snap_realized is not None:
            realized = snap_realized
        elif total_pnl is not None and open_pnl is not None:
            realized = round(total_pnl - open_pnl, 2)
        else:
            realized = fills_realized
        if total_pnl is None:
            total_pnl = round((open_pnl or 0) + (realized or 0), 2)

        out_accounts.append({
            "name": a["account_name"], "env": env,
            "open_pnl": open_pnl, "realized_pnl": realized,
            "total_pnl": total_pnl, "total_cash": total_cash,
            "positions": positions,
        })

    def total(field):
        vals = [acc[field] for acc in out_accounts if acc.get(field) is not None]
        return round(sum(vals), 2) if vals else None

    totals = {
        "open_pnl": total("open_pnl"),
        "realized_pnl": total("realized_pnl"),
        "total_pnl": total("total_pnl"),
    }
    if totals["total_pnl"] is None and (totals["open_pnl"] is not None or totals["realized_pnl"] is not None):
        totals["total_pnl"] = round((totals["open_pnl"] or 0) + (totals["realized_pnl"] or 0), 2)

    # Live performance metrics from real closed round-trips (broker fills).
    chron = sorted(all_trips, key=lambda t: str(t.get("closed_at", "")))
    pnls = [float(t.get("pnl") or 0) for t in chron]
    running = peak = max_dd = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    wins = len([p for p in pnls if p > 0])
    losses = len([p for p in pnls if p < 0])
    closed = wins + losses
    metrics = {
        "win_rate": round(wins / closed * 100, 1) if closed else 0.0,
        "wins": wins,
        "losses": losses,
        "closed_trades": closed,
        "realized_pnl": round(sum(pnls), 2),
        "max_drawdown": round(abs(max_dd), 2),
    }

    return {
        "ok": True,
        "connected": len(accounts) > 0,
        "accounts": out_accounts,
        "totals": totals,
        "metrics": metrics,
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


def account_trade_history(user_id: int, only_account_id=None):
    """Closed round-trips + open positions across the user's connected accounts.
    Pass only_account_id (broker_accounts.id) to scope to a single account."""
    try:
        accounts = get_broker_accounts(user_id, connected_only=True)
        if only_account_id not in (None, "", "all"):
            accounts = [a for a in accounts if str(a["id"]) == str(only_account_id)]
    except Exception as e:
        # Even if the account lookup fails, fall through to the permanent ledger.
        print(f"account_trade_history: account lookup failed (uid {user_id}): {e}")
        accounts = []
    token_cache: Dict[tuple, list] = {}
    contract_names: Dict[Any, str] = {}
    all_trips, all_open = [], []

    for a in accounts:
        # A broker hiccup on ONE account (expired token, slow API, odd fill) must
        # never blank the whole dashboard/journal — we still return the permanent
        # ledger below. So each account's LIVE read is isolated.
        try:
            token = ensure_fresh_token(a)
            env = a.get("env") or "live"
            acct_id = a.get("account_id")
            if not token or not acct_id:
                continue

            key = (token, env)
            if key not in token_cache:
                token_cache[key] = (tvo.get_fills(env, token), tvo.get_orders(env, token))
            fills_all, orders_all = token_cache[key]
            orders_map = _orders_account_map(orders_all)
            single_account = len(accounts) == 1

            def name_for(cid):
                if cid in contract_names:
                    return contract_names[cid]
                c = tvo.get_contract(env, token, cid) or {}
                nm = c.get("name") if isinstance(c, dict) else None
                contract_names[cid] = nm or f"#{cid}"
                return contract_names[cid]

            acct_fills = _fills_for_account(fills_all, orders_map, acct_id, single_account)
            trips, openp = build_round_trips(acct_fills, name_for)
            for t in trips:
                t["account"] = a["account_name"]
                t["_account_id"] = a["id"]
            for o in openp:
                o["account"] = a["account_name"]
            all_trips += trips
            all_open += openp
        except Exception as e:
            print(f"account_trade_history: live read failed for account {a.get('id')}: {e}")
            continue

    # Tradovate's get_fills only returns a short recent window, so realized P&L
    # would vanish from old days. Persist every trip we see into a permanent
    # ledger and ALWAYS return the ledger ∪ live fills, so the dashboard/journal
    # show the full realized P&L (deduped by intrinsic identity). Both steps are
    # best-effort: a persistence error must not discard the ledger we can read.
    try:
        _ledger_persist_trips(user_id, all_trips)
    except Exception as e:
        print(f"account_trade_history: ledger persist failed (uid {user_id}): {e}")
    try:
        all_trips = _ledger_merge(user_id, all_trips, only_account_id)
    except Exception as e:
        print(f"account_trade_history: ledger merge failed (uid {user_id}): {e}")
    all_trips.sort(key=lambda x: str(x.get("closed_at", "")), reverse=True)
    return all_trips, all_open


def _trip_ident(t):
    """Stable INTRINSIC identity of a closed round-trip, used to dedup the permanent
    ledger UNION live fills. Keyed on the account NAME, which is STABLE across
    reconnects (broker_accounts.id rotates 44->55->56..., but the Tradovate account
    name does not) — so a trade still inside the live fills window when a user
    reconnects is not counted twice. Distinct accounts keep distinct identities, so
    copy-trade fills across accounts are never collapsed. Falls back to the row id
    only for legacy rows that predate the account_name column (always out of the live
    window, so they never produce a live duplicate)."""
    acct = t.get("account") or t.get("_account_id", "")

    def _n(v):   # normalize numbers so int 100 and float 100.0 share one identity
        try:
            return format(float(v), ".4f")
        except (TypeError, ValueError):
            return str(v)

    return "|".join((
        str(acct), str(t.get("symbol", "")), str(t.get("side", "")),
        _n(t.get("qty", "")), _n(t.get("entry_price", "")), _n(t.get("exit_price", "")),
        str(t.get("closed_at", "")),
    ))


def _ledger_persist_trips(user_id, trips):
    """Save newly-seen closed round-trips into the permanent ledger (idempotent).
    Deduped by intrinsic identity (account_id+symbol+side+qty+prices+close) so the
    same trade is never logged twice even if its trip_key shifts."""
    rows = [t for t in (trips or []) if t.get("closed_at")]
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    for t in rows:
        exists = con.execute(
            "SELECT 1 FROM trade_log WHERE user_id=? AND account_id=? AND symbol=? "
            "AND side=? AND qty=? AND entry_price=? AND exit_price=? AND closed_at=? LIMIT 1",
            (user_id, t.get("_account_id"), t.get("symbol"), t.get("side"), t.get("qty"),
             t.get("entry_price"), t.get("exit_price"), t.get("closed_at")),
        ).fetchone()
        if exists:
            continue
        con.execute(
            "INSERT INTO trade_log(user_id,account_id,account_name,trip_key,side,symbol,qty,"
            "entry_price,exit_price,pnl,opened_at,closed_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,trip_key) DO NOTHING",
            (user_id, t.get("_account_id"), t.get("account"), trip_key(t), t.get("side"),
             t.get("symbol"), t.get("qty"), t.get("entry_price"), t.get("exit_price"),
             t.get("pnl"), t.get("opened_at"), t.get("closed_at"), now),
        )
    con.commit()
    con.close()


def _ledger_merge(user_id, live_trips, only_account_id=None):
    """Return live trips UNION the permanent ledger, deduped by INTRINSIC identity
    (never the cosmetic account name). Live values win; the ledger backfills days
    Tradovate no longer returns. Collapsing by identity also folds away any legacy
    duplicate rows so realized P&L is never double-counted."""
    q = ("SELECT trip_key, account_id, account_name, side, symbol, qty, entry_price, "
         "exit_price, pnl, opened_at, closed_at FROM trade_log WHERE user_id=?")
    params = [user_id]
    if only_account_id not in (None, "", "all"):
        try:
            q += " AND account_id=?"
            params.append(int(only_account_id))
        except (TypeError, ValueError):
            pass
    con = db()
    rows = con.execute(q, tuple(params)).fetchall()
    con.close()
    by_ident = {}
    for r in rows:
        t = {
            "account": r["account_name"] or "", "_account_id": r["account_id"],
            "side": r["side"], "symbol": r["symbol"], "qty": r["qty"],
            "entry_price": r["entry_price"], "exit_price": r["exit_price"],
            "pnl": r["pnl"], "opened_at": r["opened_at"], "closed_at": r["closed_at"],
        }
        by_ident[_trip_ident(t)] = t
    for t in (live_trips or []):     # live (fresh) wins
        by_ident[_trip_ident(t)] = t
    return list(by_ident.values())


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


def dashboard_metrics(user_id: int, trips: Optional[list] = None):
    """Win rate / drawdown / realized PnL from REAL broker fills (closed
    round-trips), not the order-placement log (which carries no PnL). Counts and
    latency still come from the execution log. Pass `trips` to reuse an already
    fetched account_trade_history result and avoid a second broker call."""
    if trips is None:
        try:
            trips, _open = account_trade_history(user_id)
        except Exception:
            trips = []

    # Round-trips ordered chronologically build the realized equity curve.
    chron = sorted(trips, key=lambda t: str(t.get("closed_at", "")))
    pnl_values = [float(t.get("pnl") or 0) for t in chron]

    equity = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnl_values:
        running += pnl
        equity.append(round(running, 2))
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    wins = len([p for p in pnl_values if p > 0])
    losses = len([p for p in pnl_values if p < 0])
    closed_with_pnl = wins + losses
    win_rate = round((wins / closed_with_pnl) * 100, 1) if closed_with_pnl else 0.0

    # Execution counts + latency from the local trade log.
    rows = get_user_trades(user_id, 500)
    executed = [r for r in rows if r["status"] in ("EXECUTED", "SIMULATED", "FLATTEN_SENT", "SKIPPED")]
    rejected = [r for r in rows if r["status"] == "REJECTED"]
    latencies = [float(r["latency_ms"] or 0) for r in rows if r["latency_ms"]]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0

    return {
        "total_trades": len(executed),
        "rejected": len(rejected),
        "closed_trades": closed_with_pnl,
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "total_pnl": round(running, 2),
        "realized_pnl": round(running, 2),
        "max_drawdown": round(abs(max_dd), 2),
        "avg_latency": avg_latency,
        "equity": equity[-40:] if equity else [0, 0, 0, 0, 0],
    }


def daily_journal(user_id: int, trips: Optional[list] = None):
    """Per-day execution counts (from the log) and realized PnL (from real broker
    round-trips). Pass `trips` to reuse a fetched account_trade_history result."""
    rows = get_user_trades(user_id, 500)
    days: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        day = _et_day(r["ts"])           # ET, to agree with the journal calendar
        if not day:
            continue
        days.setdefault(day, {"trades": 0, "executed": 0, "rejected": 0, "pnl": 0.0})
        days[day]["trades"] += 1
        if r["status"] == "REJECTED":
            days[day]["rejected"] += 1
        if r["status"] in ("EXECUTED", "SIMULATED", "FLATTEN_SENT", "SKIPPED"):
            days[day]["executed"] += 1

    # Realized PnL per day from actual broker fills (closed round-trips), keyed by ET
    # close date so the dashboard mini-journal matches the journal calendar exactly.
    if trips is None:
        try:
            trips, _open = account_trade_history(user_id)
        except Exception:
            trips = []
    for t in trips:
        day = _et_day(t.get("closed_at"))
        if not day:
            continue
        days.setdefault(day, {"trades": 0, "executed": 0, "rejected": 0, "pnl": 0.0})
        days[day]["pnl"] += float(t.get("pnl") or 0)

    return sorted(days.items(), reverse=True)[:10]


# ============================================================
# KhomaTradingJournal — analytics over real closed round-trips
# ============================================================

_ET = "America/New_York"


def _trip_dt(t, key):
    try:
        d = datetime.fromisoformat(str(t.get(key) or "").replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def trip_key(t: dict) -> str:
    """Stable, URL-safe id for a closed round-trip so tags/notes/screenshots
    survive re-reading the same trip from broker fills."""
    sig = "|".join(str(x) for x in (
        t.get("account", ""), t.get("symbol", ""), t.get("side", ""),
        t.get("qty", ""), t.get("entry_price", ""), t.get("exit_price", ""),
        t.get("closed_at", ""),
    ))
    return hashlib.sha1(sig.encode()).hexdigest()


def get_trip_journal_map(user_id: int) -> Dict[str, dict]:
    con = db()
    rows = con.execute("SELECT * FROM trip_journal WHERE user_id=?", (user_id,)).fetchall()
    con.close()
    return {r["trip_key"]: dict(r) for r in rows}


def save_trip_journal(user_id: int, key: str, tags: str, note: str, image_path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    con = db()
    if image_path is None:
        existing = con.execute("SELECT image_path FROM trip_journal WHERE user_id=? AND trip_key=?",
                               (user_id, key)).fetchone()
        image_path = existing["image_path"] if existing else None
    con.execute(
        """INSERT INTO trip_journal(user_id,trip_key,tags,note,image_path,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(user_id,trip_key) DO UPDATE SET
             tags=excluded.tags, note=excluded.note, image_path=excluded.image_path, updated_at=excluded.updated_at""",
        (user_id, key, tags, note, image_path, now, now),
    )
    con.commit()
    con.close()


def parse_tags(s: str) -> list:
    return [t.strip() for t in str(s or "").replace(";", ",").split(",") if t.strip()]


def journal_analytics(trips: list, tags_map: dict = None) -> dict:
    """Full performance analytics over closed round-trips (entry/exit/pnl/side/
    qty/symbol/opened_at/closed_at). The best of Tradezella/TraderSync, computed
    from what KhomaAPI actually has from broker fills."""
    pnls = [float(t.get("pnl") or 0) for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    scratches = [p for p in pnls if p == 0]
    n = len(pnls)
    gross_profit = round(sum(wins), 2)
    gross_loss = round(-sum(losses), 2)
    net = round(sum(pnls), 2)
    decided = len(wins) + len(losses)
    win_rate = round(len(wins) / decided * 100, 1) if decided else 0.0
    avg_win = round(gross_profit / len(wins), 2) if wins else 0.0
    avg_loss = round(gross_loss / len(losses), 2) if losses else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (None if gross_profit == 0 else float("inf"))
    win_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else None
    expectancy = round((win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss), 2) if decided else 0.0

    # Hold times (minutes).
    holds = []
    for t in trips:
        a, b = _trip_dt(t, "opened_at"), _trip_dt(t, "closed_at")
        if a and b and b >= a:
            holds.append((b - a).total_seconds() / 60.0)
    avg_hold = round(sum(holds) / len(holds), 1) if holds else 0.0

    # Streaks (chronological).
    chron = sorted(trips, key=lambda x: str(x.get("closed_at", "")))
    max_w = max_l = cur_w = cur_l = 0
    for t in chron:
        p = float(t.get("pnl") or 0)
        if p > 0:
            cur_w += 1; cur_l = 0; max_w = max(max_w, cur_w)
        elif p < 0:
            cur_l += 1; cur_w = 0; max_l = max(max_l, cur_l)

    # Equity curve (cumulative).
    equity, run = [], 0.0
    for t in chron:
        run += float(t.get("pnl") or 0)
        equity.append(round(run, 2))

    # Daily P&L (ET date) -> calendar + best/worst day.
    daily = {}
    for t in trips:
        d = _trip_dt(t, "closed_at")
        if not d:
            continue
        key = d.astimezone(ZoneInfo(_ET)).strftime("%Y-%m-%d")
        daily[key] = round(daily.get(key, 0) + float(t.get("pnl") or 0), 2)
    green_days = sum(1 for v in daily.values() if v > 0)
    red_days = sum(1 for v in daily.values() if v < 0)
    best_day = max(daily.items(), key=lambda x: x[1]) if daily else None
    worst_day = min(daily.items(), key=lambda x: x[1]) if daily else None
    day_win_rate = round(green_days / (green_days + red_days) * 100, 1) if (green_days + red_days) else 0.0
    avg_daily = round(sum(daily.values()) / len(daily), 2) if daily else 0.0

    # Breakdowns.
    def bucket(keyfn):
        b = {}
        for t in trips:
            k = keyfn(t)
            if k is None:
                continue
            e = b.setdefault(k, {"pnl": 0.0, "n": 0, "w": 0})
            p = float(t.get("pnl") or 0)
            e["pnl"] += p; e["n"] += 1
            if p > 0:
                e["w"] += 1
        for e in b.values():
            e["pnl"] = round(e["pnl"], 2)
            e["win_rate"] = round(e["w"] / e["n"] * 100) if e["n"] else 0
        return b

    by_symbol = bucket(lambda t: (t.get("symbol") or "?"))
    by_side = bucket(lambda t: (t.get("side") or "?").lower())
    by_weekday = bucket(lambda t: _trip_dt(t, "closed_at").astimezone(ZoneInfo(_ET)).weekday() if _trip_dt(t, "closed_at") else None)
    by_hour = bucket(lambda t: _trip_dt(t, "closed_at").astimezone(ZoneInfo(_ET)).hour if _trip_dt(t, "closed_at") else None)

    # Performance by user tag (a trip can carry several tags).
    by_tag = {}
    if tags_map:
        for t in trips:
            jr = tags_map.get(trip_key(t))
            if not jr:
                continue
            p = float(t.get("pnl") or 0)
            for tag in parse_tags(jr.get("tags")):
                e = by_tag.setdefault(tag, {"pnl": 0.0, "n": 0, "w": 0})
                e["pnl"] += p; e["n"] += 1
                if p > 0:
                    e["w"] += 1
        for e in by_tag.values():
            e["pnl"] = round(e["pnl"], 2)
            e["win_rate"] = round(e["w"] / e["n"] * 100) if e["n"] else 0

    return {
        "n": n, "net": net, "gross_profit": gross_profit, "gross_loss": gross_loss,
        "win_rate": win_rate, "wins": len(wins), "losses": len(losses), "scratches": len(scratches),
        "avg_win": avg_win, "avg_loss": avg_loss, "profit_factor": profit_factor,
        "win_loss_ratio": win_loss_ratio, "expectancy": expectancy, "avg_hold": avg_hold,
        "largest_win": round(max(pnls), 2) if pnls else 0.0,
        "largest_loss": round(min(pnls), 2) if pnls else 0.0,
        "max_win_streak": max_w, "max_loss_streak": max_l,
        "equity": equity, "daily": daily,
        "green_days": green_days, "red_days": red_days, "day_win_rate": day_win_rate,
        "avg_daily": avg_daily, "best_day": best_day, "worst_day": worst_day,
        "by_symbol": by_symbol, "by_side": by_side, "by_weekday": by_weekday, "by_hour": by_hour,
        "by_tag": by_tag,
    }


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

    max_rejections = int(user["max_rejections_per_day"] or 50) if user else 50

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

    if s in ["reverse", "flip", "reverse_position"]:
        return "reverse"

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


def account_tabs(user_id: int, active, base: str) -> str:
    """Account switcher: 'All Accounts' + one tab per connected account, so the
    user gets a separate per-account dashboard/journal view."""
    accts = get_broker_accounts(user_id, connected_only=True)
    if not accts:
        return ""

    def tab(aid, label):
        cls = "btn" if str(active) == str(aid) else "btn secondary"
        return f'<a class="{cls}" style="margin:0 8px 8px 0" href="{base}?account={aid}">{label}</a>'

    html = tab("all", "All Accounts")
    for a in accts:
        label = f'{a["account_name"]} · {(a["env"] or "").upper()}'
        html += tab(a["id"], label)
    return f'<div style="margin:0 0 20px;display:flex;flex-wrap:wrap">{html}</div>'


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
    live = status == "Running"
    status_label = "Live" if live else "Paused"
    status_pill_cls = "pill" if live else "pill gray"

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
.nav a {{ display:flex; gap:12px; align-items:center; padding:13px 15px; color:#374151; text-decoration:none; border-radius:12px; margin-bottom:7px; font-size:14px; font-weight:700; transition:background .16s ease, color .16s ease, transform .12s ease; }}
.nav a:hover {{ transform:translateX(2px); }}
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
.btn,button {{ border:none; background:linear-gradient(135deg,#12a150,#087135); color:white; padding:12px 15px; border-radius:12px; text-decoration:none; font-weight:900; display:inline-flex; align-items:center; justify-content:center; gap:8px; cursor:pointer; margin:4px 6px 4px 0; box-shadow:0 12px 28px rgba(15,143,69,.18); transition:transform .14s cubic-bezier(.2,.7,.3,1), box-shadow .2s ease, filter .15s ease, background .2s ease; will-change:transform; }}
.btn:hover,button:hover {{ transform:translateY(-2px); filter:brightness(1.06); box-shadow:0 18px 38px rgba(15,143,69,.28); }}
.btn:active,button:active {{ transform:translateY(0) scale(.985); filter:brightness(.97); box-shadow:0 6px 16px rgba(15,143,69,.20); transition-duration:.05s; }}
.btn:focus-visible,button:focus-visible {{ outline:none; box-shadow:0 0 0 4px var(--green-soft),0 12px 28px rgba(15,143,69,.18); }}
.btn.secondary,button.secondary {{ background:white; color:#374151; border:1px solid var(--line); box-shadow:none; }}
.btn.secondary:hover,button.secondary:hover {{ background:#f8faf9; border-color:var(--green-line); filter:none; box-shadow:0 8px 20px rgba(17,24,39,.07); }}
.btn.secondary:active,button.secondary:active {{ background:#eef2f0; }}
.btn.secondary:focus-visible,button.secondary:focus-visible {{ box-shadow:0 0 0 4px var(--green-soft); }}
.btn.danger,button.danger {{ background:var(--danger); box-shadow:0 12px 28px rgba(220,38,38,.18); }}
.btn.danger:hover,button.danger:hover {{ box-shadow:0 18px 38px rgba(220,38,38,.30); }}
.btn.danger:focus-visible,button.danger:focus-visible {{ box-shadow:0 0 0 4px #fde2e2,0 12px 28px rgba(220,38,38,.18); }}
@media (prefers-reduced-motion: reduce) {{ .btn,button {{ transition:none; }} .btn:hover,button:hover,.btn:active,button:active {{ transform:none; }} }}
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
/* Hamburger (hidden on desktop) + drawer backdrop */
.menu-btn {{ display:none; align-items:center; justify-content:center; background:#fff; color:var(--text); border:1px solid var(--line); width:44px; height:44px; min-width:44px; padding:0; border-radius:12px; font-size:21px; line-height:1; box-shadow:none; margin:0; flex:0 0 auto; }}
.menu-btn:hover {{ transform:none; filter:none; background:#f8faf9; box-shadow:0 8px 20px rgba(17,24,39,.07); }}
.menu-btn:active {{ transform:scale(.95); }}
.sidebar-backdrop {{ display:none; position:fixed; inset:0; background:rgba(17,24,39,.45); z-index:150; opacity:0; transition:opacity .2s ease; }}
.sidebar-backdrop.show {{ display:block; opacity:1; }}
.topbar-left {{ display:flex; align-items:center; gap:12px; min-width:0; }}

/* Tablet: collapse the 12-col grid to a single column before the phone breakpoint */
@media(max-width:1100px) {{
  .span3,.span4,.span5,.span6,.span7,.span8 {{ grid-column:span 12; }}
  .formgrid {{ grid-template-columns:1fr; }}
}}

/* Phone / small tablet: off-canvas slide-in nav drawer + stacked content */
@media(max-width:900px) {{
  .shell {{ display:block; }}
  .sidebar {{ width:280px; transform:translateX(-100%); transition:transform .26s cubic-bezier(.2,.7,.3,1); z-index:200; box-shadow:0 24px 80px rgba(17,24,39,.20); overflow-y:auto; padding-bottom:40px; }}
  .sidebar.open {{ transform:translateX(0); }}
  .sidebar-card {{ position:static; margin-top:20px; }}
  .main {{ margin-left:0; }}
  .menu-btn {{ display:inline-flex; }}
  .topbar {{ height:60px; padding:0 14px; gap:10px; }}
  .top-left span {{ display:none; }}
  .top-left b {{ font-size:13px; display:inline-block; max-width:38vw; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; vertical-align:middle; }}
  .top-actions {{ gap:8px; }}
  .avatar {{ width:38px; height:38px; font-size:14px; }}
  .content {{ padding:20px 15px 56px; }}
  .header {{ display:block; margin-bottom:18px; }}
  .header h2 {{ font-size:25px; letter-spacing:-.8px; }}
  .header p {{ font-size:14px; margin-top:7px; }}
  .grid {{ gap:15px; }}
  .span12,.span3,.span4,.span5,.span6,.span7,.span8 {{ grid-column:span 12; }}
  .card {{ padding:18px; border-radius:18px; overflow-x:auto; }}
  .metric {{ font-size:27px; }}
  .formgrid {{ grid-template-columns:1fr; }}
  table {{ min-width:540px; }}  /* wide tables scroll inside their card instead of squashing */
}}
@media(max-width:430px) {{
  .header h2 {{ font-size:22px; }}
  .top-left b {{ max-width:30vw; }}
  .pill {{ padding:7px 9px; font-size:11px; }}
  .content {{ padding:18px 12px 50px; }}
}}
</style>
<script>
function copyText(id) {{
  const text = document.getElementById(id).innerText;
  navigator.clipboard.writeText(text);
  alert('Copied');
}}
function toggleSidebar() {{
  const s = document.getElementById('sidebar');
  const b = document.getElementById('sidebarBackdrop');
  const open = s.classList.toggle('open');
  if (b) {{ b.classList.toggle('show', open); }}
  document.body.style.overflow = open ? 'hidden' : '';
}}
function closeSidebar() {{
  const s = document.getElementById('sidebar');
  const b = document.getElementById('sidebarBackdrop');
  if (s) {{ s.classList.remove('open'); }}
  if (b) {{ b.classList.remove('show'); }}
  document.body.style.overflow = '';
}}
// Tapping any nav link closes the drawer; resizing back to desktop resets it.
document.addEventListener('DOMContentLoaded', function() {{
  document.querySelectorAll('.sidebar .nav a').forEach(function(a) {{
    a.addEventListener('click', closeSidebar);
  }});
}});
window.addEventListener('resize', function() {{
  if (window.innerWidth > 900) {{ closeSidebar(); }}
}});
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
<div class="sidebar-backdrop" id="sidebarBackdrop" onclick="closeSidebar()"></div>
<aside class="sidebar" id="sidebar">
  <div class="brand"><div class="logo">
  <img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:14px;">
</div><div><h1>KhomaAPI</h1><small>Execution Infrastructure</small></div></div>
  <div class="nav">
    {nav_item(active,'dashboard','/dashboard','⌁','Dashboard')}
    {nav_item(active,'broker','/broker','◇','Broker Connect')}
    {nav_item(active,'webhooks','/webhooks','⌘','Webhooks')}
    {nav_item(active,'logs','/logs','▥','Trade Logs')}
    {nav_item(active,'journal','/journal','◷','Journal')}
    {nav_item(active,'calendar','/calendar','◆','Economic Calendar')}
    {nav_item(active,'risk','/risk','☰','Risk Engine')}
    {nav_item(active,'settings','/settings','⚙','Settings')}
  </div>
</aside>
<main class="main">
  <div class="topbar">
    <div class="topbar-left">
      <button class="menu-btn" onclick="toggleSidebar()" aria-label="Open menu">☰</button>
      <div class="top-left"><b>{email}</b><span>TradingView automation workspace</span></div>
    </div>
    <div class="top-actions"><span class="{status_pill_cls}">● {status_label}</span>
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

    // Real-time risk events: surface instantly and refresh the relevant views.
    if(data.event === "risk") {{
        try {{
            var bar = document.createElement("div");
            if(data.type === "eval_passed") {{
                bar.textContent = "🎉 EVALUATION PASSED — " + (data.account||"") + "! Funded setup unlocked.";
                bar.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#0f8f45;color:#fff;font-weight:800;padding:12px 18px;text-align:center;box-shadow:0 6px 20px rgba(0,0,0,.2);";
            }} else {{
                bar.textContent = "⚠ RISK LOCK — " + (data.account||"") + ": " + (data.reason||"");
                bar.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;font-weight:800;padding:12px 18px;text-align:center;box-shadow:0 6px 20px rgba(0,0,0,.2);";
            }}
            document.body.appendChild(bar);
        }} catch(e) {{}}
        if(window.location.pathname === "/dashboard" ||
           window.location.pathname === "/risk" ||
           window.location.pathname === "/logs") {{
            setTimeout(function(){{ location.reload(); }}, 2000);
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
    # Return an explicit HTMLResponse so routes WITHOUT response_class=HTMLResponse
    # (e.g. the POST handlers for forgot/change email/password) still render as
    # HTML instead of being JSON-encoded into raw text by FastAPI.
    return HTMLResponse(f"""
<!DOCTYPE html><html><head><title>KhomaAPI Login</title><style>
body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;background:#f8faf9;color:#111827;}}
.wrap{{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px 16px;box-sizing:border-box;background:radial-gradient(circle at 10% 10%,#dff5e7,transparent 30%),radial-gradient(circle at 90% 20%,#eefaf2,transparent 28%),#f8faf9;}}
.card{{width:470px;max-width:100%;box-sizing:border-box;background:white;border:1px solid #e5e7eb;border-radius:24px;padding:34px;box-shadow:0 24px 90px rgba(17,24,39,.09);}}
@media(max-width:520px){{ .wrap{{padding:16px 12px;}} .card{{padding:26px 22px;border-radius:20px;}} h1{{font-size:24px;}} }}
.logo{{width:48px;height:48px;border-radius:15px;background:linear-gradient(135deg,#16a34a,#064e2a);color:white;display:flex;align-items:center;justify-content:center;font-weight:950;margin-bottom:20px;}}
h1{{letter-spacing:-1px;margin:0 0 8px;}} p{{color:#6b7280;line-height:1.55;}} input{{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:13px;margin:8px 0 14px;box-sizing:border-box;}}
button,.btn{{background:#0f8f45;color:white;border:none;padding:13px 16px;border-radius:13px;font-weight:900;text-decoration:none;display:inline-block;cursor:pointer;box-shadow:0 10px 24px rgba(15,143,69,.18);transition:transform .14s cubic-bezier(.2,.7,.3,1),box-shadow .2s ease,filter .15s ease;}} a{{color:#0f8f45;font-weight:850;}}
button:hover,.btn:hover{{transform:translateY(-2px);filter:brightness(1.06);box-shadow:0 16px 34px rgba(15,143,69,.28);}}
button:active,.btn:active{{transform:translateY(0) scale(.985);filter:brightness(.97);box-shadow:0 6px 16px rgba(15,143,69,.2);transition-duration:.05s;}}
button:focus-visible,.btn:focus-visible{{outline:none;box-shadow:0 0 0 4px #dff5e7,0 10px 24px rgba(15,143,69,.18);}}
.google{{background:white;color:#111827;border:1px solid #e5e7eb;width:100%;margin-bottom:12px;box-shadow:none;}}
.google:hover{{background:#f8faf9;filter:none;box-shadow:0 8px 20px rgba(17,24,39,.07);}}
@media (prefers-reduced-motion: reduce){{button,.btn{{transition:none;}}button:hover,.btn:hover,button:active,.btn:active{{transform:none;}}}}
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

    // Real-time risk events: surface instantly and refresh the relevant views.
    if(data.event === "risk") {{
        try {{
            var bar = document.createElement("div");
            if(data.type === "eval_passed") {{
                bar.textContent = "🎉 EVALUATION PASSED — " + (data.account||"") + "! Funded setup unlocked.";
                bar.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#0f8f45;color:#fff;font-weight:800;padding:12px 18px;text-align:center;box-shadow:0 6px 20px rgba(0,0,0,.2);";
            }} else {{
                bar.textContent = "⚠ RISK LOCK — " + (data.account||"") + ": " + (data.reason||"");
                bar.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#b91c1c;color:#fff;font-weight:800;padding:12px 18px;text-align:center;box-shadow:0 6px 20px rgba(0,0,0,.2);";
            }}
            document.body.appendChild(bar);
        }} catch(e) {{}}
        if(window.location.pathname === "/dashboard" ||
           window.location.pathname === "/risk" ||
           window.location.pathname === "/logs") {{
            setTimeout(function(){{ location.reload(); }}, 2000);
        }}
    }}
}};

socket.onclose = () => {{
    console.log("WebSocket disconnected");
}};

</script>

</body>
</html>
""")


# ============================================================
# AUTH ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/dashboard" if current_user(request) else "/login")


@app.get("/signup", response_class=HTMLResponse)
def signup_page():
    return login_layout(f'''
    <div class="logo">
<img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
</div>
    <h1>Set up your KhomaAPI account</h1>
    <p>Already purchased a plan? Use the <b>same email you bought with on Whop</b> to set your password and unlock your dashboard.</p>
    {google_login_button()}
    <form method="post" action="/signup">
      <input name="email" type="email" placeholder="Email you purchased with" required>
      <input name="password" type="password" placeholder="Choose a password" minlength="8" required>
      <p style="color:#6b7280;font-size:13px;margin:-6px 0 14px;">At least 8 characters, with an uppercase letter, a number, and a special character.</p>
      <button>Activate my account</button>
    </form>
    <p>Haven't picked a plan yet? <a href="/subscribe">Choose a plan</a></p>
    <p>Already set up? <a href="/login">Sign in</a></p>
    ''')

@app.post("/signup", response_class=HTMLResponse)
def signup(email: str = Form(...), password: str = Form(...)):

    if not valid_email(email):
        return login_layout("<h1>Invalid Email</h1><p>Please enter valid email.</p>")

    ok, message = strong_password(password)

    if not ok:
        return login_layout(f"<h1>Weak Password</h1><p>{message}</p>")

    # Gated set-password: when enforcement is ON, only an email with an active
    # Whop purchase may create an account ("buy first, then set your password").
    membership = None
    if ENFORCE_SUBSCRIPTIONS:
        membership = whop_membership_for_email(email)
        if not membership:
            return login_layout(
                "<h1>No active plan found</h1>"
                "<p>We couldn't find an active KhomaAPI plan for that email. "
                "Choose a plan first, then set your password here using the "
                "<b>same email</b> you bought with.</p>"
                "<a class='btn' href='/subscribe'>Choose a plan</a>"
                "<p style='margin-top:14px;'><a href='/login'>Back to sign in</a></p>")

    # If email is configured, accounts start unverified and must confirm.
    # Without SMTP, auto-verify. A confirmed Whop buyer is auto-verified (their
    # Whop email is proof of purchase) so they aren't blocked behind a second step.
    verified = 0 if email_enabled() else 1
    if membership:
        verified = 1

    con = db()
    try:
        cur = con.cursor()
        uid = dbmod.insert_returning_id(
            cur,
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
        cur.execute("INSERT INTO brokers(user_id) VALUES(?)", (uid,))
        con.commit()
    except Exception:
        con.close()
        return login_layout("<h1>Account already exists.</h1><p>That email is already registered.</p><a href='/login'>Go to login</a>")

    con.close()

    # Link the verified purchase immediately so the new account is active.
    if membership:
        try:
            link_membership_to_user(membership, by_user_id=uid)
        except Exception as e:
            print(f"signup whop link failed: {e}")

    if email_enabled():
        token = create_email_token(uid, "signup")
        link = f"{APP_URL}/verify-email/{token}"
        sent = send_branded_email(
            email.lower().strip(),
            "Verify your KhomaAPI account",
            "Welcome to KhomaAPI 👋",
            "Thanks for signing up. Confirm your email address to activate your account and start automating your TradingView strategies.<br><br>This link expires in 1 hour.",
            button_label="Verify my email",
            button_url=link,
            text_fallback=f"Welcome to KhomaAPI. Confirm your email to activate your account:\n\n{link}\n\nThis link expires in 1 hour.",
        )
        if sent:
            return login_layout("<h1>Check your email</h1><p>We sent a verification link to confirm your account. Click it, then log in.</p><a class='btn' href='/login'>Go to login</a>")
        # Email delivery failed — don't lock the user out: activate immediately.
        con = db()
        con.execute("UPDATE users SET is_verified=1 WHERE id=?", (uid,))
        con.commit()
        con.close()
        return login_layout(f"<h1>Account created</h1><p>We couldn't send the verification email, so your account has been activated directly — you can log in.</p><p class='muted' style='font-size:12px'>Email status: {LAST_EMAIL_ERROR}</p><a class='btn' href='/login'>Go to login</a>")

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
    return login_layout(f'''
    <div class="logo">
<img src="/static/logo.png" style="width:100%;height:100%;object-fit:cover;border-radius:15px;">
</div>
    <h1>Welcome back</h1>
    <p>Login to your KhomaAPI execution workspace.</p>
    {google_login_button()}
    <form method="post" action="/login">
      <input name="email" placeholder="Email" required>
      <input name="password" type="password" placeholder="Password" required>
      <button>Login</button>
    </form>
    <p><a href="/forgot-password">Forgot Password?</a></p>
    <p>Just purchased a plan? <a href="/signup">Set your password</a></p>
    <p style="font-size:13px;color:#6b7280;">No account yet? <a href="/subscribe">Choose a plan</a> to get started.</p>
    ''')


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_norm = email.lower().strip()
    ip = sec.client_ip(request)
    fail_key = f"loginfail:{ip}:{email_norm}"
    # Backoff: too many recent failures for this email or IP → make them wait.
    if LIMITER.count(fail_key, 900) >= 8 or LIMITER.count(f"loginfail:{ip}", 900) >= 25:
        return login_layout("<h1>Too many attempts</h1><p>Please wait a few minutes before trying again.</p><a href='/login'>Back</a>")

    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (email_norm,)).fetchone()
    con.close()

    if not user or not verify_password(password, user["password_hash"]):
        LIMITER.add(fail_key)
        LIMITER.add(f"loginfail:{ip}")
        return login_layout("<h1>Invalid login</h1><p>Email or password is wrong.</p><a href='/login'>Try again</a>")

    LIMITER.clear(fail_key)  # successful login resets the counter

    if email_enabled() and not user["is_verified"]:
        # Re-send the verification link. If email delivery is failing, activate
        # the account instead of locking the user out permanently.
        token = create_email_token(user["id"], "signup")
        link = f"{APP_URL}/verify-email/{token}"
        sent = send_branded_email(
            user["email"],
            "Verify your KhomaAPI account",
            "Confirm your email",
            "Please confirm your email address to activate your KhomaAPI account.<br><br>This link expires in 1 hour.",
            button_label="Verify my email",
            button_url=link,
            text_fallback=f"Confirm your email to activate your KhomaAPI account:\n\n{link}\n\nThis link expires in 1 hour.",
        )
        if sent:
            return login_layout("<h1>Verify your email</h1><p>We just re-sent your verification link — click it, then log in.</p><a href='/login'>Back to login</a>")
        con = db()
        con.execute("UPDATE users SET is_verified=1 WHERE id=?", (user["id"],))
        con.commit()
        con.close()

    try_link_whop(user)  # attach an existing Whop membership by email, if any
    sid = create_session(user["id"])
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        "khoma_session",
        sid,
        httponly=True,
        secure=COOKIE_SECURE,
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
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", f"{APP_URL}/auth/google/callback")


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
    # Only trust Google's VERIFIED email as the identity (v2 userinfo uses
    # "verified_email"; OpenID uses "email_verified").
    email_verified = google_user.get("verified_email", google_user.get("email_verified"))

    if not email:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Google account email not received."})
    if email_verified is False:
        return JSONResponse(status_code=400, content={"ok": False, "error": "Your Google email address is not verified."})

    email_norm = email.lower().strip()
    con = db()
    existing_user = con.execute("SELECT * FROM users WHERE email=?", (email_norm,)).fetchone()
    con.close()

    # Gated like every other entry point: a brand-new Google email with NO active
    # Whop purchase does not get an account — send them to choose a plan.
    membership = None
    if not existing_user and ENFORCE_SUBSCRIPTIONS:
        membership = whop_membership_for_email(email_norm)
        if not membership:
            return RedirectResponse("/subscribe", status_code=302)

    if not existing_user:
        random_password = secrets.token_hex(24)
        con = db()
        cur = con.cursor()
        uid = dbmod.insert_returning_id(
            cur,
            """
            INSERT INTO users(email,password_hash,api_key,webhook_secret,created_at,allowed_symbols)
            VALUES(?,?,?,?,?,?)
            """,
            (
                email_norm,
                hash_password(random_password),
                "khoma_live_" + secrets.token_urlsafe(24),
                secrets.token_hex(20),
                datetime.now(timezone.utc).isoformat(),
                "*",
            ),
        )
        cur.execute("INSERT INTO brokers(user_id) VALUES(?)", (uid,))
        con.commit()
        user = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        con.close()
        if membership:
            try:
                link_membership_to_user(membership, by_user_id=uid)
            except Exception as e:
                print(f"google whop link failed: {e}")
    else:
        user = existing_user

    try_link_whop(user)  # attach an existing Whop membership by email, if any
    sid = create_session(user["id"])
    response = RedirectResponse("/dashboard", status_code=302)
    response.set_cookie(
        "khoma_session",
        sid,
        httponly=True,
        secure=COOKIE_SECURE,
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
  function setMetric(id, value, suffix) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    if (suffix !== undefined) el.textContent = value + suffix;
  }
  function render(data) {
    var tp = document.getElementById("totalPnl");
    var lp = document.getElementById("livePnl");
    var lm = document.getElementById("liveMonitor");
    if (!data || !data.connected) {
      if (lp) lp.innerHTML = "<p class='muted'>Connect a Tradovate account to see live PnL.</p>";
      if (lm) lm.innerHTML = "<p class='muted'>No connected account yet. Go to Broker Connect to link your Tradovate accounts.</p>";
      return;
    }
    // Total PnL (live) — straight from Tradovate.
    if (tp && data.totals && data.totals.total_pnl !== null && data.totals.total_pnl !== undefined) {
      tp.textContent = fmt(data.totals.total_pnl);
      tp.className = cls(data.totals.total_pnl);
    }
    // Performance metrics from real closed round-trips — update live.
    var mx = data.metrics || {};
    if (mx.win_rate !== undefined && mx.win_rate !== null) setMetric("winRate", mx.win_rate, "%");
    if (mx.max_drawdown !== undefined && mx.max_drawdown !== null) setMetric("maxDrawdown", "$" + Number(mx.max_drawdown).toFixed(2));
    var wrs = document.getElementById("winRateSub");
    if (wrs && mx.closed_trades !== undefined) wrs.innerHTML = (mx.wins || 0) + " wins • " + (mx.losses || 0) + " losses • " + (mx.closed_trades || 0) + " closed";
    var dds = document.getElementById("drawdownSub");
    if (dds && mx.realized_pnl !== undefined) dds.innerHTML = "Realized: <b>" + fmt(mx.realized_pnl) + "</b> from closed trades.";
    // Per-account PnL — mirrors Tradovate's Open P/L and Total P/L.
    if (lp) {
      var h = "";
      (data.accounts || []).forEach(function (a) {
        h += "<div class='journal-day'><div><b>" + esc(a.name) + "</b><small>" + esc((a.env || "").toUpperCase()) + "</small></div>" +
             "<div style='text-align:right'><b class='" + (Number(a.total_pnl) < 0 ? "bad" : "good") + "'>Total " + fmt(a.total_pnl) + "</b>" +
             "<small>Open " + fmt(a.open_pnl) + " • Realized " + fmt(a.realized_pnl) + "</small></div></div>";
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
    var acct = (window.KHOMA_ACCT || "all");
    fetch("/api/live/monitor?account=" + encodeURIComponent(acct), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }
  poll();
  setInterval(poll, 4000);
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

    # Per-account view: 'all' (combined) or a specific broker_accounts.id.
    sel = request.query_params.get("account", "all") or "all"
    only = None if sel in ("all", "") else sel
    tabs = account_tabs(user["id"], sel, "/dashboard")

    # Fetch real broker round-trips once (scoped to the selected account) and
    # reuse for both metrics + journal.
    try:
        trips, _open = account_trade_history(user["id"], only_account_id=only)
    except Exception:
        trips = []
    m = dashboard_metrics(user["id"], trips=trips)
    journal = daily_journal(user["id"], trips=trips)

    running = user["automation_status"] == "Running"
    state_label = "ON" if running else "OFF"
    state_class = "good" if running else "bad"

    trade_rows = "".join([
        f"<tr><td>{t['ts'][:19]}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t['qty']}</td><td>{t['status']}</td><td>{t['mode']}</td><td>{t['latency_ms']}ms</td></tr>"
        for t in trades
    ]) or "<tr><td colspan='7'>No execution events yet.</td></tr>"

    journal_rows = "".join([
        f"<div class='journal-day'><div><b>{day}</b><small>{vals['executed']} executed • {vals['rejected']} rejected</small></div>"
        f"<div style='text-align:right'><b class='{'good' if vals['pnl']>0 else ('bad' if vals['pnl']<0 else '')}'>${vals['pnl']:.2f}</b><small>realized</small></div></div>"
        for day, vals in journal
    ]) or "<p class='muted'>No journal data yet.</p>"

    start_btn = '<a class="btn secondary" href="/start">Start Automation</a>' if running else '<a class="btn" href="/start">Start Automation</a>'
    pause_btn = '<a class="btn" href="/pause">Pause</a>' if running else '<a class="btn secondary" href="/pause">Pause</a>'

    content = f'''
    <div class="header">
      <div><h2>Execution Dashboard</h2><p>System is <b class="{state_class}">{state_label}</b> · Orders today: <b>{today_order_count(user['id'])}</b></p></div>
      <div>{start_btn}{pause_btn}</div>
    </div>
    {tabs}

    <div class="grid">
      <div class="card span3"><h3>Total PnL (live)</h3><div class="metric good" id="totalPnl">${m['total_pnl']}</div><p class="muted">Live from Tradovate — open + realized.</p></div>
      <div class="card span3"><h3>Win Rate</h3><div class="metric" id="winRate">{m['win_rate']}%</div><p class="muted" id="winRateSub">{m['wins']} wins • {m['losses']} losses • {m['closed_trades']} closed</p></div>
      <div class="card span3"><h3>Max Drawdown</h3><div class="metric warn" id="maxDrawdown">${m['max_drawdown']}</div><p class="muted" id="drawdownSub">Realized: <b>${m['realized_pnl']}</b> from closed trades.</p></div>
      <div class="card span3"><h3>Avg Latency</h3><div class="metric">{m['avg_latency']}ms</div><p class="muted">Cloud routing + broker response.</p></div>

      {dashboard_risk_panel(user['id'])}
      {dashboard_rejected_feed(user['id'])}

      <div class="card span8"><h3>Live Trade Monitor</h3><p class="muted">Open positions from your connected Tradovate accounts. Refreshes automatically.</p><div id="liveMonitor"><p class="muted">Loading live positions…</p></div></div>
      <div class="card span4"><h3>Live Account PnL</h3><p class="muted">Per connected account.</p><div id="livePnl"><p class="muted">Loading…</p></div></div>

      <div class="card span8"><h3>Execution Log</h3><p class="muted">Recent order events routed by KhomaAPI.</p><table><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Mode</th><th>Latency</th></tr>{trade_rows}</table></div>
      <div class="card span4"><h3>Trading Journal</h3><p class="muted">Realized PnL grouped by day (broker fills).</p>{journal_rows}<a class="btn secondary" href="/journal?account={sel}">Open Journal</a></div>
    </div>
    '''
    acct_script = f"<script>window.KHOMA_ACCT={json.dumps(sel)};</script>"
    return layout(content + acct_script + DASHBOARD_LIVE_SCRIPT, user, "dashboard")


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

    def is_copy(a):
        return (a.get("group_type") or "independent") == "copy"

    available_chips = "".join(chip(a) for a in accounts if not is_copy(a))
    box_chips = "".join(chip(a) for a in accounts if is_copy(a))
    connected_count = len(accounts)
    box_count = sum(1 for a in accounts if is_copy(a))
    available_count = connected_count - box_count

    if not accounts:
        zones_html = "<p class='muted'>No accounts connected yet. Click \"Connect with Tradovate\" to log in and import your accounts.</p>"
    else:
        zones_html = f'''
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;">
          <div>
            <h4 style="margin:0 0 6px">Available Accounts <span class="pill gray">INDEPENDENT</span></h4>
            <div id="zone-available" class="dropzone" data-box="0" style="min-height:120px;border:2px dashed #e5e7eb;border-radius:16px;padding:10px;">
              <div class="chips">{available_chips}</div>
              <p class="empty muted" style="text-align:center;padding:18px 0;">Tradable accounts that run independently. Each one executes only alerts routed to it by name, so different strategies can run on different accounts. Drag an account here to remove it from copy trading.</p>
            </div>
          </div>
          <div>
            <h4 style="margin:0 0 6px">Copy Trading Accounts <span class="pill" style="background:#fee2e2;color:#b91c1c;border-color:#fecaca;">LIVE</span></h4>
            <div id="zone-box" class="dropzone" data-box="1" style="min-height:120px;border:2px dashed #cdebd8;border-radius:16px;padding:10px;background:#f7fdf9;">
              <div class="chips">{box_chips}</div>
              <p class="empty muted" style="text-align:center;padding:18px 0;">Every master signal (an alert with no specific account) is mirrored 1:1 across all accounts here as real market orders. Independent accounts are unaffected.</p>
            </div>
          </div>
        </div>
        '''

    content = f'''
    <div class="header"><div><h2>Broker Connection</h2><p>Log in with Tradovate to connect your cash, live, and prop firm accounts, then organize them into Available Accounts (independent execution) or Copy Trading Accounts (mirrored master signals).</p></div></div>
    <div class="grid">
      {banner}
      <div class="card span5"><h3>Connection Status</h3>
        <div class="metric {'good' if connected_count else 'bad'}">{connected_count} Connected</div>
        <p class="muted">{available_count} available (independent) · {box_count} in copy trading. Connect through Tradovate's secure login — KhomaAPI never sees your password.</p>
        <label style="font-size:13px;font-weight:700;">Account type to connect</label>
        <select id="connect-env" style="margin:6px 0 12px;">
          <option value="demo">Demo</option>
          <option value="live">Live</option>
          <option value="prop">Prop Firm</option>
        </select>
        <a class="btn" href="/auth/tradovate/connect?env=demo" onclick="this.href='/auth/tradovate/connect?env='+document.getElementById('connect-env').value;">Connect with Tradovate</a>
        <p class="muted" style="margin-top:8px;font-size:12px;">Pick what you're connecting so the right accounts import and nothing gets traded by accident.</p>
      </div>
      <div class="card span7"><h3>Account Groups</h3>
        <p class="muted">Drag accounts between the two groups. <b>Available Accounts</b> trade independently; <b>Copy Trading Accounts</b> all receive the same master signal. The two systems run simultaneously and never interfere.</p>
        {zones_html}
      </div>
    </div>
    '''
    return layout(content + BROKER_COPY_SCRIPT, user, "broker")


@app.post("/broker/copy/set")
def broker_copy_set(request: Request, account_id: int = Form(...), in_box: str = Form(...)):
    user, _e, deny = gate(request, feature=ent.COPY_TRADING, api=True)
    if deny:
        return deny
    group_type = "copy" if in_box in ("1", "true", "True", "on") else "independent"
    set_account_group(user["id"], account_id, group_type)
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
    user, e, deny = gate(request)
    if deny:
        return deny
    if account_cap_remaining(user, e) == 0:
        return RedirectResponse("/broker?error=plan_limit", status_code=302)

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

    domain = public_base_url(request)
    webhook_url = f"{domain}/webhook/trade"
    example = json.dumps({
        "client_id": user["email"],
        "auth": user["webhook_secret"],
        "symbol": "{{ticker}}",
        "side": "{{strategy.order.action}}",
        "qty": "{{strategy.order.contracts}}",
        "request_id": "{{strategy.order.id}}"
    }, indent=2)

    routed_example = json.dumps({
        "client_id": user["email"],
        "auth": user["webhook_secret"],
        "symbol": "{{ticker}}",
        "side": "{{strategy.order.action}}",
        "qty": "{{strategy.order.contracts}}",
        "request_id": "{{strategy.order.id}}",
        "account": "YourTradovateAccountName"
    }, indent=2)

    # Per-account position sizing.
    accounts = get_broker_accounts(user["id"], connected_only=True)
    sizing_rows = ""
    for a in accounts:
        cq = a.get("contract_qty")
        val = "" if cq in (None, "") else int(cq)
        sizing_rows += (
            f"<tr><td><b>{a['account_name']}</b> <small style='color:#9ca3af'>{(a.get('env') or '').upper()} · {(a.get('group_type') or 'independent').title()}</small></td>"
            f"<td style='text-align:right'><input form='sizing-form' name='qty_{a['id']}' value='{val}' placeholder='N/A (use alert)' "
            f"style='width:160px;display:inline-block;text-align:center;margin:0;'></td></tr>"
        )
    if accounts:
        sizing_card = f'''
      <div class="card span12"><h3>Per-Account Position Sizing</h3>
        <p class="muted">Set a fixed number of contracts to trade on each account — this overrides the alert's <code>qty</code>. Leave blank for <b>N/A</b> (use whatever the TradingView alert sends). Works for both Copy Trading (same signal, different size per account) and Independent accounts.</p>
        <form id="sizing-form" method="post" action="/webhooks/sizing"></form>
        <table><tr><th>Account</th><th style="text-align:right">Contracts (blank = N/A)</th></tr>{sizing_rows}</table>
        <button form="sizing-form" style="margin-top:12px;">Save Position Sizing</button>
      </div>'''
    else:
        sizing_card = '''<div class="card span12"><h3>Per-Account Position Sizing</h3><p class="muted">Connect a Tradovate account on the Broker page to set per-account contract sizes.</p></div>'''

    content = f'''
    <div class="header"><div><h2>TradingView Webhooks</h2><p>Works with ANY TradingView Pine Script strategy — copy this URL and JSON into your alert.</p></div></div>
    <div class="grid">
      <div class="card span12"><h3>Webhook URL</h3><div class="keybox"><span id="webhook-url">{webhook_url}</span><button onclick="copyText('webhook-url')">Copy</button></div></div>
      {sizing_card}
      <div class="card span7"><h3>Dynamic TradingView JSON (master / copy signal)</h3><pre class="codebox" id="json-template">{example}</pre><button onclick="copyText('json-template')">Copy JSON</button><p class="muted">No <code>account</code> field = master signal, mirrored across all Copy Trading Accounts.</p></div>
      <div class="card span5"><h3>Setup Instructions</h3><p class="muted">1. Open any TradingView alert.<br>2. Enable Webhook URL.<br>3. Paste the webhook URL.<br>4. Paste the JSON below.<br>5. Your strategy controls action / qty / symbol automatically.</p><div class="copy-note">Each client uses the same endpoint with a unique client_id + secret. Accounts never intersect between clients.</div></div>
      <div class="card span7"><h3>Route to one Independent account</h3><pre class="codebox" id="json-routed">{routed_example}</pre><button onclick="copyText('json-routed')">Copy JSON</button><p class="muted">Add an <code>account</code> field (exact Tradovate account name) to trade ONLY that Available account — different strategies can target different accounts.</p></div>
      <div class="card span5"><h3>Supported Actions</h3><p class="muted"><b>side</b> accepts: <code>buy</code>, <code>sell</code>, <code>close</code> (flatten), and <code>reverse</code> (flip position). Common Pine synonyms like <code>long</code>, <code>short</code>, <code>strategy.close</code> also work.</p><p class="muted">Any extra strategy-specific fields you include are accepted and ignored — alerts are never rejected for unknown parameters.</p></div>
      <div class="card span12"><h3>Manual Alert Format</h3><p class="muted">For manual alerts, set <b>side</b> to buy, sell, close, or reverse. Symbol can be any exact Tradovate contract symbol such as MNQM6, MESM6, MYMM6, etc.</p></div>
    </div>
    '''
    return layout(content, user, "webhooks")


@app.post("/webhooks/sizing")
async def webhooks_sizing_save(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    for a in get_broker_accounts(user["id"], connected_only=True):
        key = f"qty_{a['id']}"
        if key in form:
            set_account_qty(user["id"], a["id"], (form.get(key) or "").strip())
    return RedirectResponse("/webhooks", status_code=302)


def dashboard_risk_panel(user_id: int) -> str:
    """Compact per-account risk status for the dashboard: ACTIVE / LOCKED /
    DISCONNECTED, today's PnL vs daily limit, and trailing-DD headroom."""
    accounts = get_broker_accounts(user_id, connected_only=True)
    if not accounts:
        return ""
    rows = ""
    for a in accounts:
        cfg = _maybe_auto_unlock(ensure_risk_config(a["id"], user_id))
        conn = account_connectivity(a)
        if conn == "disconnected":
            badge = "<span class='pill gray' style='background:#fff7ed;color:#c2410c;border-color:#fed7aa;'>DISCONNECTED</span>"
        elif cfg.get("locked"):
            badge = "<span class='pill' style='background:#fee2e2;color:#b91c1c;border-color:#fecaca;'>LOCKED</span>"
        else:
            badge = "<span class='pill'>ACTIVE</span>"
        state = ACCOUNT_STATE_CACHE.get(a["id"], (None,))[0] or {}
        eff = effective_risk_cfg(cfg)
        detail = ""
        # Evaluation progress toward profit goal.
        if (cfg.get("account_phase") or "evaluation") == "evaluation":
            goal = risk._num(cfg.get("profit_goal"))
            evp = eval_profit_value(cfg, state)
            if goal and evp is not None:
                pct = max(0, min(100, (evp / goal) * 100))
                detail += (f"<small>Goal ${goal:,.0f}: <b class='{'good' if evp>=0 else 'bad'}'>${evp:,.0f}</b> ({pct:.0f}%)</small>"
                           f"{_progress_bar(pct, '#0f8f45')}")
        dll = risk._num(eff.get("daily_loss_limit"))
        day_pnl = state.get("day_pnl")
        if dll and day_pnl is not None:
            cls = "bad" if day_pnl < 0 else "good"
            detail += f"<small>PnL <b class='{cls}'>${day_pnl:,.0f}</b> / -${dll:,.0f}</small> "
        tdd = risk._num(eff.get("trailing_dd"))
        hwm = cfg.get("high_water_mark")
        eq = state.get("equity")
        if tdd and hwm is not None and eq is not None:
            detail += f"<small>DD headroom <b>${max(0, tdd-(hwm-eq)):,.0f}</b></small>"
        if cfg.get("locked"):
            detail = f"<small class='bad'>{cfg.get('locked_reason') or ''}</small>"
        rows += (f"<div class='journal-day'><div><b>{a['account_name']}</b> "
                 f"<small style='color:#9ca3af'>{(a.get('env') or '').upper()}</small></div>"
                 f"<div style='text-align:right'>{badge}<br>{detail}</div></div>")
    return (f"<div class='card span6'><h3>Risk Status</h3>"
            f"<p class='muted'>Live per-account enforcement state.</p>{rows}"
            f"<a class='btn secondary' href='/risk' style='margin-top:10px;'>Open Risk Engine</a></div>")


def dashboard_rejected_feed(user_id: int) -> str:
    """Recent alerts the risk engine blocked or locked — shows the engine working."""
    con = db()
    rows = con.execute(
        "SELECT * FROM trades WHERE user_id=? AND (status='RISK_LOCK' OR (status='REJECTED' AND message LIKE 'Risk:%')) "
        "ORDER BY id DESC LIMIT 8",
        (user_id,),
    ).fetchall()
    con.close()
    items = "".join(
        f"<div class='journal-day'><div><b>{r['symbol']}</b> <small>{(r['ts'] or '')[:19].replace('T',' ')}</small></div>"
        f"<div style='text-align:right;max-width:60%'><small class='bad'>{(r['message'] or '')[:120]}</small></div></div>"
        for r in rows
    ) or "<p class='muted'>No risk-blocked alerts yet.</p>"
    return f"<div class='card span6'><h3>Risk-Blocked Alerts</h3><p class='muted'>Alerts the engine stopped before they hit the broker.</p>{items}</div>"


def load_risk_presets() -> list:
    try:
        with open(os.path.join(BASE_DIR, "app", "risk_presets.yaml")) as f:
            return (yaml.safe_load(f) or {}).get("presets", [])
    except Exception:
        return []


def _rv(cfg, key):
    v = cfg.get(key)
    return "" if v in (None, "") else v


def _progress_bar(used_pct: float, color: str) -> str:
    pct = max(0, min(100, used_pct))
    return (f"<div style='height:10px;background:#eef0ef;border-radius:999px;overflow:hidden;margin:6px 0;'>"
            f"<div style='height:100%;width:{pct:.0f}%;background:{color};'></div></div>")


def risk_account_card(user, a, cfg, state, pf=None) -> str:
    aid = a["id"]
    name = a["account_name"]
    env = (a.get("env") or "").upper()
    conn = account_connectivity(a)
    locked = bool(cfg.get("locked"))
    phase = (cfg.get("account_phase") or "standard")
    passed = bool(cfg.get("eval_passed"))
    eff = effective_risk_cfg(cfg)

    # Status badge
    if conn == "disconnected":
        status = "<span class='pill gray' style='background:#fff7ed;color:#c2410c;border-color:#fed7aa;'>● DISCONNECTED</span>"
    elif locked:
        status = "<span class='pill' style='background:#fee2e2;color:#b91c1c;border-color:#fecaca;'>● LOCKED</span>"
    else:
        status = "<span class='pill'>● ACTIVE</span>"

    if phase == "funded":
        phase_badge = "<span class='pill'>FUNDED</span>"
    elif phase == "evaluation" and passed:
        phase_badge = "<span class='pill' style='background:#ecfdf5;color:#047857;border-color:#a7f3d0;'>✓ EVAL PASSED</span>"
    elif phase == "evaluation":
        phase_badge = "<span class='pill gray'>EVALUATION</span>"
    else:
        env_label = (a.get("env") or "live").upper()
        phase_badge = f"<span class='pill gray'>{env_label}</span>"

    lock_line = ""
    if locked:
        lock_line = (f"<p class='bad' style='margin:6px 0;'><b>Locked:</b> {cfg.get('locked_reason') or ''}</p>"
                     f"<form method='post' action='/risk/account/{aid}/unlock' style='margin:0 0 10px;'>"
                     f"<button class='secondary' style='padding:7px 12px;'>Unlock now</button></form>")

    # Evaluation progress + live status
    live = ""
    eq = state.get("equity") if state else None
    if phase == "evaluation":
        goal = risk._num(cfg.get("profit_goal"))
        ev_profit = eval_profit_value(cfg, state) if state else None
        if goal:
            if ev_profit is not None:
                pct = (ev_profit / goal) * 100
                live += (f"<p class='muted' style='margin:10px 0 0;'>Evaluation progress: "
                         f"<b class='{'good' if ev_profit>=0 else 'bad'}'>${ev_profit:,.2f}</b> / goal ${goal:,.0f} "
                         f"<b>({max(0,pct):.0f}%)</b></p>{_progress_bar(pct, '#0f8f45')}")
            else:
                live += (f"<p class='muted' style='margin:10px 0 0;'>Evaluation goal ${goal:,.0f} — "
                         f"progress starts once live balance is read.</p>{_progress_bar(0, '#0f8f45')}")
    pf_txt = (f"{pf}" if pf is not None else "—")
    pft = risk._num(cfg.get("profit_factor_target"))
    if pft or pf is not None:
        live += f"<p class='muted' style='margin:8px 0 0;'>Profit factor: <b>{pf_txt}</b>{f' / target {pft:g}' if pft else ''}</p>"

    # Live PnL vs the IN-FORCE limits (phase-aware).
    if state and state.get("ok"):
        dll = risk._num(eff.get("daily_loss_limit"))
        day_pnl = state.get("day_pnl")
        if dll and day_pnl is not None:
            used = (-day_pnl / dll) * 100 if day_pnl < 0 else 0
            cls = "bad" if day_pnl < 0 else "good"
            live += (f"<p class='muted' style='margin:10px 0 0;'>Today's PnL: <b class='{cls}'>${day_pnl:,.2f}</b> "
                     f"/ limit -${dll:,.0f}</p>{_progress_bar(used, '#dc2626')}")
        tdd = risk._num(eff.get("trailing_dd"))
        hwm = cfg.get("high_water_mark")
        if tdd and hwm is not None and eq is not None:
            dd = max(0, hwm - eq)
            used = (dd / tdd) * 100
            live += (f"<p class='muted' style='margin:10px 0 0;'>Drawdown from peak: <b>${dd:,.2f}</b> / ${tdd:,.0f} "
                     f"(headroom ${max(0, tdd-dd):,.2f})</p>{_progress_bar(used, '#ca8a04')}")
    elif conn == "disconnected":
        live += "<p class='muted' style='margin:10px 0 0;'>No live data — account is disconnected.</p>"

    f = lambda k: _rv(cfg, k)
    checked = "" if str(cfg.get("enabled", 1)) in ("0", "False", "false") else "checked"
    basis = cfg.get("trailing_basis") or "intraday"

    # Funded section (unlocked once the evaluation is passed).
    funded_section = ""
    if passed or phase == "funded":
        fchecked = "checked" if phase == "funded" else ""
        funded_section = f'''
        <div style="margin:16px 0 6px;padding:14px;border:1px solid #a7f3d0;background:#f0fdf4;border-radius:14px;">
          <b style="color:#047857;">🎉 Funded account</b>
          <p class="muted" style="margin:4px 0 10px;">You passed the evaluation. Set your funded rules — buffer zone locks you that many dollars BEFORE the firm's hard max loss.</p>
          <div class="formgrid">
            <div><label>Buffer zone ($)</label><input name="buffer_zone" value="{f('buffer_zone')}" placeholder="e.g. 500"></div>
            <div><label>Funded max loss ($)</label><input name="funded_max_loss" value="{f('funded_max_loss')}" placeholder="e.g. 3000"></div>
            <div><label>Funded daily loss ($)</label><input name="funded_daily_loss" value="{f('funded_daily_loss')}" placeholder="e.g. 1500"></div>
          </div>
          <label style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px;margin-top:8px;"><input type="checkbox" name="activate_funded" value="1" {fchecked} style="width:auto;margin:0;"> Activate funded mode (enforce funded rules)</label>
        </div>
        '''

    return f'''
    <div class="card span6" data-acct="{aid}">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;">
        <h3 style="margin:0;">{name} <small style="color:#9ca3af;font-weight:600;">{env}</small></h3>
        <div style="display:flex;gap:6px;">{phase_badge}{status}</div>
      </div>
      {lock_line}{live}
      <form method="post" action="/risk/account/{aid}/save" style="margin-top:14px;">
        <label style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px;"><input type="checkbox" name="enabled" value="1" {checked} style="width:auto;margin:0;"> Risk enforcement enabled</label>
        <div class="formgrid" style="margin-top:10px;">
          <div><label>Account type</label><select name="account_phase">
            <option value="standard" {"selected" if phase=="standard" else ""}>Standard ({env or 'LIVE'})</option>
            <option value="evaluation" {"selected" if phase=="evaluation" else ""}>Prop Evaluation</option>
            <option value="funded" {"selected" if phase=="funded" else ""}>Prop Funded</option>
          </select></div>
          <div><label>Prop firm</label><input name="prop_firm" value="{f('prop_firm')}" placeholder="e.g. Apex, TPT"></div>
        </div>
        <div class="formgrid">
          <div><label>Load preset</label><select onchange="applyPreset({aid}, this.value)"><option value="">— template —</option>{"".join(f'<option value="{p["id"]}">{p["name"]}</option>' for p in load_risk_presets())}</select></div>
          <div></div>
        </div>

        <h4 style="margin:14px 0 0;">Evaluation goal <small class="muted" style="font-weight:500;">— used in Prop Evaluation</small></h4>
        <div class="formgrid">
          <div><label>Profit goal ($)</label><input name="profit_goal" value="{f('profit_goal')}" placeholder="e.g. 3000"></div>
          <div><label>Profit factor target</label><input name="profit_factor_target" value="{f('profit_factor_target')}" placeholder="e.g. 1.5"></div>
        </div>

        <h4 style="margin:14px 0 0;">Risk limits</h4>
        <div class="formgrid">
          <div><label>Daily loss limit ($)</label><input name="daily_loss_limit" value="{f('daily_loss_limit')}" placeholder="e.g. 1000"></div>
          <div><label>Max drawdown ($)</label><input name="trailing_dd" value="{f('trailing_dd')}" placeholder="e.g. 2000"></div>
          <div><label>Drawdown basis</label><select name="trailing_basis"><option value="intraday" {"selected" if basis=="intraday" else ""}>Intraday equity</option><option value="closed" {"selected" if basis=="closed" else ""}>Closed balance</option></select></div>
          <div><label>Intraday drawdown ($)</label><input name="intraday_dd" value="{f('intraday_dd')}" placeholder="give-back from today's peak, e.g. 800"></div>
          <div><label>Consistency rule (%)</label><input name="consistency_pct" value="{f('consistency_pct')}" placeholder="max % of total profit in one day, e.g. 50"></div>
          <div><label>Profit auto-stop ($, optional)</label><input name="profit_target" value="{f('profit_target')}" placeholder="optional"></div>
        </div>

        {funded_section}

        <h4 style="margin:14px 0 0;">Position &amp; time guards</h4>
        <div class="formgrid">
          <div><label>Max position (contracts)</label><input name="max_position" value="{f('max_position')}"></div>
          <div><label>Max contracts / order</label><input name="max_contracts_per_order" value="{f('max_contracts_per_order')}"></div>
          <div><label>Max open positions</label><input name="max_open_positions" value="{f('max_open_positions')}"></div>
          <div><label>Daily trade cap</label><input name="daily_trade_cap" value="{f('daily_trade_cap')}"></div>
          <div><label>Trading hours start (HH:MM)</label><input name="hours_start" value="{f('hours_start')}" placeholder="09:30"></div>
          <div><label>Trading hours end (HH:MM)</label><input name="hours_end" value="{f('hours_end')}" placeholder="16:00"></div>
          <div><label>Timezone</label><input name="tz" value="{cfg.get('tz') or 'America/New_York'}"></div>
          <div><label>Session reset hour (local)</label><input name="reset_hour" value="{cfg.get('reset_hour') or 17}"></div>
        </div>
        <button>Save rules</button>
        <button formaction="/risk/account/{aid}/flatten-lock" class="danger" formnovalidate onclick="return confirm('Flatten ALL positions on {name} and lock it now?');">Flatten &amp; Lock</button>
      </form>
    </div>
    '''


RISK_PRESET_SCRIPT = """
<script>
window.KHOMA_PRESETS = %s;
function applyPreset(aid, pid){
  if(!pid) return;
  var p = (window.KHOMA_PRESETS||[]).find(function(x){return x.id===pid;});
  if(!p) return;
  if(p.note) { /* show note */ }
  var card = document.querySelector('[data-acct="'+aid+'"]');
  if(!card) return;
  var fields = p.fields||{};
  Object.keys(fields).forEach(function(k){
    var el = card.querySelector('[name="'+k+'"]');
    if(el){ el.value = fields[k]; }
  });
  alert((p.note||'Preset loaded.') + '\\n\\nThese are STARTING values — verify them against your firm\\'s current rules, then Save.');
}
</script>
"""


@app.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    accounts = get_broker_accounts(user["id"], connected_only=True)
    # Profit factor per account from real broker round-trips (fetched once).
    pf_by_name = {}
    try:
        trips, _open = account_trade_history(user["id"])
        by_name = {}
        for t in trips:
            by_name.setdefault(t.get("account"), []).append(t)
        pf_by_name = {n: profit_factor_from_trips(ts) for n, ts in by_name.items()}
    except Exception:
        pf_by_name = {}

    cards = ""
    for a in accounts:
        cfg = _maybe_auto_unlock(ensure_risk_config(a["id"], user["id"]))
        try:
            state = _cached_state(a, cfg)
        except Exception:
            state = {"ok": False}
        cards += risk_account_card(user, a, cfg, state, pf=pf_by_name.get(a["account_name"]))
    if not accounts:
        cards = "<div class='card span12'><p class='muted'>Connect a Tradovate account on the Broker page to configure per-account risk rules.</p></div>"

    # News / lockout windows
    con = db()
    nws = con.execute("SELECT * FROM news_windows WHERE user_id=? ORDER BY starts_at", (user["id"],)).fetchall()
    con.close()
    news_rows = "".join(
        f"<tr><td>{(n['starts_at'] or '')[:16].replace('T',' ')}</td><td>{(n['ends_at'] or '')[:16].replace('T',' ')}</td>"
        f"<td>{n['label'] or ''}</td><td><form method='post' action='/risk/news/{n['id']}/delete' style='margin:0'><button class='secondary' style='padding:5px 10px;margin:0'>Remove</button></form></td></tr>"
        for n in nws
    ) or "<tr><td colspan='4' class='muted'>No lockout windows set.</td></tr>"

    presets_json = json.dumps(load_risk_presets())
    content = f'''
    <div class="header"><div><h2>Risk Engine</h2><p>Account-survival enforcement. Set your own limits per account — KhomaAPI enforces them server-side, in real time, against your live Tradovate balance, and flattens + locks an account the instant a hard limit is breached.</p></div>
      <form method="post" action="/risk/flatten-all" onsubmit="return confirm('FLATTEN ALL positions across every connected account and lock them now?');"><button class="danger">⚠ Flatten All &amp; Lock</button></form>
    </div>

    <div class="card span12" style="background:#fffbeb;border-color:#fde68a;">
      <p class="muted" style="margin:0;">⚖️ Presets are <b>starting templates</b>, not a compliance guarantee. KhomaAPI enforces exactly the values you save here against your live account — always verify them against your prop firm's current rules.</p>
    </div>

    <div class="grid">{cards}</div>

    <div class="card span12" style="margin-top:22px;">
      <h3>News / Manual Lockout Windows</h3>
      <p class="muted">During these windows, all opening alerts are rejected (closing is still allowed). Times are UTC.</p>
      <form method="post" action="/risk/news/add" class="formgrid" style="align-items:end;">
        <div><label>Start (UTC)</label><input type="datetime-local" name="starts_at" required></div>
        <div><label>End (UTC)</label><input type="datetime-local" name="ends_at" required></div>
        <div><label>Label</label><input name="label" placeholder="e.g. FOMC"></div>
        <div><button>Add window</button></div>
      </form>
      <table><tr><th>Start</th><th>End</th><th>Label</th><th></th></tr>{news_rows}</table>
    </div>

    <div class="card span12">
      <h3>Account-wide safety (legacy)</h3>
      <form method="post" action="/risk/save"><div class="formgrid">
        <div><label>Max Orders Per Day</label><input name="max_orders" value="{user['max_orders']}"></div>
        <div><label>Duplicate Lock Seconds</label><input name="duplicate_seconds" value="{user['duplicate_seconds']}"></div>
        <div><label>Max Rejections Per Day</label><input name="max_rejections_per_day" value="{user['max_rejections_per_day']}"></div>
      </div><button>Save</button></form>
    </div>
    '''
    return layout(content + (RISK_PRESET_SCRIPT % presets_json), user, "risk")


def _risk_owns(user, account_id):
    con = db()
    row = con.execute("SELECT * FROM broker_accounts WHERE id=? AND user_id=?", (account_id, user["id"])).fetchone()
    con.close()
    return dict(row) if row else None


@app.post("/risk/account/{account_id}/save")
async def risk_account_save(request: Request, account_id: int):
    user, e, deny = gate(request)
    if deny:
        return deny
    if not _risk_owns(user, account_id):
        return RedirectResponse("/risk")
    form = await request.form()

    # Eval→funded phase tracking + prop presets are a Pro/Elite/Founder feature.
    # Solo still gets the core risk engine (daily loss, drawdown, kill-switch),
    # just pinned to the 'standard' phase.
    eval_allowed = e.has(ent.EVAL_FUNDED)

    def numf(key):
        v = (form.get(key) or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    existing = get_risk_config(account_id)
    # Account type is chosen explicitly (standard / evaluation / funded). The
    # "Activate funded" toggle in the funded section is a shortcut to 'funded'.
    phase = (form.get("account_phase") or "standard").lower()
    if phase not in ("standard", "evaluation", "funded"):
        phase = "standard"
    if form.get("activate_funded"):
        phase = "funded"
    # Plan gate: only Pro+ may use evaluation/funded phases.
    if phase in ("evaluation", "funded") and not eval_allowed:
        phase = "standard"

    values = {
        "enabled": 1 if form.get("enabled") else 0,
        "prop_firm": (form.get("prop_firm") or "").strip(),
        "account_phase": phase,
        "profit_goal": numf("profit_goal"),
        "profit_factor_target": numf("profit_factor_target"),
        "daily_loss_limit": numf("daily_loss_limit"),
        "trailing_dd": numf("trailing_dd"),
        "trailing_basis": "closed" if form.get("trailing_basis") == "closed" else "intraday",
        "intraday_dd": numf("intraday_dd"),
        "consistency_pct": numf("consistency_pct"),
        "profit_target": numf("profit_target"),
        "buffer_zone": numf("buffer_zone"),
        "funded_daily_loss": numf("funded_daily_loss"),
        "funded_max_loss": numf("funded_max_loss"),
        "max_position": numf("max_position"),
        "max_contracts_per_order": numf("max_contracts_per_order"),
        "max_open_positions": numf("max_open_positions"),
        "daily_trade_cap": numf("daily_trade_cap"),
        "hours_start": (form.get("hours_start") or "").strip(),
        "hours_end": (form.get("hours_end") or "").strip(),
        "tz": (form.get("tz") or "America/New_York").strip(),
        "reset_hour": int(numf("reset_hour") or 17),
    }
    save_risk_config(account_id, user["id"], values)
    # If the profit goal changed, re-baseline the evaluation progress.
    if numf("profit_goal") != (risk._num(existing.get("profit_goal"))) and not existing.get("eval_passed"):
        con = db()
        con.execute("UPDATE account_risk_config SET eval_start_balance=NULL WHERE account_id=?", (account_id,))
        con.commit()
        con.close()
    return RedirectResponse("/risk", status_code=302)


@app.post("/risk/account/{account_id}/flatten-lock")
def risk_account_flatten_lock(request: Request, account_id: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    a = _risk_owns(user, account_id)
    if a:
        flatten_and_lock_account(a, "Manually flattened & locked by user")
    return RedirectResponse("/risk", status_code=302)


@app.post("/risk/account/{account_id}/unlock")
def risk_account_unlock(request: Request, account_id: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    if _risk_owns(user, account_id):
        set_account_lock(account_id, False, "", "")
    return RedirectResponse("/risk", status_code=302)


@app.post("/risk/flatten-all")
def risk_flatten_all(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    for a in get_broker_accounts(user["id"], connected_only=True):
        flatten_and_lock_account(a, "Global Flatten All & Lock by user")
    return RedirectResponse("/risk", status_code=302)


@app.post("/risk/news/add")
async def risk_news_add(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    starts, ends = (form.get("starts_at") or "").strip(), (form.get("ends_at") or "").strip()
    if starts and ends:
        # datetime-local has no tz -> treat as UTC.
        con = db()
        con.execute(
            "INSERT INTO news_windows(user_id,account_id,starts_at,ends_at,label,created_at) VALUES(?,?,?,?,?,?)",
            (user["id"], None, starts + ":00+00:00" if len(starts) == 16 else starts,
             ends + ":00+00:00" if len(ends) == 16 else ends, (form.get("label") or "").strip(),
             datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
    return RedirectResponse("/risk", status_code=302)


@app.post("/risk/news/{nid}/delete")
def risk_news_delete(request: Request, nid: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    con = db()
    con.execute("DELETE FROM news_windows WHERE id=? AND user_id=?", (nid, user["id"]))
    con.commit()
    con.close()
    return RedirectResponse("/risk", status_code=302)


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
    <div class="card"><h3>Execution Log</h3><p class="muted">Every order request KhomaAPI routed — executed, rejected, and unrouted.</p><table><tr><th>Time</th><th>Request ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Mode</th><th>Status</th><th>Latency</th><th>Message</th></tr>{rows}</table></div>
    '''
    return layout(content, user, "logs")


def _money(v, dec=2):
    if v is None:
        return "—"
    if v == float("inf"):
        return "∞"
    return ("-$" if v < 0 else "$") + f"{abs(v):,.{dec}f}"


def _pnl_cls(v):
    return "good" if (v or 0) > 0 else ("bad" if (v or 0) < 0 else "")


def journal_calendar_html(daily: dict, y: int, m: int) -> str:
    """Tradezella-style monthly P&L heatmap: green/red day cells + weekly totals."""
    import calendar as _cal
    cal = _cal.Calendar(firstweekday=6)  # Sunday-first
    head = "".join(f"<th style='font-size:11px;color:#9ca3af;padding:6px;'>{d}</th>"
                   for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Week"])
    body = ""
    for week in cal.monthdayscalendar(y, m):
        cells = ""
        wk_total = 0.0
        wk_has = False
        for day in week:
            if day == 0:
                cells += "<td style='padding:3px;'></td>"
                continue
            key = f"{y}-{m:02d}-{day:02d}"
            pnl = daily.get(key)
            if pnl is None:
                bg, txt, val = "#f8faf9", "#9ca3af", ""
            else:
                wk_total += pnl; wk_has = True
                if pnl > 0:
                    bg, txt = "#eafaf0", "#0f8f45"
                elif pnl < 0:
                    bg, txt = "#fdeaea", "#b91c1c"
                else:
                    bg, txt = "#f3f4f6", "#6b7280"
                val = f"<div style='font-size:12px;font-weight:800;'>{_money(pnl,0)}</div>"
            cells += (f"<td style='padding:3px;vertical-align:top;'>"
                      f"<div style='background:{bg};border-radius:10px;min-height:54px;padding:6px;'>"
                      f"<div style='font-size:11px;color:#9ca3af;'>{day}</div>"
                      f"<div style='color:{txt};'>{val}</div></div></td>")
        wk_cell = ""
        if wk_has:
            wk_cell = (f"<div style='background:#f1fbf5;border:1px solid #cdebd8;border-radius:10px;min-height:54px;padding:6px;'>"
                       f"<div style='font-size:10px;color:#9ca3af;'>Total</div>"
                       f"<div style='font-weight:800;font-size:12px;' class='{_pnl_cls(wk_total)}'>{_money(wk_total,0)}</div></div>")
        cells += f"<td style='padding:3px;vertical-align:top;'>{wk_cell}</td>"
        body += f"<tr>{cells}</tr>"
    return f"<table style='width:100%;border-collapse:collapse;'><tr>{head}</tr>{body}</table>"


def journal_bars_html(bucket: dict, label_fn, order="label") -> str:
    """Horizontal P&L bars for a breakdown (by symbol / weekday / hour / side)."""
    if not bucket:
        return "<p class='muted'>No data.</p>"
    maxabs = max((abs(e["pnl"]) for e in bucket.values()), default=1) or 1
    if order == "pnl":
        items = sorted(bucket.items(), key=lambda x: -x[1]["pnl"])
    else:
        items = sorted(bucket.items(), key=lambda x: x[0])
    rows = ""
    for k, e in items:
        w = abs(e["pnl"]) / maxabs * 100
        color = "#16a34a" if e["pnl"] >= 0 else "#dc2626"
        rows += (f"<div style='display:flex;align-items:center;gap:10px;margin:7px 0;'>"
                 f"<div style='width:74px;font-size:13px;font-weight:700;'>{label_fn(k)}</div>"
                 f"<div style='flex:1;height:18px;background:#f1f3f2;border-radius:6px;overflow:hidden;'>"
                 f"<div style='height:100%;width:{w:.0f}%;background:{color};'></div></div>"
                 f"<div style='width:92px;text-align:right;font-size:13px;font-weight:800;' class='{_pnl_cls(e['pnl'])}'>{_money(e['pnl'],0)}</div>"
                 f"<div style='width:62px;text-align:right;font-size:11px;color:#9ca3af;'>{e['n']}t · {e['win_rate']}%</div>"
                 f"</div>")
    return rows


_WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@app.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    sel = request.query_params.get("account", "all") or "all"
    only = None if sel in ("all", "") else sel
    tabs = account_tabs(user["id"], sel, "/journal")

    try:
        all_trips, _open = account_trade_history(user["id"], only_account_id=only)
    except Exception as e:
        # Last-resort: still show the permanent ledger so the journal is never blank.
        print(f"journal_page: account_trade_history failed, falling back to ledger: {e}")
        try:
            all_trips = _ledger_merge(user["id"], [], only)
        except Exception:
            all_trips = []

    # Date-range filter: presets (today/7d/30d/month/ytd/all) or custom from/to.
    rng = (request.query_params.get("range") or "all").lower()
    q_from = request.query_params.get("from", "")
    q_to = request.query_params.get("to", "")
    et = ZoneInfo(_ET)
    now_et = datetime.now(et)
    start_utc = end_utc = None
    if rng == "today":
        start_utc = now_et.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    elif rng == "7d":
        start_utc = now_et - timedelta(days=7)
    elif rng == "30d":
        start_utc = now_et - timedelta(days=30)
    elif rng == "month":
        start_utc = now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    elif rng == "ytd":
        start_utc = now_et.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    elif rng == "custom":
        try:
            if q_from:
                start_utc = datetime.strptime(q_from, "%Y-%m-%d").replace(tzinfo=et).astimezone(timezone.utc)
            if q_to:
                end_utc = (datetime.strptime(q_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=et)).astimezone(timezone.utc)
        except Exception:
            pass

    if start_utc is not None or end_utc is not None:
        s_utc = start_utc or datetime(1970, 1, 1, tzinfo=timezone.utc)
        e_utc = end_utc or datetime.now(timezone.utc)
        trips = _trips_in_range(all_trips, s_utc, e_utc)
    else:
        trips = all_trips

    # Range selector bar.
    def rbtn(val, label):
        cls = "btn" if rng == val else "btn secondary"
        return f'<a class="{cls}" style="padding:7px 13px;margin:0 6px 6px 0;" href="/journal?account={sel}&range={val}">{label}</a>'
    range_bar = (
        '<div class="card span12" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;">'
        + rbtn("today", "Today") + rbtn("7d", "7D") + rbtn("30d", "30D") + rbtn("month", "This Month")
        + rbtn("ytd", "YTD") + rbtn("all", "All")
        + f'<form method="get" action="/journal" style="display:flex;gap:6px;align-items:center;margin:0 0 0 8px;">'
        + f'<input type="hidden" name="account" value="{sel}"><input type="hidden" name="range" value="custom">'
        + f'<input type="date" name="from" value="{q_from}" style="width:auto;margin:0;">'
        + f'<span class="muted">→</span><input type="date" name="to" value="{q_to}" style="width:auto;margin:0;">'
        + f'<button style="margin:0;">Apply</button></form></div>'
    )

    tmap = get_trip_journal_map(user["id"])
    s = journal_analytics(trips, tags_map=tmap)
    # Override daily PnL / net / calendar / equity curve with PERSISTED history
    # (real per-day PnL), so past days aren't $0 from Tradovate's missing fills.
    _sd = start_utc.astimezone(et).strftime("%Y-%m-%d") if start_utc else None
    _ed = end_utc.astimezone(et).strftime("%Y-%m-%d") if end_utc else None
    s = apply_persisted_pnl(s, user["id"], _sd, _ed, only)

    # Calendar month: ?month=YYYY-MM, else month of latest trade, else now (ET).
    month_q = request.query_params.get("month", "")
    if month_q and re.match(r"^\d{4}-\d{2}$", month_q):
        cy, cm = int(month_q[:4]), int(month_q[5:7])
    elif s["daily"]:
        latest = max(s["daily"].keys())
        cy, cm = int(latest[:4]), int(latest[5:7])
    else:
        now_et = datetime.now(ZoneInfo(_ET))
        cy, cm = now_et.year, now_et.month
    prev_m = (date(cy, cm, 1) - timedelta(days=1)).strftime("%Y-%m")
    next_m = (date(cy, cm, 28) + timedelta(days=7)).strftime("%Y-%m")
    month_label = date(cy, cm, 1).strftime("%B %Y")

    # Metric cards
    def card(title, value, sub="", cls=""):
        return (f"<div class='card span3'><h3>{title}</h3>"
                f"<div class='metric {cls}'>{value}</div><p class='muted'>{sub}</p></div>")

    pf = s["profit_factor"]
    pf_disp = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
    cards = (
        card("Net P&L", _money(s["net"]), f"{s['n']} trades · {s['green_days']}G/{s['red_days']}R days", _pnl_cls(s["net"]))
        + card("Win Rate", f"{s['win_rate']}%", f"{s['wins']}W · {s['losses']}L")
        + card("Profit Factor", pf_disp, "gross profit ÷ gross loss")
        + card("Expectancy", _money(s["expectancy"]), "avg $ per trade", _pnl_cls(s["expectancy"]))
        + card("Avg Win", _money(s["avg_win"]), f"largest {_money(s['largest_win'],0)}", "good")
        + card("Avg Loss", _money(-s["avg_loss"]), f"largest {_money(s['largest_loss'],0)}", "bad")
        + card("Avg Hold", f"{s['avg_hold']:.0f}m", f"win/loss ratio {s['win_loss_ratio'] if s['win_loss_ratio'] is not None else '—'}")
        + card("Streaks", f"{s['max_win_streak']}W / {s['max_loss_streak']}L", f"avg day {_money(s['avg_daily'],0)}")
    )

    equity_svg = chart_svg(s["equity"]) if s["equity"] else "<p class='muted'>No closed trades yet.</p>"
    best = (f"{s['best_day'][0]} ({_money(s['best_day'][1],0)})" if s["best_day"] else "—")
    worst = (f"{s['worst_day'][0]} ({_money(s['worst_day'][1],0)})" if s["worst_day"] else "—")

    # Recent closed trades (scoped) — with tags + notes + screenshot editor.
    def _trow(t):
        k = trip_key(t)
        jr = tmap.get(k, {})
        tag_chips = "".join(
            f"<span style='display:inline-block;background:#eef2ff;color:#3730a3;border-radius:8px;padding:1px 8px;margin:1px 3px 1px 0;font-size:11px;font-weight:700;'>{tg}</span>"
            for tg in parse_tags(jr.get("tags"))
        )
        marks = ("📝" if (jr.get("note") or "").strip() else "") + ("🖼" if jr.get("image_path") else "")
        return (
            f"<tr><td>{str(t.get('closed_at') or '')[:16].replace('T',' ')}</td><td><b>{t['symbol']}</b></td><td>{t.get('account','')}</td>"
            f"<td>{t['side']}</td><td>{t['qty']}</td><td>{t['entry_price']}</td><td>{t['exit_price']}</td>"
            f"<td class=\"{_pnl_cls(t['pnl'])}\">{_money(t['pnl'])}</td>"
            f"<td>{tag_chips}{marks}</td>"
            f"<td><a class='btn secondary' style='padding:5px 10px;margin:0;' href='/journal/trip/{k}?account={sel}'>{'Edit' if jr else 'Tag / Note'}</a></td></tr>"
        )
    trip_rows = "".join(_trow(t) for t in sorted(trips, key=lambda x: str(x.get('closed_at','')), reverse=True)[:80]) \
        or "<tr><td colspan='10'>No closed trades yet.</td></tr>"

    tag_card = ""
    if s.get("by_tag"):
        tag_card = f'''<div class="card span6"><h3>By Tag / Setup</h3>{journal_bars_html(s['by_tag'], lambda k: k, order='pnl')}</div>'''

    content = f'''
    <div class="header"><div><h2>KhomaTradingJournal</h2><p>Full performance analytics from your real Tradovate fills — equity curve, P&amp;L calendar, profit factor, expectancy and breakdowns.</p></div></div>
    {tabs}
    <div class="grid">
      {range_bar}
      {cards}

      <div class="card span8"><h3>Equity Curve</h3><p class="muted">Cumulative daily realized P&amp;L over time.</p><div class="equity-wrap">{equity_svg}</div></div>
      <div class="card span4"><h3>Day Performance</h3>
        <p class="muted">Winning days <b class="good">{s['green_days']}</b> · Losing days <b class="bad">{s['red_days']}</b> · Day win rate <b>{s['day_win_rate']}%</b></p>
        <div class="journal-day"><div><b>Best day</b></div><div class="good">{best}</div></div>
        <div class="journal-day"><div><b>Worst day</b></div><div class="bad">{worst}</div></div>
        <div class="journal-day"><div><b>Avg daily P&amp;L</b></div><div class="{_pnl_cls(s['avg_daily'])}">{_money(s['avg_daily'])}</div></div>
        <div class="journal-day"><div><b>Gross profit / loss</b></div><div><span class="good">{_money(s['gross_profit'],0)}</span> / <span class="bad">{_money(-s['gross_loss'],0)}</span></div></div>
      </div>

      <div class="card span8"><h3>P&amp;L Calendar — {month_label}</h3>
        <div style="float:right;margin-top:-34px;">
          <a class="btn secondary" style="padding:6px 11px;" href="/journal?account={sel}&month={prev_m}">‹</a>
          <a class="btn secondary" style="padding:6px 11px;" href="/journal?account={sel}&month={next_m}">›</a>
        </div>
        {journal_calendar_html(s['daily'], cy, cm)}
      </div>
      <div class="card span4"><h3>Long vs Short</h3>{journal_bars_html(s['by_side'], lambda k: k.title())}
        <h3 style="margin-top:16px;">By Day of Week</h3>{journal_bars_html(s['by_weekday'], lambda k: _WEEKDAY[k])}
      </div>

      <div class="card span6"><h3>By Symbol</h3>{journal_bars_html(s['by_symbol'], lambda k: k, order='pnl')}</div>
      <div class="card span6"><h3>By Hour (ET)</h3>{journal_bars_html(s['by_hour'], lambda k: f"{k:02d}:00")}</div>
      {tag_card}

      <div class="card span12"><h3>Closed Trades</h3><p class="muted">Click <b>Tag / Note</b> on any trade to add setups, a journal note, and a screenshot.</p><table><tr><th>Closed</th><th>Symbol</th><th>Account</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Tags</th><th></th></tr>{trip_rows}</table></div>
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


_COMMON_TAGS = ["A+ setup", "Breakout", "Reversal", "Trend", "Scalp", "News",
                "FOMO", "Revenge", "Overtraded", "Followed plan", "Broke rules", "Mistake"]


@app.get("/journal/trip/{key}", response_class=HTMLResponse)
def journal_trip_edit(request: Request, key: str):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    sel = request.query_params.get("account", "all") or "all"
    try:
        trips, _open = account_trade_history(user["id"])
    except Exception:
        trips = []
    trip = next((t for t in trips if trip_key(t) == key), None)
    jr = get_trip_journal_map(user["id"]).get(key, {})

    if not trip:
        ctx = "<p class='muted'>This trade is no longer in your recent broker history, but you can still keep its note below.</p>"
    else:
        ctx = (f"<div class='journal-day'><div><b>{trip['symbol']}</b> · {trip['side']} {trip['qty']} "
               f"<small style='color:#9ca3af'>{trip.get('account','')}</small></div>"
               f"<div class='{_pnl_cls(trip['pnl'])}'><b>{_money(trip['pnl'])}</b></div></div>"
               f"<p class='muted'>Entry {trip['entry_price']} → Exit {trip['exit_price']} · closed {str(trip.get('closed_at') or '')[:16].replace('T',' ')}</p>")

    img = f"<div style='margin:10px 0;'><img src='{jr.get('image_path')}' style='max-width:100%;border-radius:12px;border:1px solid #e5e7eb;'></div>" if jr.get("image_path") else ""
    chips = "".join(
        f"<span onclick=\"addTag('{tg}')\" style='cursor:pointer;display:inline-block;background:#f1f3f2;border-radius:8px;padding:4px 10px;margin:3px 5px 3px 0;font-size:12px;font-weight:700;'>+ {tg}</span>"
        for tg in _COMMON_TAGS
    )
    content = f'''
    <div class="header"><div><h2>Trade Journal Entry</h2><p>Tag the setup, write what happened, and attach a screenshot.</p></div>
      <a class="btn secondary" href="/journal?account={sel}">← Back to Journal</a></div>
    <div class="card span12">{ctx}</div>
    <div class="card span12">
      <form method="post" action="/journal/trip/{key}" enctype="multipart/form-data">
        <label>Tags / setups (comma-separated)</label>
        <input id="tags" name="tags" value="{jr.get('tags','') or ''}" placeholder="e.g. A+ setup, Breakout, Followed plan">
        <div style="margin:6px 0 14px;">{chips}</div>
        <label>Notes — what happened, what you'd do differently</label>
        <textarea name="note" rows="6" placeholder="Your trade review...">{jr.get('note','') or ''}</textarea>
        {img}
        <label>Screenshot (PNG/JPG/GIF/WEBP, max 5MB)</label>
        <input type="file" name="image" accept="image/*">
        <button>Save Entry</button>
      </form>
    </div>
    <script>
    function addTag(t){{
      var el=document.getElementById('tags');
      var cur=el.value.split(',').map(function(x){{return x.trim();}}).filter(Boolean);
      if(cur.indexOf(t)===-1){{ cur.push(t); el.value=cur.join(', '); }}
    }}
    </script>
    '''
    return layout(content, user, "journal")


@app.post("/journal/trip/{key}")
async def journal_trip_save(request: Request, key: str, tags: str = Form(""),
                            note: str = Form(""), image: UploadFile = File(None)):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    image_path = None
    if image is not None and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext not in ALLOWED_IMAGE_EXT or not (image.content_type or "").startswith("image/"):
            return layout(f"<div class='card'><p class='bad'>Only image files allowed.</p><a class='btn secondary' href='/journal/trip/{key}'>Back</a></div>", user, "journal")
        data = await image.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return layout(f"<div class='card'><p class='bad'>Image too large (max 5MB).</p><a class='btn secondary' href='/journal/trip/{key}'>Back</a></div>", user, "journal")
        safe_name = f"u{user['id']}_trip_{key[:10]}_{secrets.token_hex(5)}{ext}"
        with open(os.path.join(UPLOADS_DIR, safe_name), "wb") as fp:
            fp.write(data)
        image_path = f"/uploads/{safe_name}"
    save_trip_journal(user["id"], key, tags.strip(), note.strip(), image_path)
    return RedirectResponse(f"/journal/trip/{key}", status_code=302)


# ============================================================
# ECONOMIC CALENDAR (high-impact events + one-click news lockout)
# ============================================================

_CAL_CACHE: Dict[str, tuple] = {}
_CAL_TTL = 1200  # 20 minutes

_IMPACT_STYLE = {
    "high": ("#dc2626", "High"),
    "medium": ("#ea580c", "Medium"),
    "low": ("#ca8a04", "Low"),
    "holiday": ("#9ca3af", "Holiday"),
}


def _cal_s(v):
    return "" if v in (None, "") else str(v)


def _norm_ff(e: dict) -> dict:
    return {
        "dt": _parse_iso(e.get("date") or ""),
        "currency": (e.get("country") or "").upper(),
        "impact": str(e.get("impact") or "").lower(),
        "title": e.get("title") or "",
        "forecast": _cal_s(e.get("forecast")),
        "previous": _cal_s(e.get("previous")),
        "actual": "",
    }


def _norm_fmp(e: dict) -> dict:
    dt = _parse_iso((e.get("date") or "").replace(" ", "T"))
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return {
        "dt": dt,
        "currency": (e.get("currency") or e.get("country") or "").upper()[:6],
        "impact": str(e.get("impact") or "").lower(),
        "title": e.get("event") or "",
        "forecast": _cal_s(e.get("estimate")),
        "previous": _cal_s(e.get("previous")),
        "actual": _cal_s(e.get("actual")),
    }


def _week_range(week: str, now_et: datetime):
    monday = (now_et - timedelta(days=now_et.weekday())).date()
    if week == "last":
        monday -= timedelta(days=7)
    elif week == "next":
        monday += timedelta(days=7)
    return monday, monday + timedelta(days=6)


def _fmp_economic_calendar(d1, d2):
    """Call FMP's economic calendar. Tries the new 'stable' endpoint first
    (current API), then the legacy v3 path (older keys). Returns (data_or_text,
    status, url_used)."""
    key = os.getenv("FMP_API_KEY")
    if not key:
        return None, 0, ""
    urls = [
        f"https://financialmodelingprep.com/stable/economic-calendar?from={d1}&to={d2}&apikey={key}",
        f"https://financialmodelingprep.com/api/v3/economic_calendar?from={d1}&to={d2}&apikey={key}",
    ]
    last = (None, 0, "")
    for u in urls:
        try:
            r = requests.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0 (KhomaAPI)"})
            raw = r.json() if r.status_code < 400 else r.text[:400]
            if isinstance(raw, list) and raw:
                return raw, r.status_code, u
            last = (raw, r.status_code, u)
        except Exception as e:
            last = (f"{type(e).__name__}: {e}", 0, u)
    return last


def fetch_calendar_events(week: str, now_utc: datetime):
    """Return (normalized_events, multiweek_supported). Uses Financial Modeling
    Prep (full date range) when FMP_API_KEY is set; otherwise the always-free
    ForexFactory weekly feed (current week only). Cached + stale-on-failure."""
    fmp_key = os.getenv("FMP_API_KEY")
    if fmp_key:
        d1, d2 = _week_range(week, now_utc.astimezone(ZoneInfo(_ET)))
        ck = f"fmp:{d1}"
        cached = _CAL_CACHE.get(ck)
        if cached and (time.time() - cached[1]) < _CAL_TTL:
            return cached[0], True
        raw, _status, _url = _fmp_economic_calendar(d1, d2)
        evs = [_norm_fmp(e) for e in raw] if isinstance(raw, list) else []
        evs = [e for e in evs if e["dt"]]
        if evs:
            _CAL_CACHE[ck] = (evs, time.time())
            return evs, True
        if cached:
            return cached[0], True
        # FMP failed -> fall through to the free feed (this week only).

    cached = _CAL_CACHE.get("ff")
    if cached and (time.time() - cached[1]) < _CAL_TTL:
        return cached[0], False
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                         timeout=15, headers={"User-Agent": "Mozilla/5.0 (KhomaAPI)"})
        raw = r.json() if r.status_code < 400 else []
        evs = [_norm_ff(e) for e in raw] if isinstance(raw, list) else []
        evs = [e for e in evs if e["dt"]]
    except Exception:
        evs = []
    if evs:
        _CAL_CACHE["ff"] = (evs, time.time())
        return evs, False
    # Fetch failed: serve cached data ONLY if it still covers the current week,
    # so we never show last week's events as if they were this week's.
    if cached and _calendar_is_current(cached[0], now_utc):
        return cached[0], False
    return [], False


def _calendar_is_current(events, now_utc: datetime) -> bool:
    """True if the cached events still belong to the current (ET) week."""
    if not events:
        return False
    et = now_utc.astimezone(ZoneInfo(_ET))
    monday = (et - timedelta(days=et.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start = monday.astimezone(timezone.utc)
    end = (monday + timedelta(days=7)).astimezone(timezone.utc)
    return any(start <= e["dt"] < end for e in events if e.get("dt"))


def _lockout_window(dt_utc: datetime, duration: str):
    """(start_utc, end_utc) for a lockout. minutes -> ±minutes; 'day' -> the
    whole ET trading day containing the event."""
    if duration == "day":
        et = dt_utc.astimezone(ZoneInfo(_ET))
        start = et.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
    try:
        m = int(duration)
    except (TypeError, ValueError):
        m = 15
    return dt_utc - timedelta(minutes=m), dt_utc + timedelta(minutes=m)


@app.get("/calendar", response_class=HTMLResponse)
def economic_calendar_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    now_utc = datetime.now(timezone.utc)
    week = (request.query_params.get("week") or "this").lower()
    impact_filter = (request.query_params.get("impact") or "all").lower()
    cur_filter = (request.query_params.get("cur") or "all").upper()

    events, multiweek = fetch_calendar_events(week, now_utc)
    if not multiweek:
        week = "this"

    currencies = sorted({e["currency"] for e in events if e["currency"]})
    today_et = now_utc.astimezone(ZoneInfo(_ET)).strftime("%Y-%m-%d")

    by_day = {}
    next_hi = None
    for e in events:
        imp = e["impact"]
        if impact_filter == "high" and imp != "high":
            continue
        if impact_filter == "med" and imp not in ("high", "medium"):
            continue
        if cur_filter != "ALL" and e["currency"] != cur_filter:
            continue
        et = e["dt"].astimezone(ZoneInfo(_ET))
        by_day.setdefault(et.strftime("%Y-%m-%d"), []).append((et, e))
        if imp == "high" and e["dt"] > now_utc and (next_hi is None or e["dt"] < next_hi[0]):
            next_hi = (e["dt"], e)

    rows_html = ""
    if not events:
        rows_html = "<div class='card span12'><p class='muted'>Calendar data is temporarily unavailable — please refresh in a minute.</p></div>"
    for daykey in sorted(by_day.keys()):
        items = sorted(by_day[daykey], key=lambda x: x[0])
        day_label = items[0][0].strftime("%A, %b %-d")
        today_badge = " <span class='pill' style='padding:2px 9px;'>TODAY</span>" if daykey == today_et else ""
        ev_rows = ""
        for et, e in items:
            imp = e["impact"]
            color, label = _IMPACT_STYLE.get(imp, ("#9ca3af", imp.title() or "—"))
            dot = f"<span style='display:inline-block;width:9px;height:9px;border-radius:999px;background:{color};margin-right:7px;'></span>"
            actual = e.get("actual") or ""
            act_html = f"<b>{actual}</b>" if actual else "<span style='color:#cbd5e1'>—</span>"
            event_iso = et.astimezone(timezone.utc).isoformat()
            block = ""
            if imp in ("high", "medium"):
                block = (f"<form method='post' action='/calendar/block' style='margin:0;' "
                         f"onsubmit=\"this.duration.value=document.getElementById('lockdur').value\">"
                         f"<input type='hidden' name='event_at' value='{event_iso}'>"
                         f"<input type='hidden' name='duration' value='15'>"
                         f"<input type='hidden' name='label' value='{e['currency']} {e['title'][:40]}'>"
                         f"<button class='secondary' style='padding:5px 10px;margin:0;'>🔒 Block</button></form>")
            ev_rows += (
                f"<tr><td style='white-space:nowrap;'>{et.strftime('%-I:%M %p')}</td>"
                f"<td><b>{e['currency']}</b></td>"
                f"<td>{dot}<span style='font-size:11px;color:{color};font-weight:800;'>{label}</span></td>"
                f"<td>{e['title']}</td>"
                f"<td style='text-align:right;'>{act_html}</td>"
                f"<td style='text-align:right;'>{e['forecast']}</td>"
                f"<td style='text-align:right;color:#9ca3af;'>{e['previous']}</td>"
                f"<td style='text-align:right;'>{block}</td></tr>"
            )
        rows_html += (f"<div class='card span12'><h3>{day_label}{today_badge}</h3>"
                      f"<table><tr><th>Time (ET)</th><th>Cur</th><th>Impact</th><th>Event</th>"
                      f"<th style='text-align:right'>Actual</th><th style='text-align:right'>Forecast</th>"
                      f"<th style='text-align:right'>Previous</th><th></th></tr>{ev_rows}</table></div>")

    def wbtn(val, lab):
        cls = "btn" if week == val else "btn secondary"
        return f'<a class="{cls}" style="padding:7px 12px;margin:0 5px 5px 0;" href="/calendar?week={val}&impact={impact_filter}&cur={cur_filter}">{lab}</a>'

    def ibtn(val, lab):
        cls = "btn" if impact_filter == val else "btn secondary"
        return f'<a class="{cls}" style="padding:7px 12px;margin:0 5px 5px 0;" href="/calendar?week={week}&impact={val}&cur={cur_filter}">{lab}</a>'

    def cbtn(val, lab):
        cls = "btn" if cur_filter == val else "btn secondary"
        return f'<a class="{cls}" style="padding:6px 11px;margin:0 5px 5px 0;font-size:13px;" href="/calendar?week={week}&impact={impact_filter}&cur={val}">{lab}</a>'

    week_nav = (wbtn("last", "‹ Last") + wbtn("this", "This week") + wbtn("next", "Next ›")) if multiweek else \
        '<span class="muted" style="font-size:12px;">This week · add an FMP_API_KEY for multi-week & historical</span>'
    cur_buttons = cbtn("ALL", "All") + "".join(cbtn(c, c) for c in currencies[:10])

    countdown = ""
    if next_hi:
        countdown = (f'<div class="card span12" id="cd-card" style="background:#0b1220;color:#e5e7eb;">'
                     f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'
                     f'<div><div style="font-size:12px;color:#93a1b5;">NEXT HIGH-IMPACT EVENT</div>'
                     f'<div style="font-size:17px;font-weight:800;">{next_hi[1]["currency"]} · {next_hi[1]["title"]}</div></div>'
                     f'<div id="cd-timer" data-ts="{int(next_hi[0].timestamp())}" style="font-size:28px;font-weight:850;letter-spacing:-1px;">—</div>'
                     f'</div></div>')

    # Recurring news-lockout rules ("never trade CPI/FOMC/PPI…").
    con = db()
    _rules = [dict(r) for r in con.execute(
        "SELECT * FROM news_rules WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()]
    con.close()

    def _rule_row(r):
        return (f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"padding:9px 0;border-bottom:1px solid var(--line);'>"
                f"<span>🔒 <b>{r['keyword']}</b> <span class='muted'>· blocks ±{r['minutes_before']}/{r['minutes_after']} min around every match</span></span>"
                f"<form method='post' action='/calendar/rule/{r['id']}/delete' style='margin:0;'>"
                f"<button class='secondary' style='padding:5px 11px;margin:0;'>Remove</button></form></div>")
    _rules_list = ("".join(_rule_row(r) for r in _rules)
                   or "<p class='muted'>No recurring rules yet — add one below and it auto-blocks every time that event hits the calendar.</p>")

    def _preset(kw):
        return (f"<button type='button' class='secondary' style='padding:6px 11px;margin:0 6px 6px 0;' "
                f"onclick=\"document.getElementById('rk').value='{kw}'\">{kw}</button>")
    recurring_html = f'''
      <div class="card span12">
        <h3>Recurring News Lockout</h3>
        <p class="muted">Set it once — KhomaAPI auto-pauses trading around these events <b>every</b> time they occur (every CPI, every FOMC, every PPI). No weekly setup, applied to all your accounts.</p>
        <div style="margin:12px 0;">{_rules_list}</div>
        <form method="post" action="/calendar/rule/add" style="display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:6px;">
          <div style="width:100%;">{_preset('CPI')}{_preset('FOMC')}{_preset('PPI')}{_preset('Non-Farm')}{_preset('Unemployment')}{_preset('Interest Rate')}</div>
          <div style="flex:1;min-width:180px;margin:0;"><label>Event keyword</label><input id="rk" name="keyword" placeholder="e.g. CPI" required style="margin:6px 0 0;"></div>
          <div style="width:120px;margin:0;"><label>Min before</label><input name="minutes_before" value="15" style="margin:6px 0 0;"></div>
          <div style="width:120px;margin:0;"><label>Min after</label><input name="minutes_after" value="15" style="margin:6px 0 0;"></div>
          <button style="margin:0;">Add rule</button>
        </form>
      </div>'''

    content = f'''
    <div class="header"><div><h2>Economic Calendar</h2><p>Major market-moving events. Choose a lockout length and block trading around red-folder news in one click — your accounts auto-pause during the window.</p></div></div>
    <div class="grid">
      <div class="card span12" style="display:flex;flex-wrap:wrap;align-items:center;gap:8px;">
        {week_nav}
        <span style="width:14px;"></span>
        {ibtn("all","All")}{ibtn("med","Medium+")}{ibtn("high","High only")}
        <span style="display:flex;align-items:center;gap:8px;margin-left:auto;">
          <label style="font-size:13px;font-weight:700;">Lockout</label>
          <select id="lockdur" style="width:auto;margin:0;">
            <option value="15">± 15 min</option><option value="30">± 30 min</option>
            <option value="60">± 1 hour</option><option value="120">± 2 hours</option>
            <option value="day">Entire day</option>
          </select>
          <form method="post" action="/calendar/block-all" style="margin:0;" onsubmit="this.duration.value=document.getElementById('lockdur').value">
            <input type="hidden" name="week" value="{week}"><input type="hidden" name="cur" value="{cur_filter}">
            <input type="hidden" name="duration" value="15">
            <button class="danger" style="padding:7px 12px;margin:0;">🔒 Block all High-impact</button>
          </form>
        </span>
      </div>
      <div class="card span12" style="display:flex;flex-wrap:wrap;align-items:center;gap:4px;">
        <span class="muted" style="font-size:12px;margin-right:6px;">Currency:</span>{cur_buttons}
        <span class="muted" style="margin-left:auto;font-size:12px;">🔴 High · 🟠 Medium · 🟡 Low · times in ET</span>
      </div>
      {recurring_html}
      {countdown}
      {rows_html}
    </div>
    <script>
    (function(){{
      var el=document.getElementById('cd-timer'); if(!el) return;
      var ts=parseInt(el.dataset.ts,10)*1000;
      function tick(){{
        var d=ts-Date.now();
        if(d<=0){{ el.textContent='NOW'; return; }}
        var h=Math.floor(d/3600000), m=Math.floor(d%3600000/60000), s=Math.floor(d%60000/1000);
        el.textContent=(h>0?h+'h ':'')+m+'m '+s+'s';
      }}
      tick(); setInterval(tick,1000);
    }})();
    </script>
    '''
    return layout(content, user, "calendar")


@app.get("/debug/calendar")
def debug_calendar(request: Request):
    """Verify the calendar data source (FMP vs free feed), field mapping, and the
    timezone of event timestamps. Public (only returns non-sensitive public
    economic data + whether a key is configured) to avoid login friction."""
    now = datetime.now(timezone.utc)
    key = os.getenv("FMP_API_KEY")
    out = {"fmp_key_set": bool(key)}
    # Probe EACH candidate FMP endpoint separately so we can see exactly which
    # (if any) works on this key/plan.
    if key:
        d1, d2 = _week_range("this", now.astimezone(ZoneInfo(_ET)))
        probes = {
            "stable_economic-calendar": f"https://financialmodelingprep.com/stable/economic-calendar?from={d1}&to={d2}&apikey={key}",
            "stable_economics-calendar": f"https://financialmodelingprep.com/stable/economics_calendar?from={d1}&to={d2}&apikey={key}",
            "v3_economic_calendar": f"https://financialmodelingprep.com/api/v3/economic_calendar?from={d1}&to={d2}&apikey={key}",
        }
        out["fmp_probes"] = {}
        for name, u in probes.items():
            try:
                r = requests.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0 (KhomaAPI)"})
                body = r.json() if r.status_code < 400 else r.text[:200]
                out["fmp_probes"][name] = {
                    "status": r.status_code,
                    "count": len(body) if isinstance(body, list) else 0,
                    "sample": body[0] if isinstance(body, list) and body else (body if not isinstance(body, list) else None),
                }
            except Exception as e:
                out["fmp_probes"][name] = {"error": f"{type(e).__name__}: {e}"}
    for wk in ("last", "this", "next"):
        evs, mw = fetch_calendar_events(wk, now)
        out[wk] = {"events": len(evs), "multiweek": mw,
                   "sample": [{"title": e["title"], "currency": e["currency"], "impact": e["impact"],
                               "et_time": e["dt"].astimezone(ZoneInfo(_ET)).strftime("%Y-%m-%d %-I:%M %p ET")}
                              for e in evs[:3]]}
    return out


@app.post("/calendar/block")
async def calendar_block(request: Request):
    """Create a news-lockout window from a calendar event — the risk engine then
    rejects opening orders across the user's accounts during that window."""
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    dt = _parse_iso((form.get("event_at") or "").strip())
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        start, end = _lockout_window(dt, (form.get("duration") or "15").strip())
        con = db()
        con.execute(
            "INSERT INTO news_windows(user_id,account_id,starts_at,ends_at,label,created_at) VALUES(?,?,?,?,?,?)",
            (user["id"], None, start.isoformat(), end.isoformat(),
             (form.get("label") or "Economic event").strip()[:80], datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
    return RedirectResponse("/calendar", status_code=302)


@app.post("/calendar/block-all")
async def calendar_block_all(request: Request):
    """Bulk-block every HIGH-impact event in the current view with one click."""
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    week = (form.get("week") or "this").lower()
    cur = (form.get("cur") or "ALL").upper()
    duration = (form.get("duration") or "15").strip()
    events, _mw = fetch_calendar_events(week, datetime.now(timezone.utc))
    con = db()
    now_iso = datetime.now(timezone.utc).isoformat()
    for e in events:
        if e["impact"] != "high":
            continue
        if cur != "ALL" and e["currency"] != cur:
            continue
        start, end = _lockout_window(e["dt"], duration)
        con.execute(
            "INSERT INTO news_windows(user_id,account_id,starts_at,ends_at,label,created_at) VALUES(?,?,?,?,?,?)",
            (user["id"], None, start.isoformat(), end.isoformat(),
             f"{e['currency']} {e['title'][:40]}", now_iso),
        )
    con.commit()
    con.close()
    return RedirectResponse(f"/calendar?week={week}&impact=high&cur={cur}", status_code=302)


@app.post("/calendar/rule/add")
async def calendar_rule_add(request: Request):
    """Add a recurring news-lockout rule (e.g. always block CPI)."""
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    form = await request.form()
    kw = (form.get("keyword") or "").strip()[:60]

    def _i(v, d):
        try:
            return max(0, min(720, int(float(v))))
        except Exception:
            return d
    if kw:
        con = db()
        con.execute(
            "INSERT INTO news_rules(user_id,account_id,keyword,minutes_before,minutes_after,enabled,created_at) "
            "VALUES(?,?,?,?,?,1,?)",
            (user["id"], None, kw, _i(form.get("minutes_before"), 15),
             _i(form.get("minutes_after"), 15), datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
        try:
            sync_recurring_news_locks()  # apply right away
        except Exception:
            pass
    return RedirectResponse("/calendar", status_code=302)


@app.post("/calendar/rule/{rid}/delete")
def calendar_rule_delete(request: Request, rid: int):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    con = db()
    con.execute("DELETE FROM news_rules WHERE id=? AND user_id=?", (rid, user["id"]))
    # Drop this user's auto windows; the re-sync rebuilds them from remaining rules.
    con.execute("DELETE FROM news_windows WHERE user_id=? AND source='auto'", (user["id"],))
    con.commit()
    con.close()
    try:
        sync_recurring_news_locks()
    except Exception:
        pass
    return RedirectResponse("/calendar", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")

    e = user_entitlements(user)
    linked = _ucol(user, "whop_membership_id")
    period = _ucol(user, "current_period_end") or ""
    whop_msg = request.query_params.get("whop")
    notice = ""
    if whop_msg == "linked":
        notice = "<div class='copy-note'>✓ Whop membership linked.</div>"
    elif whop_msg == "notfound":
        notice = ("<div class='copy-note' style='background:#fef2f2;color:#b91c1c;border-color:#fecaca;'>"
                  "No active KhomaAPI membership found for your email. Use the same email you bought with on Whop, or contact support.</div>")
    elif whop_msg == "unconfigured":
        notice = ("<div class='copy-note' style='background:#fff7ed;color:#9a3412;border-color:#fed7aa;'>"
                  "Billing isn’t fully configured yet.</div>")
    if e.active:
        sub_status = f"<span class='pill'>● Active — {(e.tier or '').title()} plan</span>"
    else:
        sub_status = "<span class='pill gray'>● No active subscription</span>"
    sub_cta = "Manage / change plan" if e.active else "Choose a plan"
    link_label = "Re-sync my Whop status" if linked else "Link my Whop account"

    content = f'''
    <div class="header"><div><h2>Settings</h2><p>Profile, authentication, and account security.</p></div></div>
    <div class="grid">

<div class="card span12">
  <h3>Subscription</h3>
  {notice}
  <p class="muted" style="margin-top:10px;">{sub_status} {('&nbsp; Renews / ends: ' + period) if period else ''}</p>
  <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap;">
    <a class="btn" href="/subscribe">{sub_cta}</a>
    <form method="post" action="/whop/link" style="margin:0;"><button class="secondary" type="submit">{link_label}</button></form>
  </div>
  <p class="muted" style="margin-top:10px;font-size:13px;">Bought with a different email than this account? Click “{link_label}” to connect your membership.</p>
</div>

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

<div class="card span12">
  <h3>Performance Email Reports</h3>
  <p class="muted">Get a clean, branded summary of your trading performance emailed to <b>{user['email']}</b>. Pulled straight from your broker fills.</p>
  <form method="post" action="/settings/digests">
    <label style="display:flex;align-items:center;gap:10px;font-weight:700;margin:8px 0;"><input type="checkbox" name="digest_daily" value="1" {'checked' if user['digest_daily'] else ''} style="width:auto;margin:0;"> Daily recap <span class="muted" style="font-weight:500;">— after the session close (≈5:00 PM ET)</span></label>
    <label style="display:flex;align-items:center;gap:10px;font-weight:700;margin:8px 0;"><input type="checkbox" name="digest_weekly" value="1" {'checked' if user['digest_weekly'] else ''} style="width:auto;margin:0;"> Weekly summary <span class="muted" style="font-weight:500;">— Saturday morning</span></label>
    <label style="display:flex;align-items:center;gap:10px;font-weight:700;margin:8px 0;"><input type="checkbox" name="digest_monthly" value="1" {'checked' if user['digest_monthly'] else ''} style="width:auto;margin:0;"> Monthly report <span class="muted" style="font-weight:500;">— 1st of the month</span></label>
    <button style="margin-top:10px;">Save Report Preferences</button>
    <a class="btn secondary" style="margin-top:10px;" href="/settings/digests/sample">Send me a sample now</a>
  </form>
</div>

<div class="card span12">
  <h3>Webhook Security</h3>
  <p class="muted">Your webhook secret signs every TradingView alert. Regenerate it if it may have leaked — this <b>immediately invalidates the old secret</b>, so any existing TradingView alerts will be rejected until you paste the new JSON from the Webhooks page.</p>
  <div class="keybox" style="margin:12px 0;"><span>{mask_value(user['webhook_secret'])}</span></div>
  <form method="post" action="/settings/regenerate-webhook-secret" onsubmit="return confirm('Regenerate your webhook secret? Existing TradingView alerts will STOP working until you update them with the new secret from the Webhooks page.');">
    <button class="danger">Regenerate Webhook Secret</button>
  </form>
</div>

    </div>
    '''
    return layout(content, user, "settings")


@app.post("/settings/digests")
async def settings_digests(request: Request):
    user, _e, deny = gate(request, feature=ent.EMAIL_DIGESTS)
    if deny:
        return deny
    form = await request.form()
    con = db()
    con.execute(
        "UPDATE users SET digest_daily=?, digest_weekly=?, digest_monthly=? WHERE id=?",
        (1 if form.get("digest_daily") else 0, 1 if form.get("digest_weekly") else 0,
         1 if form.get("digest_monthly") else 0, user["id"]),
    )
    con.commit()
    con.close()
    return RedirectResponse("/settings", status_code=302)


@app.get("/settings/digests/sample")
def settings_digest_sample(request: Request):
    """Send the user a sample performance digest right now (preview)."""
    user, _e, deny = gate(request, feature=ent.EMAIL_DIGESTS)
    if deny:
        return deny
    try:
        trips, _o = account_trade_history(user["id"])
    except Exception:
        trips = []
    now_et = datetime.now(timezone.utc).astimezone(ZoneInfo(_ET))
    sent = _send_one_digest(dict(user), "sample (last 30 days)", "Your KhomaAPI performance — sample",
                            _trips_in_range(trips, datetime.now(timezone.utc) - timedelta(days=30), datetime.now(timezone.utc)),
                            start_date=(now_et - timedelta(days=30)).strftime("%Y-%m-%d"), end_date=now_et.strftime("%Y-%m-%d"))
    return login_layout(
        ("<h1>Sample sent</h1><p>Check your inbox for a sample performance report.</p>" if sent
         else f"<h1>Couldn't send</h1><p>{LAST_EMAIL_ERROR}</p>")
        + "<a class='btn' href='/settings'>Back to Settings</a>"
    )


@app.post("/settings/regenerate-webhook-secret")
def regenerate_webhook_secret(request: Request):
    user = require_user(request)
    if not user:
        return RedirectResponse("/login")
    new_secret = secrets.token_hex(20)
    con = db()
    con.execute("UPDATE users SET webhook_secret=? WHERE id=?", (new_secret, user["id"]))
    con.commit()
    con.close()
    return login_layout(
        "<h1>Webhook secret regenerated</h1>"
        "<p>Your old secret is now invalid. Open the Webhooks page, copy the new JSON, "
        "and paste it into every TradingView alert — existing alerts will be rejected until you do.</p>"
        "<a class='btn' href='/webhooks'>Go to Webhooks</a> <a class='btn secondary' href='/settings'>Back to Settings</a>"
    )


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
    # Generic TradingView webhook contract — works with ANY Pine Script strategy.
    # `side` accepts buy / sell / close / reverse (and common Pine synonyms).
    # Any extra strategy-specific fields are accepted and ignored (not rejected).
    model_config = {"extra": "allow"}

    client_id: str
    auth: str
    # symbol/side are optional so exit / move_stop alerts (event + auth only) parse;
    # entries still require them (enforced in risk_check → "Missing symbol").
    symbol: Optional[str] = None
    side: Optional[str] = None
    qty: Optional[Any] = 1
    request_id: Optional[str] = None
    # Optional: route this alert to ONE specific connected account (independent
    # strategy). When omitted, the alert is treated as a master signal and
    # mirrored across every account in the Copy Trading group.
    account: Optional[str] = None


class WebhookFlatten(BaseModel):
    client_id: str
    auth: str
    symbol: str
    request_id: Optional[str] = None




@app.get("/auth/tradovate/connect")
def tradovate_connect(request: Request):
    user, e, deny = gate(request)
    if deny:
        return deny
    # Plan account cap — block starting a connect that can't add any account.
    if account_cap_remaining(user, e) == 0:
        return RedirectResponse("/broker?error=plan_limit", status_code=302)

    # Which environment(s) to import. A single Tradovate login exposes BOTH demo
    # and live. Demo -> demo root; Live -> live root; Prop Firm -> both (eval
    # accounts often live on sim/demo while funded accounts are on live).
    choice = (request.query_params.get("env") or "demo").lower()
    envs = {"demo": ("demo",), "live": ("live",), "prop": ("live", "demo")}.get(choice, ("demo",))

    # Tie this OAuth attempt to the logged-in user via an unguessable state token.
    state = secrets.token_urlsafe(24)
    OAUTH_STATES[state] = {"user_id": user["id"], "envs": envs}

    return RedirectResponse(build_tradovate_login(state))


@app.get("/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/broker?error={error}", status_code=302)

    if not code:
        return RedirectResponse("/broker?error=missing_code", status_code=302)

    # Resolve which user started this flow: state map first, session cookie as fallback.
    st = OAUTH_STATES.pop(state, None)
    envs = ("live", "demo")
    if isinstance(st, dict):
        user_id = st.get("user_id")
        envs = st.get("envs") or envs
    else:
        user_id = st  # legacy int value
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

    accounts_result = fetch_accounts(access_token, envs=envs)
    accounts = accounts_result.get("accounts") or []

    if not accounts:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Connected, but no accounts returned.",
                     "detail": accounts_result.get("error")},
        )

    # Plan account cap: import only up to the user's remaining allowance so a
    # Solo plan can't exceed 2 accounts even though one Tradovate login may
    # expose more. Extra accounts are skipped (the user is told to upgrade).
    capped = False
    if ENFORCE_SUBSCRIPTIONS:
        con = db()
        u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        con.close()
        remaining = account_cap_remaining(u, user_entitlements(u))
        if remaining is not None and len(accounts) > remaining:
            accounts = accounts[:max(0, remaining)]
            capped = True

    # Save each account tagged with the environment it actually came from
    # (demo vs live), so the user never trades the wrong one by accident.
    for account in accounts:
        save_broker_account(user_id, account.get("_env", "live"), account, access_token, expires_at)

    dest = "/broker?connected=1" + ("&capped=1" if capped else "")
    return RedirectResponse(url=dest, status_code=302)


def _route_signal_accounts(user, target_name):
    """Which accounts a webhook targets: one named INDEPENDENT account, else the Copy
    Trading group, else every connected account. Returns (accounts, route_label)."""
    if target_name:
        acct = find_connected_account(user["id"], target_name)
        return ([acct] if acct else []), "ROUTED"
    accounts = get_copy_accounts(user["id"])
    if accounts:
        return accounts, "COPIED"
    return get_broker_accounts(user["id"], connected_only=True), "BROADCAST"


@app.post("/webhook/trade")
def webhook_trade(payload: WebhookTrade):
    start_time = time.perf_counter()
    request_id = payload.request_id or secrets.token_hex(8)

    con = db()
    user = con.execute("SELECT * FROM users WHERE email=?", (payload.client_id.lower().strip(),)).fetchone()
    con.close()

    if not user:
        return JSONResponse(status_code=200, content={"ok": False, "error": "Client not found."})

    # Subscription gate: no active plan → reject the order and log it. (No-op
    # until enforcement is switched on.)
    if not webhook_subscription_ok(user):
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, str(payload.symbol or "").upper(),
                  str(payload.side or ""), clean_qty(payload.qty), "rejected",
                  "REJECTED", latency, "blocked: no active subscription", {})
        return JSONResponse(status_code=402, content={
            "ok": False, "status": "REJECTED",
            "error": "blocked: no active subscription"})

    try:
        target_name = (payload.account or "").strip()
        extras = payload.model_extra or {}
        event = str(extras.get("event") or "").lower().strip()
        brackets_on = _brackets_on_for(user["email"])   # global flag + canary allow-list

        # --- replace_stop: move the runner's protective stop to breakeven via CANCEL + re-place.
        # Routed on `event` FIRST (independent of the bracket flag) so it can NEVER fall through
        # to an order-placement path — fixes the bug where side:"sell" opened another short.
        # Only cancels the old stop and creates a new one; never opens/adds/reverses a position.
        if event == "replace_stop":
            if payload.auth != user["webhook_secret"]:
                raise Exception("Invalid webhook secret.")
            sym = (str(payload.symbol).upper() if payload.symbol else None)
            if not sym:
                latency = round((time.perf_counter() - start_time) * 1000, 3)
                msg = "Rejected: replace_stop requires a symbol."
                log_trade(user["id"], request_id, "*", event, 0, "rejected", "REJECTED", latency, msg, {})
                return JSONResponse(status_code=200, content={"ok": False, "status": "REJECTED", "error": msg})
            accounts, route = _route_signal_accounts(user, target_name)
            pos_side = str(payload.side or "").lower()   # POSITION side; the new stop is the OPPOSITE
            new_stop = extras.get("stop")
            response = replace_stops_to_accounts(accounts, sym, pos_side, new_stop)
            replaced = sum(1 for r in response.get("results", []) if r.get("ok"))
            latency = round((time.perf_counter() - start_time) * 1000, 3)
            message = f"Replace stop -> {new_stop}: re-armed {replaced} runner stop(s) on {response['accounts']} account(s)."
            log_trade(user["id"], request_id, sym, event, 0, "live", "EXECUTED", latency, message, response)
            return {"ok": True, "action": "REPLACE_STOP", "status": "EXECUTED", "message": message,
                    "mode": "live", "symbol": sym, "latency_ms": latency, "response": response}

        # --- Lifecycle events that MANAGE an existing position; they never open one.
        # This also fixes the old bug where move_stop (side:"buy") was misread as a new
        # BUY entry, and where exit was dropped for missing fields.
        if brackets_on and event in ("exit", "move_stop"):
            if payload.auth != user["webhook_secret"]:
                raise Exception("Invalid webhook secret.")
            accounts, route = _route_signal_accounts(user, target_name)
            sym = (str(payload.symbol).upper() if payload.symbol else None)
            # A symbol-less move_stop would yank EVERY resting stop on the account to one price
            # — never intended. Fail closed. (A symbol-less exit is still a valid flatten-all.)
            if event == "move_stop" and not sym:
                latency = round((time.perf_counter() - start_time) * 1000, 3)
                msg = "Rejected: move_stop requires a symbol (refusing an account-wide stop move)."
                log_trade(user["id"], request_id, "*", event, 0, "rejected", "REJECTED", latency, msg, {})
                return JSONResponse(status_code=200, content={"ok": False, "status": "REJECTED", "error": msg})
            if event == "exit":
                response = exit_from_accounts(accounts, sym)          # cancel orders + flatten (idempotent)
                action = "EXIT"
                message = f"Exit: cancel working orders + flatten on {response['accounts']} account(s)."
            else:  # move_stop -> modify the resting stop (e.g. to breakeven)
                new_stop = extras.get("stop")
                response = move_stops_to_accounts(accounts, sym, new_stop)
                moved = sum(int(r.get("moved") or 0) for r in response.get("results", []))
                action = "MOVE_STOP"
                message = f"Move stop → {new_stop}: modified {moved} working stop(s) on {response['accounts']} account(s)."
            latency = round((time.perf_counter() - start_time) * 1000, 3)
            log_trade(user["id"], request_id, sym or "*", event, 0, "live", "EXECUTED", latency, message, response)
            return {"ok": True, "action": action, "status": "EXECUTED", "message": message,
                    "mode": "live", "symbol": sym, "latency_ms": latency, "response": response}

        # --- ENTRY (or legacy buy/sell/flatten/reverse signal). Dedup is scoped per
        # target so the same alert can hit different accounts without one blocking another.
        dedup_id = f"{target_name}|{request_id}" if target_name else request_id
        symbol, side, qty = risk_check(user, payload.auth, payload.symbol, payload.side, payload.qty, dedup_id)

        # Routing (independent and copy systems run side-by-side, never crossing).
        accounts, route = _route_signal_accounts(user, target_name)
        if target_name and not accounts:
            raise Exception(f"Account '{target_name}' is not connected.")

        if accounts:
            # Real resting OCO bracket(s) when the entry carries a stop (buy/sell only);
            # scale-out alerts (qtyScale+qtyRunner+tp1+tp2) place TWO independent brackets.
            legs = entry_legs_from_alert(side, qty, extras) if (brackets_on and side in ("buy", "sell")) else None
            has_bracket = bool(legs) and any(st is not None for (_q, st, _l) in legs)
            if has_bracket:
                response = execute_bracket_to_accounts(accounts, symbol, side, legs)
                action = route + "_BRACKET"
            else:
                response = execute_to_accounts(accounts, symbol, side, qty)
                action = route
            mode = "live"
            status = "EXECUTED" if (response["placed"] > 0 or response["total"] == 0) else "REJECTED"
            if target_name:
                scope = f"account {target_name}"
            elif route == "BROADCAST":
                scope = f"{response['accounts']} connected account(s)"
            else:
                scope = f"{response['accounts']} copy account(s)"
            kind = "bracket" if has_bracket else "order"
            message = f"{route.title()} {response['placed']}/{response['total']} {kind}(s) to {scope}."
            # If nothing was placed, surface the broker's reason (e.g. token
            # expired -> reconnect) so the failure isn't silent.
            if response["placed"] == 0 and response["total"] > 0:
                errors = [r.get("error") for r in response.get("results", []) if r.get("error")]
                if errors:
                    message += " " + errors[0]
        else:
            # Only reached when the user has zero connected accounts at all.
            response = {"routed": False, "symbol": symbol, "side": side, "qty": qty}
            action = "NO_ROUTE"
            mode = "live"
            status = "NO_ROUTE"
            message = "No connected accounts — connect a Tradovate account on the Broker Connection page."

        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, symbol, side, qty, mode, status, latency, message, response)

        return {
            "ok": status == "EXECUTED",
            "action": action,
            "status": status,
            "message": message,
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

    if not webhook_subscription_ok(user):
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, str(payload.symbol or "").upper(), "flatten", 0,
                  "rejected", "REJECTED", latency, "blocked: no active subscription", {})
        return JSONResponse(status_code=402, content={
            "ok": False, "status": "REJECTED", "error": "blocked: no active subscription"})

    try:
        if payload.auth != user["webhook_secret"]:
            raise Exception("Invalid webhook secret.")
        broker_connection_check(user["id"])
        result = safe_flatten_symbol(user["id"], payload.symbol.upper())
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, payload.symbol.upper(), "flatten", 0, "live", "FLATTEN_SENT", latency, "Flatten attempted.", result)
        return {"ok": True, "latency_ms": latency, "result": result}
    except Exception as e:
        latency = round((time.perf_counter() - start_time) * 1000, 3)
        log_trade(user["id"], request_id, payload.symbol.upper(), "flatten", 0, "rejected", "REJECTED", latency, str(e), {})
        return {"ok": False, "error": str(e), "latency_ms": latency}


# ============================================================
# WHOP BILLING
# ============================================================

@app.post("/whop/webhook")
async def whop_webhook(request: Request):
    """Signature-verified Whop webhook. The body is only a trigger — we re-fetch
    the membership from the Whop API and use its authoritative `valid` flag to
    grant/revoke, so a forged or stale body can't unlock anything."""
    raw = await request.body()
    ok, reason = whopmod.verify_signature(raw, request.headers, WHOP_WEBHOOK_SECRET)
    if not ok:
        print(f"WHOP webhook REJECTED: {reason}")
        return JSONResponse(status_code=401, content={"ok": False, "error": "invalid signature"})

    try:
        payload = json.loads(raw.decode() or "{}")
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad json"})

    action = payload.get("action") or payload.get("event") or ""
    mid = whopmod.extract_membership_id(payload)
    print(f"WHOP webhook accepted ({reason}) action={action} membership={mid}")
    if not mid:
        return {"ok": True, "note": "no membership in event"}

    # Source of truth: re-fetch. Fall back to the event body only if the API
    # fetch fails AND the body already looks like a full membership object.
    m = whopmod.fetch_membership(mid, WHOP_API_KEY)
    if m is whopmod.GONE or m == whopmod.GONE:
        # Membership deleted → revoke any account linked to it.
        con = db()
        con.execute("UPDATE users SET subscription_status='revoked' WHERE whop_membership_id=?", (mid,))
        con.commit()
        con.close()
        return {"ok": True, "membership": mid, "revoked": True}
    if not m or not m.get("id"):
        data = payload.get("data")
        m = data if isinstance(data, dict) and data.get("id") else None
    if not m:
        return {"ok": True, "note": "membership not retrievable"}

    linked = link_membership_to_user(m)

    # Buy-before-signup: no account matches yet. Record it (keyed by email) so
    # the buyer's first login links instantly without an API scan.
    if linked is None:
        st = whopmod.membership_state(m)
        if st["email"] and ent.tier_for_plan_id(st["plan_id"]):
            con = db()
            con.execute(
                "INSERT INTO whop_pending(email, membership_id, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET membership_id=excluded.membership_id, "
                "updated_at=excluded.updated_at",
                (st["email"], st["membership_id"], datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            con.close()

    return {"ok": True, "membership": mid, "linked_user_id": linked}


@app.post("/whop/link")
def whop_link(request: Request):
    """'Link my Whop' button — finds the logged-in user's membership by email
    and attaches it (hybrid fallback when the buyer's Whop email differs)."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not WHOP_API_KEY:
        return RedirectResponse("/settings?whop=unconfigured", status_code=302)
    m = whopmod.find_membership_by_email(
        _ucol(user, "email"), WHOP_API_KEY,
        allowed_plan_ids=list(ent.plan_env_map().keys()))
    if not m:
        return RedirectResponse("/settings?whop=notfound", status_code=302)
    link_membership_to_user(m, by_user_id=user["id"])
    return RedirectResponse("/settings?whop=linked", status_code=302)


@app.get("/subscribe", response_class=HTMLResponse)
@app.get("/upgrade", response_class=HTMLResponse)
def subscribe_page(request: Request):
    up = request.query_params.get("upgrade")
    banner = ""
    if up and up != "1":
        banner = f"{_FEATURE_LABELS.get(up, up)} requires the Pro plan or higher — upgrade below to unlock it."
    elif up:
        banner = "Upgrade your plan to unlock this feature."
    return HTMLResponse(plans_html(banner))


# ============================================================
# PUBLIC VERIFIED-RESULTS PAGE (live, read-only, no login)
# ============================================================

_TRACK_CSS = """
*{box-sizing:border-box;}
body{margin:0;background:#0b0f15;color:#e7edf5;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;}
a{color:inherit;text-decoration:none;}
.wrap{max-width:1080px;margin:0 auto;padding:30px 20px 70px;}
.topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:24px;}
.brand{font-size:22px;font-weight:850;letter-spacing:-.6px;}
.vbadge{display:inline-flex;align-items:center;gap:6px;background:rgba(22,217,126,.12);color:#16d97e;border:1px solid rgba(22,217,126,.32);padding:6px 11px;border-radius:999px;font-size:12px;font-weight:800;}
.live{display:inline-flex;align-items:center;gap:7px;color:#8aa0b8;font-size:12px;margin-left:auto;}
.live .dot{width:8px;height:8px;border-radius:999px;background:#16d97e;animation:pulse 1.6s infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(22,217,126,.5);}70%{box-shadow:0 0 0 8px rgba(22,217,126,0);}100%{box-shadow:0 0 0 0 rgba(22,217,126,0);}}
.hero{display:grid;grid-template-columns:1.5fr 1fr;gap:16px;margin-bottom:16px;}
.card{background:#121823;border:1px solid #1e2836;border-radius:18px;padding:22px;}
.k{color:#8aa0b8;font-size:13px;font-weight:650;}
.big{font-size:42px;font-weight:900;letter-spacing:-1.6px;margin-top:6px;}
.pos{color:#16d97e;} .neg{color:#ff5470;} .flat{color:#8aa0b8;}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:18px;}
.cards .v{font-size:22px;font-weight:850;margin-top:7px;letter-spacing:-.5px;}
.sec-h{font-size:13px;font-weight:800;color:#aeb9c6;text-transform:uppercase;letter-spacing:.09em;margin:0 0 14px;}
.eq-wrap{height:250px;width:100%;}
.tcal-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}
.tcal-month{font-weight:850;font-size:16px;}
.tcal-nav{width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;border:1px solid #1e2836;border-radius:10px;font-size:18px;}
.tcal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:7px;}
.tcal-h{color:#6c7c90;font-size:11px;font-weight:700;text-align:center;text-transform:uppercase;}
.tcal-c{min-height:66px;border:1px solid #1a2230;border-radius:12px;padding:7px 9px;background:#0e141d;}
.tcal-c.empty{background:transparent;border:none;}
.tcal-c.pos{background:rgba(22,217,126,.10);border-color:rgba(22,217,126,.28);}
.tcal-c.neg{background:rgba(255,84,112,.10);border-color:rgba(255,84,112,.28);}
.tcal-d{font-size:11px;color:#8aa0b8;}
.tcal-p{font-size:14px;font-weight:850;margin-top:9px;}
.tcal-c.pos .tcal-p{color:#16d97e;} .tcal-c.neg .tcal-p{color:#ff5470;}
.foot{color:#6c7c90;font-size:12px;text-align:center;margin-top:32px;line-height:1.7;}
.empty-note{color:#8aa0b8;text-align:center;padding:36px;}
@media(max-width:760px){.hero{grid-template-columns:1fr;}.big{font-size:34px;}.tcal-c{min-height:54px;}.tcal-p{font-size:12px;}}
"""

_TRACK_JS = """
async function tick(){
  try{
    const r = await fetch('/verified/live', {cache:'no-store'});
    const d = await r.json();
    if(!d.ok) return;
    var net=document.getElementById('net');
    if(net){ net.textContent=d.net_disp; net.className='big '+(d.net>=0?'pos':'neg'); }
    var today=document.getElementById('today');
    if(today){ today.textContent=d.today_disp; today.className='big '+(d.today>0?'pos':(d.today<0?'neg':'flat')); }
    var eq=document.getElementById('equity'); if(eq) eq.textContent=d.equity_disp;
    var up=document.getElementById('upd'); if(up) up.textContent=d.updated;
  }catch(e){}
}
tick(); setInterval(tick, 3000);
"""


def _public_track_user():
    con = db()
    row = con.execute("SELECT * FROM users WHERE email=?", (PUBLIC_TRACK_EMAIL,)).fetchone()
    con.close()
    return row


def _public_connected_ids(user):
    """Accounts to LOG from: the pinned list if set, else every account currently
    connected to this user's KhomaAPI dashboard ('any connected account')."""
    if PUBLIC_TRACK_ACCOUNT_IDS:
        out = []
        for x in PUBLIC_TRACK_ACCOUNT_IDS:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out
    return [a["id"] for a in get_broker_accounts(user["id"], connected_only=True)]


def _public_track_account_ids(user):
    """Accounts to DISPLAY: connected accounts PLUS any account that already has
    logged history — so the track record keeps showing past trades even after an
    account disconnects. (A pinned list overrides everything.)"""
    if PUBLIC_TRACK_ACCOUNT_IDS:
        return _public_connected_ids(user)
    ids = set(_public_connected_ids(user))
    con = db()
    for tbl in ("daily_equity", "trade_log"):
        try:
            for r in con.execute(f"SELECT DISTINCT account_id FROM {tbl} WHERE user_id=?",
                                 (user["id"],)).fetchall():
                if r["account_id"] is not None:
                    ids.add(r["account_id"])
        except Exception:
            pass
    con.close()
    return sorted(ids)


def _et_day(iso_str):
    """ET calendar date 'YYYY-MM-DD' for an ISO timestamp (UTC if naive); falls back
    to the first 10 chars so a bad value never crashes the public feed."""
    if not iso_str:
        return ""
    try:
        d = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo(_ET)).strftime("%Y-%m-%d")
    except Exception:
        return str(iso_str)[:10]


def public_daily_map(account_ids):
    """{ 'YYYY-MM-DD': summed realized P&L } across the chosen accounts, built from the
    permanent trade ledger (closed round-trips) keyed by ET close date. The ledger is
    the authoritative source — so the public calendar ALWAYS matches the trade history.
    (The old daily_equity snapshots returned $0 on real trade days and are not used.)"""
    if not account_ids:
        return {}
    ph = ",".join("?" for _ in account_ids)
    con = db()
    rows = con.execute(
        f"SELECT account_id, symbol, side, qty, entry_price, exit_price, pnl, closed_at "
        f"FROM trade_log WHERE account_id IN ({ph})",
        tuple(account_ids),
    ).fetchall()
    con.close()
    out, seen = {}, set()
    for r in rows:
        ident = (r["account_id"], r["symbol"], r["side"], r["qty"],
                 r["entry_price"], r["exit_price"], str(r["closed_at"]))
        if ident in seen:            # fold away any legacy duplicate rows
            continue
        seen.add(ident)
        day = _et_day(r["closed_at"])
        if not day:
            continue
        out[day] = round(out.get(day, 0.0) + float(r["pnl"] or 0), 2)
    return out


def public_live_snapshot(account_ids):
    """Live today-PnL + equity summed from the warm poller cache (~1s fresh)."""
    today, equity, have = 0.0, 0.0, False
    for aid in account_ids:
        c = ACCOUNT_STATE_CACHE.get(int(aid))
        if not c:
            continue
        st = c[0]
        if st.get("day_pnl") is not None:
            today += st["day_pnl"]; have = True
        if st.get("equity") is not None:
            equity += st["equity"]
    return {"today": round(today, 2) if have else None, "equity": round(equity, 2) if equity else None}


def persist_track_trades():
    """Permanently log each CLOSED trade from the public-track account(s). Runs in
    the leader loop so every trade taken on the connected account is recorded
    forever — that's what feeds the live 'Verified via Tradovate' section."""
    try:
        user = _public_track_user()
        if not user:
            return
        for aid in _public_connected_ids(user):   # log from any connected account
            try:
                # account_trade_history already persists live trips via the intrinsic-
                # deduped ledger; this scopes + re-persists idempotently as a backstop.
                trips, _o = account_trade_history(user["id"], only_account_id=aid)
            except Exception:
                continue
            if not trips:
                continue
            for t in trips:
                t["_account_id"] = aid
            _ledger_persist_trips(user["id"], trips)
    except Exception as e:
        print("persist_track_trades error:", e)


def persist_all_account_trades():
    """Server-side: capture EVERY connected user's closed round-trips into the
    permanent trade ledger, on a schedule, while their broker tokens are valid and
    fills are still in Tradovate's short window. This is what lets EVERY client (not
    just the public-track account) see today's realized P&L on their dashboard/journal
    even after the fills age out — independent of whether they have the page open.

    Drives account_trade_history(user_id), which already persists every live trip to
    trade_log (intrinsic-deduped) and is broker-error resilient, so one bad account
    never stops the sweep. Bounded concurrency keeps it safe at 1,000+ accounts."""
    try:
        con = db()
        rows = con.execute(
            "SELECT DISTINCT user_id FROM broker_accounts WHERE status='connected'").fetchall()
        con.close()
        uids = [r["user_id"] for r in rows]
    except Exception as e:
        print(f"persist_all_account_trades: user lookup failed: {e}")
        return
    if not uids:
        return

    def _one(uid):
        try:
            account_trade_history(uid)   # persists this user's live trips as a side effect
        except Exception as e:
            print(f"persist_all_account_trades: uid {uid} failed: {e}")

    with ThreadPoolExecutor(max_workers=min(WATCH_WORKERS, len(uids))) as pool:
        list(pool.map(_one, uids))


def _track_live_trades(account_ids):
    if not account_ids:
        return []
    ph = ",".join("?" for _ in account_ids)
    con = db()
    rows = con.execute(
        f"SELECT account_id,side,symbol,qty,entry_price,exit_price,pnl,closed_at FROM trade_log "
        f"WHERE account_id IN ({ph}) ORDER BY closed_at", tuple(account_ids)).fetchall()
    con.close()
    out, seen = [], set()
    for r in rows:
        ident = (r["account_id"], r["symbol"], r["side"], r["qty"],
                 r["entry_price"], r["exit_price"], str(r["closed_at"]))
        if ident in seen:            # fold away any legacy duplicate rows
            continue
        seen.add(ident)
        out.append(dict(r))
    return out


def _track_user_trades(user_id):
    """All closed round-trips for the public-track USER (every account they've ever
    owned), keyed by user_id — so the verified record survives reconnects that change
    the volatile broker_accounts.id (account 44 -> 55 -> ...). Intrinsic-deduped."""
    con = db()
    rows = con.execute(
        "SELECT account_id,side,symbol,qty,entry_price,exit_price,pnl,closed_at FROM trade_log "
        "WHERE user_id=? ORDER BY closed_at", (user_id,)).fetchall()
    con.close()
    out, seen = [], set()
    for r in rows:
        ident = (r["account_id"], r["symbol"], r["side"], r["qty"],
                 r["entry_price"], r["exit_price"], str(r["closed_at"]))
        if ident in seen:
            continue
        seen.add(ident)
        out.append(dict(r))
    return out


def _trade_stats(trades):
    pnls = [float(t.get("pnl") or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gl = -sum(losses)
    pf = round(sum(wins) / gl, 2) if gl > 0 else (None if not wins else float("inf"))
    n = len(pnls)
    return {"net": round(sum(pnls), 2), "trades": n, "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "pf_disp": ("∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—"))}


def _track_stats(daily):
    days = sorted(daily.items())
    vals = [v for _, v in days]
    net = round(sum(vals), 2)
    green = sum(1 for v in vals if v > 0)
    red = sum(1 for v in vals if v < 0)
    gw = sum(v for v in vals if v > 0)
    gl = -sum(v for v in vals if v < 0)
    pf = round(gw / gl, 2) if gl > 0 else (None if gw == 0 else float("inf"))
    wr = round(green / (green + red) * 100, 1) if (green + red) else 0.0
    return {"net": net, "green": green, "red": red, "days": len(vals), "win_rate": wr,
            "pf": pf, "best": (max(days, key=lambda x: x[1]) if days else None),
            "worst": (min(days, key=lambda x: x[1]) if days else None)}


def _track_equity_svg(values):
    if not values:
        values = [0, 0]
    if len(values) == 1:
        values = [0, values[0]]
    w, h, pad = 920, 250, 16
    mn, mx = min(values), max(values)
    span = (mx - mn) or 1
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (w - 2 * pad) * (i / ((n - 1) or 1))
        y = h - pad - (h - 2 * pad) * ((v - mn) / span)
        pts.append((round(x, 1), round(y, 1)))
    line = " ".join(f"{x},{y}" for x, y in pts)
    area = f"{pad},{h-pad} " + line + f" {w-pad},{h-pad}"
    col = "#16d97e" if values[-1] >= 0 else "#ff5470"
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="width:100%;height:100%;">'
            f'<polygon points="{area}" fill="{col}" opacity="0.10"/>'
            f'<polyline points="{line}" fill="none" stroke="{col}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


def _track_calendar_html(daily, y, m):
    import calendar as _calmod
    _wd, ndays = _calmod.monthrange(y, m)
    offset = (date(y, m, 1).weekday() + 1) % 7  # Sunday-first grid
    heads = "".join(f"<div class='tcal-h'>{d}</div>" for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
    cells = ["<div class='tcal-c empty'></div>"] * offset
    month_total = 0.0
    for day in range(1, ndays + 1):
        key = f"{y:04d}-{m:02d}-{day:02d}"
        v = daily.get(key)
        if v is None:
            cells.append(f"<div class='tcal-c'><div class='tcal-d'>{day}</div></div>")
        else:
            month_total += v
            cls = "pos" if v > 0 else ("neg" if v < 0 else "flat")
            cells.append(f"<div class='tcal-c {cls}'><div class='tcal-d'>{day}</div>"
                         f"<div class='tcal-p'>{_money(v, 0)}</div></div>")
    label = date(y, m, 1).strftime("%B %Y")
    prev = (date(y, m, 1) - timedelta(days=1)).strftime("%Y-%m")
    nxt = (date(y, m, 28) + timedelta(days=7)).strftime("%Y-%m")
    mt_cls = "pos" if month_total >= 0 else "neg"
    return (f"<div class='tcal-top'><a class='tcal-nav' href='/verified?month={prev}'>&lsaquo;</a>"
            f"<div class='tcal-month'>{label} &middot; <span class='{mt_cls}'>{_money(month_total, 0)}</span></div>"
            f"<a class='tcal-nav' href='/verified?month={nxt}'>&rsaquo;</a></div>"
            f"<div class='tcal-grid'>{heads}{''.join(cells)}</div>")


_TRACK_LIVE_CACHE = {"ts": 0.0, "data": None}


def _track_payload():
    """Cached (~2s) live payload so many public viewers don't hammer the DB."""
    now = time.time()
    if _TRACK_LIVE_CACHE["data"] and (now - _TRACK_LIVE_CACHE["ts"]) < 2:
        return _TRACK_LIVE_CACHE["data"]
    user = _public_track_user()
    if not user:
        data = {"ok": False}
    else:
        # Scope the verified record to the track USER (their email), not a volatile
        # broker_accounts.id — so it survives every reconnect (account 44 -> 55 -> ...)
        # and always reflects exactly this one KhomaAPI account's real trades.
        uid = user["id"]
        daily = ledger_daily_map(uid)
        stats = _track_stats(daily)
        live = public_live_snapshot(_public_connected_ids(user))   # today's intraday, connected accts
        trades = _track_user_trades(uid)          # permanent per-trade log, all of the user's accounts
        ts = _trade_stats(trades)
        et = datetime.now(timezone.utc).astimezone(ZoneInfo(_ET))
        pf = stats["pf"]
        data = {
            "ok": True,
            "name": PUBLIC_TRACK_NAME,
            "net": stats["net"], "net_disp": _money(stats["net"]),
            "today": live["today"] if live["today"] is not None else 0,
            "today_disp": (_money(live["today"]) if live["today"] is not None else "—"),
            "equity_disp": (_money(live["equity"]) if live["equity"] else "—"),
            "win_rate": stats["win_rate"],
            "pf_disp": ("∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")),
            "best": list(stats["best"]) if stats["best"] else None,
            "worst": list(stats["worst"]) if stats["worst"] else None,
            "days": stats["days"], "green": stats["green"], "red": stats["red"],
            "daily": daily,   # {YYYY-MM-DD: pnl} — live, for the calendar
            # Live trade-by-trade log + trade-based stats for the "Verified" section.
            "trades": [{"side": t.get("side"), "date": _et_day(t.get("closed_at")),
                        "entry": t.get("entry_price"), "exit": t.get("exit_price"),
                        "qty": t.get("qty"), "pnl": t.get("pnl")} for t in trades],
            "trade_net_disp": _money(ts["net"]), "trade_count": ts["trades"],
            "trade_win_rate": ts["win_rate"], "trade_pf_disp": ts["pf_disp"],
            "trade_wins": ts["wins"], "trade_losses": ts["losses"],
            "updated": et.strftime("%b %d, %Y · %I:%M:%S %p ET"),
        }
    _TRACK_LIVE_CACHE.update(ts=now, data=data)
    return data


# Public, CORS-enabled so a SEPARATELY-HOSTED static marketing site can read it.
_TRACK_CORS = {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}


@app.get("/verified/live")
def verified_live():
    return JSONResponse(_track_payload(), headers=_TRACK_CORS)


@app.get("/verified/data")
def verified_data():
    return JSONResponse(_track_payload(), headers=_TRACK_CORS)


@app.get("/results", response_class=HTMLResponse)
def public_results_page():
    """Public marketing results page — serves the standalone verified-site file
    (documented backtest + live 'Verified via Tradovate' section). Read-only,
    cached; no auth, no effect on trading."""
    try:
        html = (BASE_DIR / "verified_site" / "index.html").read_text(encoding="utf-8")
    except Exception:
        html = "<!DOCTYPE html><h1 style='font-family:sans-serif'>Results are being set up — check back shortly.</h1>"
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=60"})


@app.get("/verified", response_class=HTMLResponse)
def verified_page(request: Request):
    user = _public_track_user()
    name = PUBLIC_TRACK_NAME
    if not user:
        body = "<div class='empty-note'>Results are being set up. Check back shortly.</div>"
        return HTMLResponse(_verified_shell(name, body), headers={"Cache-Control": "no-store"})

    # Scope to the track USER (email), not a volatile broker_accounts.id — survives reconnects.
    daily = ledger_daily_map(user["id"])
    stats = _track_stats(daily)
    live = public_live_snapshot(_public_connected_ids(user))

    # Calendar month: ?month=YYYY-MM, else latest data month, else current ET.
    mq = request.query_params.get("month", "")
    if re.match(r"^\d{4}-\d{2}$", mq or ""):
        cy, cm = int(mq[:4]), int(mq[5:7])
    elif daily:
        latest = max(daily.keys())
        cy, cm = int(latest[:4]), int(latest[5:7])
    else:
        nowet = datetime.now(timezone.utc).astimezone(ZoneInfo(_ET))
        cy, cm = nowet.year, nowet.month

    eq, run = [], 0.0
    for d in sorted(daily.keys()):
        run += daily[d]
        eq.append(round(run, 2))

    net_disp = _money(stats["net"])
    today_disp = _money(live["today"]) if live["today"] is not None else "—"
    equity_disp = _money(live["equity"]) if live["equity"] else "—"
    pf_disp = "∞" if stats["pf"] == float("inf") else (f"{stats['pf']:.2f}" if stats["pf"] is not None else "—")
    best_disp = (f"{stats['best'][0]} · {_money(stats['best'][1], 0)}" if stats["best"] else "—")
    worst_disp = (f"{stats['worst'][0]} · {_money(stats['worst'][1], 0)}" if stats["worst"] else "—")

    if not daily and live["today"] is None:
        inner = "<div class='empty-note'>📈 Live results begin accumulating as trades are taken. Check back soon — this page updates in real time.</div>"
    else:
        inner = f"""
      <div class="hero">
        <div class="card"><div class="k">Total Verified Net P&amp;L</div>
          <div id="net" class="big {('pos' if stats['net']>=0 else 'neg')}">{net_disp}</div>
          <div class="k">{stats['days']} trading days · {stats['green']}G / {stats['red']}R</div></div>
        <div class="card"><div class="k">Today (live)</div>
          <div id="today" class="big {('pos' if (live['today'] or 0)>0 else ('neg' if (live['today'] or 0)<0 else 'flat'))}">{today_disp}</div>
          <div class="k">Account equity: <span id="equity">{equity_disp}</span></div></div>
      </div>
      <div class="cards">
        <div class="card"><div class="k">Win Rate (days)</div><div class="v">{stats['win_rate']}%</div></div>
        <div class="card"><div class="k">Profit Factor</div><div class="v">{pf_disp}</div></div>
        <div class="card"><div class="k">Best Day</div><div class="v pos">{best_disp}</div></div>
        <div class="card"><div class="k">Worst Day</div><div class="v neg">{worst_disp}</div></div>
      </div>
      <div class="card" style="margin-bottom:16px;"><div class="sec-h">Equity Curve</div>
        <div class="eq-wrap">{_track_equity_svg(eq)}</div></div>
      <div class="card"><div class="sec-h">Daily P&amp;L Calendar</div>{_track_calendar_html(daily, cy, cm)}</div>
        """
    return HTMLResponse(_verified_shell(name, inner), headers={"Cache-Control": "no-store"})


def _verified_shell(name, inner):
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{name}</title><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>{_TRACK_CSS}</style></head><body><div class="wrap">
  <div class="topbar">
    <span class="brand">{name}</span>
    <span class="vbadge">✓ VERIFIED · LIVE FROM TRADOVATE</span>
    <span class="live"><span class="dot"></span>Updated <span id="upd">just now</span></span>
  </div>
  {inner}
  <div class="foot">Real brokerage results pulled directly from Tradovate via KhomaAPI — updated live every few seconds.<br>
  Past performance is not indicative of future results. Trading futures involves substantial risk.</div>
</div><script>{_TRACK_JS}</script></body></html>"""


@app.get("/health")
def health():
    return {
        "ok": True,
        "app": "KhomaAPI v5 Full SaaS Dashboard",
        "db": "postgres" if dbmod.IS_PG else "sqlite",
        "time_utc": datetime.now(timezone.utc).isoformat(),
    }


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _status_data() -> dict:
    """Aggregate, non-identifying system metrics for the public status page."""
    con = db()
    # Broker connectivity: operational if any account had a recent heartbeat.
    recent_cut = (datetime.now(timezone.utc) - timedelta(seconds=HEARTBEAT_STALE)).isoformat()
    total = con.execute("SELECT COUNT(*) c FROM broker_accounts WHERE status='connected'").fetchone()["c"]
    live = con.execute(
        "SELECT COUNT(*) c FROM broker_accounts WHERE status='connected' AND last_heartbeat >= ?",
        (recent_cut,),
    ).fetchone()["c"]
    # Avg execution latency over the last ~500 executed orders (no user data).
    row = con.execute(
        "SELECT AVG(latency_ms) a, COUNT(*) c FROM (SELECT latency_ms FROM trades WHERE status='EXECUTED' AND latency_ms>0 ORDER BY id DESC LIMIT 500) AS recent"
    ).fetchone()
    con.close()
    avg_latency = round(row["a"], 1) if row and row["a"] else None

    if total == 0:
        broker = ("operational", "Operational")
    elif live > 0:
        broker = ("operational", "Operational")
    else:
        broker = ("degraded", "Degraded — no recent broker heartbeat")
    return {
        "uptime": _fmt_uptime(time.time() - APP_START_TIME),
        "api": ("operational", "Operational"),
        "broker": broker,
        "avg_latency": avg_latency,
        "executions_sampled": row["c"] if row else 0,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


@app.get("/api/status")
def api_status():
    return _status_data()


@app.get("/status", response_class=HTMLResponse)
def status_page():
    d = _status_data()

    def dot(state):
        color = "#16a34a" if state == "operational" else ("#ca8a04" if state == "degraded" else "#dc2626")
        return f"<span style='display:inline-block;width:11px;height:11px;border-radius:999px;background:{color};margin-right:9px;'></span>"

    overall_ok = d["api"][0] == "operational" and d["broker"][0] == "operational"
    headline = "All systems operational" if overall_ok else "Partial degradation"
    head_color = "#0f8f45" if overall_ok else "#ca8a04"
    lat = f"{d['avg_latency']} ms" if d["avg_latency"] is not None else "—"

    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>KhomaAPI Status</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;background:#0b1220;color:#e5e7eb;}}
.wrap{{max-width:680px;margin:0 auto;padding:56px 20px;}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:30px;}}
.brand img{{width:40px;height:40px;border-radius:10px;}}
.brand h1{{font-size:20px;margin:0;letter-spacing:-.4px;}}
.head{{background:{head_color};border-radius:16px;padding:22px 24px;font-size:22px;font-weight:800;color:#fff;margin-bottom:24px;}}
.card{{background:#111a2e;border:1px solid #1f2a44;border-radius:14px;padding:18px 22px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center;}}
.card b{{font-weight:700;}} .muted{{color:#93a1b5;font-size:13px;}}
.metrics{{display:flex;gap:12px;margin-top:8px;}} .metric{{flex:1;background:#111a2e;border:1px solid #1f2a44;border-radius:14px;padding:18px 22px;}}
.metric .v{{font-size:26px;font-weight:850;letter-spacing:-1px;}} .foot{{color:#6b7a90;font-size:12px;margin-top:24px;text-align:center;}}
@media(max-width:520px){{ .wrap{{padding:34px 16px;}} .head{{font-size:19px;padding:18px 18px;}} .metrics{{flex-direction:column;}} .card{{padding:16px 18px;}} }}
</style></head><body><div class="wrap">
  <div class="brand"><img src="/static/logo.png" alt="KhomaAPI"><h1>KhomaAPI Status</h1></div>
  <div class="head">{headline}</div>
  <div class="card"><div>{dot(d['api'][0])}<b>Execution API</b></div><span class="muted">{d['api'][1]}</span></div>
  <div class="card"><div>{dot(d['broker'][0])}<b>Broker Connectivity (Tradovate)</b></div><span class="muted">{d['broker'][1]}</span></div>
  <div class="metrics">
    <div class="metric"><div class="muted">Uptime</div><div class="v">{d['uptime']}</div></div>
    <div class="metric"><div class="muted">Avg execution latency</div><div class="v">{lat}</div></div>
  </div>
  <div class="card" style="margin-top:12px;"><div><b>Incidents</b></div><span class="muted">No incidents reported</span></div>
  <p class="foot">Last updated {d['as_of']} · KhomaAlgorithms</p>
</div></body></html>""")


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
    link = f"{APP_URL}/confirm-email-change/{token}"
    sent = send_branded_email(
        user["email"],
        "Confirm your KhomaAPI email change",
        "Confirm your email change",
        f"You requested to change your KhomaAPI email to <b>{new_email}</b>. Confirm from your current address to continue.<br><br>If this wasn't you, you can safely ignore this email.",
        button_label="Confirm email change",
        button_url=link,
        text_fallback=f"You requested to change your email to {new_email}.\n\nConfirm from your current address:\n{link}\n\nIf this wasn't you, ignore this email.",
    )
    if sent:
        return login_layout("<h1>Confirm at your current email</h1><p>We sent a confirmation link to your current address. After you confirm, we'll email the new address to verify it.</p><a class='btn' href='/settings'>Back to Settings</a>")
    # Email delivery failed — you're already authenticated, so apply directly.
    con = db()
    con.execute("UPDATE users SET email=? WHERE id=?", (new_email, user["id"]))
    con.commit()
    con.close()
    return login_layout(f"<h1>Email Updated</h1><p>We couldn't send the confirmation email, so the change was applied directly. (Email error: {LAST_EMAIL_ERROR})</p><a class='btn' href='/settings'>Return to Settings</a>")


@app.get("/confirm-email-change/{token}", response_class=HTMLResponse)
def confirm_email_change(token: str):
    row = consume_email_token(token, kind="email_old")
    if not row:
        return login_layout("<h1>Link invalid or expired</h1><a href='/settings'>Back</a>")
    new_email = row["payload"]
    # Step 2 of 2: verify at the NEW email address.
    token2 = create_email_token(row["user_id"], "email_new", new_email)
    link = f"{APP_URL}/verify-email-change/{token2}"
    send_branded_email(
        new_email,
        "Verify your new KhomaAPI email",
        "Verify your new email",
        "Verify this address to finish your KhomaAPI email change.<br><br>This link expires in 1 hour.",
        button_label="Verify this email",
        button_url=link,
        text_fallback=f"Verify this address to finish your KhomaAPI email change:\n\n{link}\n\nThis link expires in 1 hour.",
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
    link = f"{APP_URL}/confirm-password-change/{token}"
    sent = send_branded_email(
        user["email"],
        "Confirm your KhomaAPI password change",
        "Confirm your password change",
        "Click below to confirm your new KhomaAPI password.<br><br>If this wasn't you, change your password immediately and ignore this email.",
        button_label="Confirm password change",
        button_url=link,
        text_fallback=f"Confirm your password change:\n\n{link}\n\nIf this wasn't you, change your password immediately and ignore this link.",
    )
    if sent:
        return login_layout("<h1>Confirm via email</h1><p>We emailed you a link to confirm the password change.</p><a class='btn' href='/settings'>Back to Settings</a>")
    # Email delivery failed — you're authenticated (gave current password), so apply directly.
    con = db()
    con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user["id"]))
    con.commit()
    con.close()
    return login_layout(f"<h1>Password Changed</h1><p>We couldn't send the confirmation email, so the change was applied directly. (Email error: {LAST_EMAIL_ERROR})</p><a class='btn' href='/settings'>Return to Settings</a>")


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
        link = f"{APP_URL}/reset-password/{token}"
        send_branded_email(
            email,
            "Reset your KhomaAPI password",
            "Reset your password",
            "We received a request to reset your KhomaAPI password. Click below to choose a new one.<br><br>This link expires in 1 hour. If you didn't request this, you can safely ignore this email.",
            button_label="Reset my password",
            button_url=link,
            text_fallback=f"Reset your password (link expires in 1 hour):\n\n{link}",
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
    # WebSockets bypass the HTTP middleware, so apply the SAME access rule here:
    # authenticate via the session cookie and require an active subscription.
    if ENFORCE_SUBSCRIPTIONS:
        from starlette.concurrency import run_in_threadpool

        def _allowed():
            uid = get_session_user_id(websocket.cookies.get("khoma_session"))
            if not uid:
                return False
            con = db()
            u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            con.close()
            return bool(u and user_entitlements(u).active)

        if not await run_in_threadpool(_allowed):
            await websocket.close(code=1008)  # policy violation
            return

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


@app.get("/debug/email")
def debug_email(request: Request):
    """Send a test email to the logged-in user and report the exact result, so
    SMTP misconfiguration is diagnosable without reading server logs."""
    user = require_user(request)
    if not user:
        return {"ok": False, "error": "Not logged in"}
    provider = "resend" if os.getenv("RESEND_API_KEY") else ("smtp" if os.getenv("SMTP_HOST") else "none")
    configured = {
        "RESEND_API_KEY": bool(os.getenv("RESEND_API_KEY")),
        "EMAIL_FROM": email_from(),
        "SMTP_HOST": bool(os.getenv("SMTP_HOST")),
        "SMTP_PORT": os.getenv("SMTP_PORT", "587"),
        "SMTP_USER": bool(os.getenv("SMTP_USER")),
        "SMTP_PASS": bool(os.getenv("SMTP_PASS")),
    }
    sent = send_branded_email(
        user["email"],
        "KhomaAPI email test",
        "Your email is working 🎉",
        "This is a branded KhomaAPI test email. If you can see the logo banner and this styled layout, outbound email and HTML rendering are both working.",
        button_label="Open KhomaAPI",
        button_url=f"{APP_URL}/dashboard",
        text_fallback="This is a KhomaAPI email test. If you received this, outbound email works.",
    )
    return {
        "ok": sent,
        "provider": provider,
        "email_enabled": email_enabled(),
        "sent_to": user["email"] if sent else None,
        "error": None if sent else LAST_EMAIL_ERROR,
        "configured": configured,
    }


@app.get("/api/live/monitor")
def api_live_monitor(request: Request):
    user = require_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"ok": False, "error": "Not logged in"})
    sel = request.query_params.get("account", "all") or "all"
    only = None if sel in ("all", "") else sel
    try:
        return live_account_monitor(user["id"], only_account_id=only)
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
    token = ensure_fresh_token(a)
    env = a.get("env") or "live"
    return {
        "ok": True,
        "account": a["account_name"],
        "env": env,
        "positions": tvo.get_positions(env, token),
        "fills": tvo.get_fills(env, token),
        "orders": tvo.get_orders(env, token),
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

