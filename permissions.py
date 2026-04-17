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

        CREATE TABLE IF NOT EXISTS hidden_users (
            email TEXT PRIMARY KEY,
            hidden_by TEXT,
            hidden_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_labels (
            email TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS app_registry (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT
        );

        CREATE TABLE IF NOT EXISTS app_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            icon TEXT DEFAULT '',
            port INTEGER NOT NULL,
            streamlit_port INTEGER,
            repo_url TEXT DEFAULT '',
            repo_subdir TEXT DEFAULT '',
            env_keys TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            submitted_by TEXT NOT NULL,
            reviewed_by TEXT,
            submitted_at TEXT DEFAULT (datetime('now')),
            reviewed_at TEXT
        );
    """)

    # Migrate: add columns if missing
    for col in ["icon TEXT DEFAULT ''", "repo_subdir TEXT DEFAULT ''", "streamlit_port INTEGER"]:
        try:
            conn.execute(f"ALTER TABLE app_submissions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # Column already exists

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


# ── App Submissions ──

def submit_app(slug, name, description, icon, port, repo_url, repo_subdir, env_keys, submitted_by, streamlit_port=None):
    """Submit a new app or update an existing one for review.
    If the slug already exists and is live/approved/error, resets it to pending (update flow).

    streamlit_port: optional second host port to publish from the container.
    Used by Streamlit apps that run Flask on `port` and Streamlit on a separate
    port for WebSocket traffic. If set, deploy.py will publish it as
    -p {streamlit_port}:{streamlit_port}.
    """
    conn = get_db()
    existing = conn.execute(
        "SELECT id, status FROM app_submissions WHERE slug = ?", (slug,)
    ).fetchone()

    if existing:
        if existing["status"] in ("live", "approved", "error"):
            # Update flow — reset to pending with new details
            conn.execute(
                """UPDATE app_submissions
                   SET name = ?, description = ?, icon = ?, port = ?, streamlit_port = ?, repo_url = ?, repo_subdir = ?, env_keys = ?,
                       submitted_by = ?, status = 'pending',
                       reviewed_by = NULL, reviewed_at = NULL,
                       submitted_at = datetime('now')
                   WHERE slug = ?""",
                (name, description, icon, port, streamlit_port, repo_url, repo_subdir or "", env_keys, submitted_by, slug),
            )
            conn.commit()
            conn.close()
            return {"status": "submitted", "update": True}
        elif existing["status"] == "pending":
            conn.close()
            return {"error": f"App '{slug}' already has a pending submission"}
        elif existing["status"] == "rejected":
            # Allow resubmission after rejection
            conn.execute(
                """UPDATE app_submissions
                   SET name = ?, description = ?, icon = ?, port = ?, streamlit_port = ?, repo_url = ?, repo_subdir = ?, env_keys = ?,
                       submitted_by = ?, status = 'pending',
                       reviewed_by = NULL, reviewed_at = NULL,
                       submitted_at = datetime('now')
                   WHERE slug = ?""",
                (name, description, icon, port, streamlit_port, repo_url, repo_subdir or "", env_keys, submitted_by, slug),
            )
            conn.commit()
            conn.close()
            return {"status": "submitted", "resubmit": True}

    try:
        conn.execute(
            """INSERT INTO app_submissions (slug, name, description, icon, port, streamlit_port, repo_url, repo_subdir, env_keys, submitted_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, name, description, icon, port, streamlit_port, repo_url, repo_subdir or "", env_keys, submitted_by),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return {"error": f"App slug '{slug}' already exists"}
    conn.close()
    return {"status": "submitted"}


def get_pending_submissions():
    """Get all pending app submissions."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM app_submissions WHERE status = 'pending' ORDER BY submitted_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_submissions():
    """Get all app submissions regardless of status."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM app_submissions ORDER BY submitted_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_submission(submission_id, reviewed_by):
    """Approve an app submission and register it."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM app_submissions WHERE id = ? AND status = 'pending'",
        (submission_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": "Submission not found or already reviewed"}

    # Mark as approved (will be set to 'live' after deploy callback)
    conn.execute(
        "UPDATE app_submissions SET status = 'approved', reviewed_by = ?, reviewed_at = datetime('now') WHERE id = ?",
        (reviewed_by, submission_id),
    )
    # Register the app
    conn.execute(
        "INSERT OR REPLACE INTO app_registry (slug, name, description) VALUES (?, ?, ?)",
        (row["slug"], row["name"], row["description"]),
    )
    conn.commit()
    conn.close()
    return {"status": "approved", "slug": row["slug"], "port": row["port"], "repo_url": row["repo_url"]}


def mark_submission_live(submission_id):
    """Mark a submission as live after successful deploy."""
    conn = get_db()
    conn.execute(
        "UPDATE app_submissions SET status = 'live' WHERE id = ?",
        (submission_id,),
    )
    conn.commit()
    conn.close()


def mark_submission_error(submission_id):
    """Mark a submission as errored after failed deploy."""
    conn = get_db()
    conn.execute(
        "UPDATE app_submissions SET status = 'error' WHERE id = ?",
        (submission_id,),
    )
    conn.commit()
    conn.close()


def reject_submission(submission_id, reviewed_by, reason=""):
    """Reject an app submission with an optional reason."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM app_submissions WHERE id = ? AND status = 'pending'",
        (submission_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": "Submission not found or already reviewed"}

    conn.execute(
        "UPDATE app_submissions SET status = 'rejected', reviewed_by = ?, reviewed_at = datetime('now') WHERE id = ?",
        (reviewed_by, submission_id),
    )
    # Store rejection reason if provided (add column if missing)
    if reason:
        try:
            conn.execute("ALTER TABLE app_submissions ADD COLUMN rejection_reason TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.execute("UPDATE app_submissions SET rejection_reason = ? WHERE id = ?", (reason, submission_id))
    conn.commit()
    conn.close()
    return {"status": "rejected"}


def delete_submission(submission_id):
    """Delete an app submission and its registry entry."""
    conn = get_db()
    row = conn.execute(
        "SELECT slug, status FROM app_submissions WHERE id = ?",
        (submission_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": "Submission not found"}

    slug = row["slug"]

    # Remove from app_registry
    conn.execute("DELETE FROM app_registry WHERE slug = ?", (slug,))
    # Remove the submission
    conn.execute("DELETE FROM app_submissions WHERE id = ?", (submission_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "slug": slug, "was_live": row["status"] == "live"}


def edit_submission(submission_id, slug=None, name=None, description=None, icon=None, port=None, streamlit_port=None, repo_url=None, env_keys=None):
    """Edit fields on an existing submission. Supports slug changes."""
    conn = get_db()
    row = conn.execute("SELECT * FROM app_submissions WHERE id = ?", (submission_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "Submission not found"}

    old_slug = row["slug"]
    new_slug = slug if (slug and slug != old_slug) else None

    # Validate new slug if changing
    if new_slug:
        import re
        if not re.match(r'^[a-z][a-z0-9-]{1,30}$', new_slug):
            conn.close()
            return {"error": "Slug must be lowercase letters, numbers, hyphens. 2-31 chars, start with letter."}
        existing = conn.execute("SELECT 1 FROM app_submissions WHERE slug = ? AND id != ?", (new_slug, submission_id)).fetchone()
        if existing:
            conn.close()
            return {"error": f"Slug '{new_slug}' is already taken"}

    updates = {}
    if new_slug:
        updates["slug"] = new_slug
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if icon is not None:
        updates["icon"] = icon
    if port is not None:
        updates["port"] = port
    if streamlit_port is not None:
        # Treat empty string or 0 as "clear" → NULL
        updates["streamlit_port"] = streamlit_port if streamlit_port else None
    if repo_url is not None:
        updates["repo_url"] = repo_url
    if env_keys is not None:
        updates["env_keys"] = env_keys

    if not updates:
        conn.close()
        return {"error": "No fields to update"}

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [submission_id]
    conn.execute(f"UPDATE app_submissions SET {set_clause} WHERE id = ?", values)

    # Update app_registry
    if new_slug:
        conn.execute("UPDATE app_registry SET slug = ? WHERE slug = ?", (new_slug, old_slug))
        conn.execute("UPDATE app_permissions SET app_slug = ? WHERE app_slug = ?", (new_slug, old_slug))
    if "name" in updates or "description" in updates:
        target_slug = new_slug or old_slug
        conn.execute(
            "UPDATE app_registry SET name = COALESCE(?, name), description = COALESCE(?, description) WHERE slug = ?",
            (updates.get("name"), updates.get("description"), target_slug),
        )

    conn.commit()
    conn.close()

    result = {"status": "updated"}
    if new_slug:
        result["old_slug"] = old_slug
        result["new_slug"] = new_slug
        result["note"] = "Slug changed. If this app is deployed, undeploy and redeploy with the new slug."
    return result


# Initialize on import
init_db()
