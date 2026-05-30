"""One-time data migration: SQLite -> Postgres.

Run AFTER provisioning Postgres and setting DATABASE_URL, to copy your existing
users / accounts / trades / notes from the SQLite file into Postgres.

Usage (on Railway shell, or anywhere both are reachable):
    DATABASE_URL=postgres://...  KHOMA_SQLITE_PATH=/path/to/khomaapi_v31.db \
        python -m scripts.migrate_to_postgres

It is idempotent (ON CONFLICT DO NOTHING) and resets the id sequences afterward,
so it's safe to run more than once.
"""

import os
import sqlite3

if not os.getenv("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
    raise SystemExit("Set DATABASE_URL to your Postgres URL first.")

# Importing app.main creates the Postgres schema (init_db runs on import).
from app import db as dbmod  # noqa: E402
import app.main  # noqa: E402,F401

SQLITE_PATH = os.getenv("KHOMA_SQLITE_PATH") or str(app.main.DB_PATH)

# Copy order doesn't matter (no hard FKs), but keep users first for sanity.
TABLES = [
    "users", "brokers", "broker_accounts", "trades", "locks", "sessions",
    "email_verifications", "password_resets", "trade_notes", "trip_journal",
    "account_risk_config", "news_windows",
]
SERIAL_ID_TABLES = ["users", "broker_accounts", "trades", "trade_notes",
                    "trip_journal", "news_windows"]


def main():
    sq = sqlite3.connect(SQLITE_PATH)
    sq.row_factory = sqlite3.Row
    pg = dbmod.connect("unused")

    sqlite_tables = {r["name"] for r in sq.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    for table in TABLES:
        if table not in sqlite_tables:
            print(f"skip {table} (not in SQLite)")
            continue
        rows = sq.execute(f"SELECT * FROM {table}").fetchall()
        n = 0
        for r in rows:
            cols = list(r.keys())
            ph = ",".join(["?"] * len(cols))
            collist = ",".join(cols)
            pg.execute(
                f"INSERT INTO {table} ({collist}) VALUES ({ph}) ON CONFLICT DO NOTHING",
                tuple(r[c] for c in cols),
            )
            n += 1
        pg.commit()
        print(f"migrated {table}: {n} rows")

    # Advance SERIAL sequences past the imported max(id).
    for table in SERIAL_ID_TABLES:
        try:
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
            )
            pg.commit()
        except Exception as e:
            print(f"seq reset {table}: {e}")

    sq.close()
    pg.close()
    print("DONE. Verify by logging in on the live site.")


if __name__ == "__main__":
    main()
