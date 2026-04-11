#!/usr/bin/env python3
"""
Database provisioning for AI Hub apps.

Creates a scoped Postgres user per app with:
  - Its own schema (app_<name>)
  - SELECT, INSERT, UPDATE on its own tables
  - CREATE on its own schema (so the app can create tables)
  - No access to public schema or other app schemas
  - No DELETE, DROP, TRUNCATE

Usage:
    python scripts/db_provision.py create myapp
    python scripts/db_provision.py create myapp --dry-run
    python scripts/db_provision.py drop myapp
    python scripts/db_provision.py drop myapp --dry-run
"""

import argparse
import os
import secrets
import stat
import string
import sys

try:
    import psycopg2
except ImportError:
    psycopg2 = None


# Connection defaults — overridable via env vars
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "aihub")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "aihub_admin")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "aihub_local_dev")

APPS_DIR = os.environ.get("APPS_DIR", os.path.join(os.path.dirname(__file__), "..", "apps"))


def _generate_password(length=32):
    """Generate a cryptographically random password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _get_connection():
    """Get admin connection to shared Postgres."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not installed — run: pip install psycopg2-binary")
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )


def _sanitize_identifier(name):
    """Sanitize an app name for use as a Postgres identifier.
    Replaces hyphens with underscores and validates the result."""
    import re
    sanitized = name.replace("-", "_")
    if not re.match(r'^[a-z][a-z0-9_]*$', sanitized):
        raise ValueError(f"Invalid identifier: {name}")
    return sanitized


def _get_sql_create(db_user, password, schema):
    """Return (sql_template, params) tuples for creating a scoped app user.
    Uses quoted identifiers and parameterized password to prevent injection."""
    # Postgres identifiers must be quoted to be safe
    q_user = f'"{db_user}"'
    q_schema = f'"{schema}"'
    return [
        (f"CREATE SCHEMA IF NOT EXISTS {q_schema}", None),
        (f"CREATE USER {q_user} WITH PASSWORD %s", (password,)),
        (f"GRANT USAGE, CREATE ON SCHEMA {q_schema} TO {q_user}", None),
        (f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {q_schema} TO {q_user}", None),
        (f"ALTER DEFAULT PRIVILEGES IN SCHEMA {q_schema} GRANT SELECT, INSERT, UPDATE ON TABLES TO {q_user}", None),
        (f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {q_schema} TO {q_user}", None),
        (f"ALTER DEFAULT PRIVILEGES IN SCHEMA {q_schema} GRANT USAGE ON SEQUENCES TO {q_user}", None),
        (f"REVOKE ALL ON SCHEMA public FROM {q_user}", None),
    ]


def _get_sql_drop(db_user, schema):
    """Return (sql_template, params) tuples for dropping an app user and schema."""
    q_user = f'"{db_user}"'
    q_schema = f'"{schema}"'
    return [
        (f"REASSIGN OWNED BY {q_user} TO {POSTGRES_USER}", None),
        (f"DROP OWNED BY {q_user}", None),
        (f"DROP USER IF EXISTS {q_user}", None),
        (f"DROP SCHEMA IF EXISTS {q_schema} CASCADE", None),
    ]


def create_app_user(app_name, dry_run=False):
    """
    Create a scoped DB user for an app.
    Returns dict with credentials on success, or error dict.
    """
    safe_name = _sanitize_identifier(app_name)
    db_user = f"app_{safe_name}"
    schema = f"app_{safe_name}"
    password = _generate_password()

    sql_statements = _get_sql_create(db_user, password, schema)

    if dry_run:
        print(f"  [dry-run] Would create Postgres user '{db_user}' with schema '{schema}'")
        print(f"  [dry-run] SQL statements:")
        for sql, params in sql_statements:
            display = sql.replace("%s", "********") if params else sql
            print(f"    {display}")
        env_path = os.path.join(os.path.abspath(APPS_DIR), app_name, ".env")
        print(f"  [dry-run] Would write credentials to {env_path}")
        return {
            "status": "dry_run",
            "db_user": db_user,
            "schema": schema,
        }

    conn = _get_connection()
    conn.autocommit = True
    cursor = conn.cursor()

    try:
        for sql, params in sql_statements:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
    except Exception as e:
        cursor.close()
        conn.close()
        return {"error": f"DB provisioning failed: {e}"}

    cursor.close()
    conn.close()

    # Write credentials to the app's .env file
    env_path = os.path.join(os.path.abspath(APPS_DIR), app_name, ".env")
    os.makedirs(os.path.dirname(env_path), exist_ok=True)

    # Append DB credentials (don't overwrite existing .env)
    db_lines = [
        f"\n# AI Hub shared database — auto-provisioned\n",
        f"DATABASE_URL=postgresql://{db_user}:{password}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}?options=-csearch_path%3D{schema}\n",
        f"DB_USER={db_user}\n",
        f"DB_PASSWORD={password}\n",
        f"DB_HOST={POSTGRES_HOST}\n",
        f"DB_PORT={POSTGRES_PORT}\n",
        f"DB_NAME={POSTGRES_DB}\n",
        f"DB_SCHEMA={schema}\n",
    ]

    with open(env_path, "a") as f:
        f.writelines(db_lines)

    # Restrict file permissions (owner read/write only)
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)

    print(f"  Created DB user '{db_user}' with schema '{schema}'")
    print(f"  Credentials written to {env_path}")

    return {
        "status": "created",
        "db_user": db_user,
        "schema": schema,
        "env_path": env_path,
    }


def drop_app_user(app_name, dry_run=False):
    """Drop an app's DB user and schema. Returns result dict."""
    safe_name = _sanitize_identifier(app_name)
    db_user = f"app_{safe_name}"
    schema = f"app_{safe_name}"

    sql_statements = _get_sql_drop(db_user, schema)

    if dry_run:
        print(f"  [dry-run] Would drop Postgres user '{db_user}' and schema '{schema}'")
        print(f"  [dry-run] SQL statements:")
        for sql, params in sql_statements:
            print(f"    {sql}")
        return {"status": "dry_run", "db_user": db_user}

    conn = _get_connection()
    conn.autocommit = True
    cursor = conn.cursor()

    try:
        for sql, params in sql_statements:
            if params:
                cursor.execute(sql, params)
            else:
                cursor.execute(sql)
    except Exception as e:
        cursor.close()
        conn.close()
        return {"error": f"DB drop failed: {e}"}

    cursor.close()
    conn.close()

    print(f"  Dropped DB user '{db_user}' and schema '{schema}'")
    return {"status": "dropped", "db_user": db_user}


def main():
    parser = argparse.ArgumentParser(description="Provision scoped DB users for AI Hub apps")
    sub = parser.add_subparsers(dest="command", required=True)

    create_p = sub.add_parser("create", help="Create a scoped DB user for an app")
    create_p.add_argument("app_name", help="App slug (e.g. 'myapp')")
    create_p.add_argument("--dry-run", action="store_true", help="Print SQL without executing")

    drop_p = sub.add_parser("drop", help="Drop an app's DB user and schema")
    drop_p.add_argument("app_name", help="App slug")
    drop_p.add_argument("--dry-run", action="store_true", help="Print SQL without executing")

    args = parser.parse_args()

    if args.command == "create":
        result = create_app_user(args.app_name, dry_run=args.dry_run)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "drop":
        result = drop_app_user(args.app_name, dry_run=args.dry_run)
        if "error" in result:
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
