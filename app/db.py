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

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row


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

    def __init__(self, raw, pg):
        self._raw = raw
        self._pg = pg

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
        self._raw.close()


def connect(sqlite_path):
    if IS_PG:
        return _Conn(psycopg.connect(DATABASE_URL), True)
    raw = sqlite3.connect(sqlite_path)
    raw.row_factory = sqlite3.Row
    return _Conn(raw, False)


def insert_returning_id(cur, sql, params, idcol="id"):
    """INSERT and return the new row's id on BOTH backends (Postgres needs
    RETURNING; SQLite uses lastrowid)."""
    if IS_PG:
        cur.execute(sql.rstrip().rstrip(";") + f" RETURNING {idcol}", params)
        row = cur.fetchone()
        return row[idcol] if row else None
    cur.execute(sql, params)
    return cur.lastrowid
