#!/usr/bin/env python3
"""Verify the admin user can read the engine's user catalog on each managed DB.

Run on the server before deciding whether to revert list_db_users to a direct
SQL query. We need to know:

  - MySQL (Nest): can doadmin run SELECT user FROM mysql.user ?
  - Postgres (Acquisition): can doadmin run SELECT rolname FROM pg_roles ?

If both pass, reverting list_db_users to SQL is the clean fix for DRIFT
false-positives. If either fails, we keep the DO-API path and replace the
DRIFT badge with a per-row connection test instead.

Usage:
  python3 scripts/check_doadmin_select_user.py
"""
import os
import sys

# Same MANAGED_DATABASES env-var shape as server.py — keep in sync if that
# block changes.
CHECKS = [
    {
        "slug": "nest",
        "engine": "mysql",
        "host": os.environ.get("DB_HOST", ""),
        "port": int(os.environ.get("DB_PORT", "25060")),
        "database": os.environ.get("DB_NAME", "egelloc"),
        "admin_user": os.environ.get("DB_ADMIN_USER", "doadmin"),
        "admin_password": os.environ.get("DB_ADMIN_PASSWORD", ""),
    },
    {
        "slug": "acquisition",
        "engine": "pg",
        "host": os.environ.get("ACQ_DB_HOST", ""),
        "port": int(os.environ.get("ACQ_DB_PORT", "25060")),
        "database": os.environ.get("ACQ_DB_NAME", "defaultdb"),
        "admin_user": os.environ.get("ACQ_DB_ADMIN_USER", "doadmin"),
        "admin_password": os.environ.get("ACQ_DB_ADMIN_PASSWORD", ""),
    },
]


def check_mysql(c):
    import mysql.connector
    conn = mysql.connector.connect(
        host=c["host"], port=c["port"], database=c["database"],
        user=c["admin_user"], password=c["admin_password"],
        connection_timeout=10,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT user FROM mysql.user LIMIT 5")
        rows = [r[0] for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


def check_pg(c):
    import psycopg2
    conn = psycopg2.connect(
        host=c["host"], port=c["port"], dbname=c["database"],
        user=c["admin_user"], password=c["admin_password"],
        sslmode="require", connect_timeout=10,
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT rolname FROM pg_roles WHERE rolcanlogin = true LIMIT 5")
        rows = [r[0] for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


def main():
    overall_ok = True
    for c in CHECKS:
        slug = c["slug"]
        engine = c["engine"]
        if not c["host"] or not c["admin_password"]:
            print(f"[{slug}/{engine}] SKIP — missing host or admin password env")
            overall_ok = False
            continue
        try:
            rows = check_mysql(c) if engine == "mysql" else check_pg(c)
            print(f"[{slug}/{engine}] OK — read {len(rows)} rows: {rows}")
        except Exception as e:
            print(f"[{slug}/{engine}] FAIL — {type(e).__name__}: {e}")
            overall_ok = False
    print()
    print("VERDICT:", "revert to SQL is safe" if overall_ok else "do NOT revert — fall back to Test-Connection button")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
