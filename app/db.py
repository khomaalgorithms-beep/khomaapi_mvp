"""Dual-backend database layer for KhomaAPI.

Default: SQLite (unchanged behavior — zero risk to the current deployment).
When DATABASE_URL (postgres://...) is set: Postgres via psycopg3, for concurrent
writes + horizontal scaling at 1,000+ users.

The whole app keeps using `?` placeholders and `con.execute(...).fetchone()` —
this layer translates SQLite SQL to Postgres on the fly and returns dict-like
rows from both backends, so the 100+ existing call sites need no changes.
"""

import os
import re
import sqlite3

def _pick_db_url() -> str:
    """First env var that's a REAL postgres URL. Tolerates an unresolved
    ${{...}} reference in DATABASE_URL by falling through to the public URL."""
    for key in ("DATABASE_URL", "DATABASE_PUBLIC_URL"):
        v = os.getenv(key, "").strip()
        if v.startswith(("postgres://", "postgresql://")):
            return v
    return ""


DATABASE_URL = _pick_db_url()
IS_PG = bool(DATABASE_URL)

# Pool sizing. Railway Postgres caps total connections (default 100); keep the
# pool well under that so many app instances/workers can coexist. Tune via env.
POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "15"))

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row

_pool = None


def _get_pool():
    """Lazily build a process-wide Postgres connection pool. Reusing warm
    connections removes the per-query TCP+TLS+auth handshake (low latency) and
    caps total connections so we never exhaust Postgres at 1,000+ clients."""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            timeout=POOL_TIMEOUT,
            max_idle=300,
            kwargs={"connect_timeout": 10},
            open=False,
            name="khoma",
        )
        # Don't block boot if Railway's private net is still warming up;
        # getconn() will wait/retry per checkout.
        _pool.open(wait=False)
    return _pool


def _translate(sql: str) -> str:
    """Rewrite SQLite SQL to Postgres-compatible SQL. No-op for SQLite."""
    if not IS_PG:
        return sql
    s = sql
    # PRAGMAs (e.g. journal_mode=WAL) are SQLite-only -> harmless no-op.
    if s.lstrip().upper().startswith("PRAGMA"):
        return "SELECT 1"
    # Escape LITERAL percent signs (e.g. LIKE 'Risk:%') BEFORE adding %s
    # placeholders — psycopg treats a bare % as a placeholder marker.
    s = s.replace("%", "%%")
    # Placeholders.
    s = s.replace("?", "%s")
    # Auto-increment PK.
    s = re.sub(r"(?i)INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "SERIAL PRIMARY KEY", s)
    s = re.sub(r"(?i)\s+AUTOINCREMENT", "", s)
    # ALTER TABLE ... ADD COLUMN -> idempotent on Postgres (avoids aborting the
    # transaction when the column already exists).
    s = re.sub(r"(?i)(ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+)(?!IF\s+NOT\s+EXISTS)", r"\1IF NOT EXISTS ", s)
    # Upsert syntax.
    if re.search(r"(?i)INSERT\s+OR\s+IGNORE", s):
        s = re.sub(r"(?i)INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s)
        if "on conflict" not in s.lower():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    if re.search(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO\s+locks", s):
        s = re.sub(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s)
        if "on conflict" not in s.lower():
            s = s.rstrip().rstrip(";") + " ON CONFLICT (user_id,symbol,side,request_id) DO UPDATE SET ts=EXCLUDED.ts"
    s = re.sub(r"(?i)INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s)
    # `ON CONFLICT(cols)` -> `ON CONFLICT (cols)` (Postgres wants the space).
    s = re.sub(r"(?i)ON\s+CONFLICT\(", "ON CONFLICT (", s)
    return s


class _Cur:
    """Uniform cursor: .execute / .fetchone / .fetchall / .rowcount / .lastrowid."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        self._raw.execute(_translate(sql), params)
        return self

    def fetchone(self):
        try:
            return self._raw.fetchone()
        except Exception:
            return None  # e.g. INSERT without RETURNING on Postgres

    def fetchall(self):
        try:
            return self._raw.fetchall()
        except Exception:
            return []

    @property
    def rowcount(self):
        return self._raw.rowcount

    @property
    def lastrowid(self):
        return getattr(self._raw, "lastrowid", None)


class _Conn:
    """Uniform connection wrapper over sqlite3 / psycopg3."""

    def __init__(self, raw, pg, pooled=False):
        self._raw = raw
        self._pg = pg
        self._pooled = pooled

    def execute(self, sql, params=()):
        if self._pg:
            cur = self._raw.cursor(row_factory=dict_row)
            cur.execute(_translate(sql), params)
            return _Cur(cur)
        return _Cur(self._raw.execute(_translate(sql), params))

    def cursor(self):
        if self._pg:
            return _Cur(self._raw.cursor(row_factory=dict_row))
        return _Cur(self._raw.cursor())

    def commit(self):
        self._raw.commit()

    def close(self):
        if self._pooled:
            # Return the connection to the pool instead of closing it. Roll back
            # any uncommitted work first so the next checkout gets a clean slate.
            try:
                self._raw.rollback()
            except Exception:
                pass
            try:
                _get_pool().putconn(self._raw)
                return
            except Exception:
                pass  # pool gone / conn broken -> hard close below
        try:
            self._raw.close()
        except Exception:
            pass


def connect(sqlite_path):
    if IS_PG:
        # Primary path: check a warm connection out of the pool.
        try:
            raw = _get_pool().getconn(timeout=POOL_TIMEOUT)
            return _Conn(raw, True, pooled=True)
        except Exception:
            pass
        # Fallback: direct connect (internal first, then public proxy).
        try:
            return _Conn(psycopg.connect(DATABASE_URL, connect_timeout=10), True)
        except Exception:
            pub = os.getenv("DATABASE_PUBLIC_URL", "").strip()
            if pub.startswith(("postgres://", "postgresql://")):
                return _Conn(psycopg.connect(pub, connect_timeout=10), True)
            raise
    raw = sqlite3.connect(sqlite_path)
    raw.row_factory = sqlite3.Row
    return _Conn(raw, False)


def wait_for_db(sqlite_path, attempts: int = 20, delay: float = 2.0):
    """Connect with retries — Railway's private network (postgres.railway.internal)
    takes a few seconds to come up after a container boots, so connecting at the
    very first instant fails. Retry until it's ready instead of crashing."""
    import time as _t
    last = None
    for i in range(attempts):
        try:
            c = connect(sqlite_path)
            c.execute("SELECT 1").fetchone()
            return c
        except Exception as e:
            last = e
            print(f"DB not ready (attempt {i + 1}/{attempts}): {e}")
            _t.sleep(delay)
    raise RuntimeError(f"Could not connect to database after {attempts} attempts: {last}")


def insert_returning_id(cur, sql, params, idcol="id"):
    """INSERT and return the new row's id on BOTH backends (Postgres needs
    RETURNING; SQLite uses lastrowid)."""
    if IS_PG:
        cur.execute(sql.rstrip().rstrip(";") + f" RETURNING {idcol}", params)
        row = cur.fetchone()
        return row[idcol] if row else None
    cur.execute(sql, params)
    return cur.lastrowid
