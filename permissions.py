"""
Permissions system for egelloC AI Hub.

Uses SQLite for permissions storage. Pulls user list from MySQL.
Apps are registered here and permissions are managed via /admin.
"""

import os
import sqlite3
from datetime import datetime

# ── App Registry ──
# Add new apps here as they're built
APPS = [
    {"slug": "hub", "name": "Tech Knowledge Base", "description": "Documentation, architecture, team resources, and internal tools"},
    {"slug": "briefer", "name": "Coaching Briefer", "description": "Pre-call briefings with AI summaries"},
    {"slug": "admin", "name": "Admin Panel", "description": "Manage user permissions"},
]

DB_PATH = os.path.join(os.path.dirname(__file__), "permissions.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_permissions (
            email TEXT NOT NULL,
            app_slug TEXT NOT NULL,
            granted_by TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (email, app_slug)
        );

        CREATE TABLE IF NOT EXISTS custom_users (
            email TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            role TEXT DEFAULT '',
            added_by TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS app_registry (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT
        );
    """)

    # Upsert app registry
    for app in APPS:
        conn.execute(
            "INSERT OR REPLACE INTO app_registry (slug, name, description) VALUES (?, ?, ?)",
            (app["slug"], app["name"], app["description"]),
        )

    conn.commit()
    conn.close()


def get_user_permissions(email):
    """Get list of app slugs this user has access to."""
    conn = get_db()
    rows = conn.execute(
        "SELECT app_slug FROM app_permissions WHERE email = ? COLLATE NOCASE",
        (email.lower(),),
    ).fetchall()
    conn.close()
    return [r["app_slug"] for r in rows]


def user_has_permission(email, app_slug):
    """Check if a user has access to a specific app."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM app_permissions WHERE email = ? COLLATE NOCASE AND app_slug = ?",
        (email.lower(), app_slug),
    ).fetchone()
    conn.close()
    return row is not None


def grant_permission(email, app_slug, granted_by):
    """Grant a user access to an app."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO app_permissions (email, app_slug, granted_by) VALUES (?, ?, ?)",
        (email.lower(), app_slug, granted_by),
    )
    conn.commit()
    conn.close()


def revoke_permission(email, app_slug):
    """Revoke a user's access to an app."""
    conn = get_db()
    conn.execute(
        "DELETE FROM app_permissions WHERE email = ? COLLATE NOCASE AND app_slug = ?",
        (email.lower(), app_slug),
    )
    conn.commit()
    conn.close()


def get_all_permissions():
    """Get all permissions grouped by user."""
    conn = get_db()
    rows = conn.execute("""
        SELECT email, app_slug, granted_by, created_at
        FROM app_permissions
        ORDER BY email, app_slug
    """).fetchall()
    conn.close()

    result = {}
    for r in rows:
        email = r["email"]
        if email not in result:
            result[email] = []
        result[email].append({
            "app_slug": r["app_slug"],
            "granted_by": r["granted_by"],
            "created_at": r["created_at"],
        })
    return result


def get_all_apps():
    """Get all registered apps."""
    conn = get_db()
    rows = conn.execute("SELECT slug, name, description FROM app_registry ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_egelloc_staff(mysql_conn):
    """Pull staff users (admin, super_admin, coach, CX, Strategist) from MySQL."""
    cursor = mysql_conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT u.email, u.first_name, u.last_name, u.status, u.avatar,
               GROUP_CONCAT(DISTINCT r.name) as roles
        FROM users u
        LEFT JOIN model_has_roles mhr ON mhr.model_id = u.id
        LEFT JOIN roles r ON r.id = mhr.role_id
        WHERE u.email LIKE '%%@egelloc.com'
          AND u.deleted_at IS NULL
          AND u.status = 'active'
        GROUP BY u.id
        HAVING roles REGEXP 'admin|super_admin|coach|CX|Strategist'
        ORDER BY u.first_name
    """)
    return cursor.fetchall()


def add_custom_user(email, first_name, last_name, role, added_by):
    """Add a user manually (not from MySQL)."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO custom_users (email, first_name, last_name, role, added_by) VALUES (?, ?, ?, ?, ?)",
        (email.lower(), first_name, last_name, role, added_by),
    )
    conn.commit()
    conn.close()


def remove_custom_user(email):
    """Remove a manually added user."""
    conn = get_db()
    conn.execute("DELETE FROM custom_users WHERE email = ? COLLATE NOCASE", (email.lower(),))
    conn.execute("DELETE FROM app_permissions WHERE email = ? COLLATE NOCASE", (email.lower(),))
    conn.commit()
    conn.close()


def get_custom_users():
    """Get all manually added users."""
    conn = get_db()
    rows = conn.execute("SELECT email, first_name, last_name, role, added_by, created_at FROM custom_users ORDER BY first_name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Initialize on import
init_db()
