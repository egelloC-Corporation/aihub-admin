"""
Permissions system for egelloC Incubator.

Uses SQLite for permissions storage. Pulls user list from MySQL.
Apps are registered here and permissions are managed via /admin.
"""

import os
import json
import sqlite3
from datetime import datetime

# ── App Registry ──
# The default is incubator's historical set. Playground (or any new
# instance) can override via INSTANCE_APPS_JSON env var — a JSON array
# of {slug, name, description} dicts. init_db() reconciles app_registry
# with this list on every startup so stale rows don't linger when the
# override is changed.
_DEFAULT_APPS = [
    {"slug": "hub", "name": "Tech Knowledge Base", "description": "Documentation, architecture, team resources, and internal tools"},
    {"slug": "briefer", "name": "Coaching Briefer", "description": "Pre-call briefings with AI summaries"},
    {"slug": "admin", "name": "Admin Panel", "description": "Manage user permissions"},
]


def _load_apps():
    raw = os.environ.get("INSTANCE_APPS_JSON", "").strip()
    if not raw:
        return _DEFAULT_APPS
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(
            isinstance(x, dict) and "slug" in x and "name" in x for x in parsed
        ):
            # Fill in missing description field to keep downstream code simple.
            return [{"slug": x["slug"], "name": x["name"],
                     "description": x.get("description", "")} for x in parsed]
    except (ValueError, TypeError):
        pass
    return _DEFAULT_APPS


APPS = _load_apps()

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

        CREATE TABLE IF NOT EXISTS webhook_seen (
            repo_url TEXT PRIMARY KEY,
            last_seen TEXT DEFAULT (datetime('now'))
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

        CREATE TABLE IF NOT EXISTS user_role_overrides (
            email TEXT PRIMARY KEY,
            roles TEXT NOT NULL,
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
            reviewed_at TEXT,
            is_internal INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body TEXT NOT NULL,
            created_by TEXT NOT NULL,        -- author email
            created_by_name TEXT DEFAULT '', -- display name at post time
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)

    # Migrate: add columns if missing.
    # is_internal=1 marks background services (e.g. webhook ingesters, headless
    # APIs) that are deployed via the platform but should NOT appear in the
    # launcher cards or the per-user Permissions matrix — there's nothing to
    # grant access to, no UI to land on.
    for col in [
        "icon TEXT DEFAULT ''",
        "repo_subdir TEXT DEFAULT ''",
        "streamlit_port INTEGER",
        "is_internal INTEGER NOT NULL DEFAULT 0",
        "branch TEXT NOT NULL DEFAULT 'main'",
    ]:
        try:
            conn.execute(f"ALTER TABLE app_submissions ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Reconcile app registry with APPS: upsert each, then drop stale rows.
    # Without the DELETE, switching INSTANCE_APPS_JSON (e.g. shrinking to
    # just admin on playground) would leave hub/briefer behind in the UI.
    #
    # Also preserve user-submitted apps (app_submissions rows with status
    # live/approved) — they're inserted into app_registry at approval time
    # (see approve_submission) and were otherwise getting wiped on every
    # boot, causing the Permissions tab to lose apps until the next submit.
    # Backfill from app_submissions so a post-boot Permissions view always
    # matches the App Registry view.
    try:
        submission_apps = {
            r["slug"]: (r["name"], r["description"] or "")
            for r in conn.execute(
                "SELECT slug, name, description FROM app_submissions "
                "WHERE status IN ('live', 'approved') "
                # Background services (is_internal=1) are deployed but never
                # surfaced as user-permission targets — they have no UI for
                # users to land on. Excluded here so they don't show up as
                # columns in the Permissions matrix on next reconciliation.
                "AND COALESCE(is_internal, 0) = 0"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        submission_apps = {}  # app_submissions not yet migrated; safe default

    current_slugs = set(app["slug"] for app in APPS) | set(submission_apps.keys())
    for app in APPS:
        conn.execute(
            "INSERT OR REPLACE INTO app_registry (slug, name, description) VALUES (?, ?, ?)",
            (app["slug"], app["name"], app["description"]),
        )
    for slug, (name, desc) in submission_apps.items():
        # Only upsert if APPS didn't already provide this slug (APPS takes
        # precedence — lets the env override a submission's name/description).
        if slug not in (a["slug"] for a in APPS):
            conn.execute(
                "INSERT OR REPLACE INTO app_registry (slug, name, description) VALUES (?, ?, ?)",
                (slug, name, desc),
            )
    if current_slugs:
        placeholders = ",".join("?" * len(current_slugs))
        conn.execute(
            f"DELETE FROM app_registry WHERE slug NOT IN ({placeholders})",
            list(current_slugs),
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
    rows = conn.execute("SELECT slug, name, description FROM app_registry ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_egelloc_staff(mysql_conn):
    """Pull staff users (admin, super_admin, coach, CX, Strategist) from MySQL.

    Skipped when FEATURES_STAFF_SYNC=false (e.g. on playground — it's a
    student-facing box that shouldn't auto-list internal egelloC staff).
    """
    if os.environ.get("FEATURES_STAFF_SYNC", "true").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return []
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
    # Re-adding a user must un-hide them — a prior delete may have left a
    # hidden_users row that would otherwise filter them out of the list.
    conn.execute("DELETE FROM hidden_users WHERE email = ? COLLATE NOCASE", (email.lower(),))
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


# Announcements (launcher "Updates" sidebar)

def create_announcement(body, created_by, created_by_name=""):
    """Insert an announcement and return its new id."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO announcements (body, created_by, created_by_name) VALUES (?, ?, ?)",
        (body, created_by.lower(), created_by_name),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_announcements(limit=50):
    """Return announcements, newest first."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, body, created_by, created_by_name, created_at "
        "FROM announcements ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_announcement(announcement_id):
    """Delete an announcement by id."""
    conn = get_db()
    conn.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
    conn.commit()
    conn.close()


# ── App Submissions ──

def submit_app(slug, name, description, icon, port, repo_url, repo_subdir, env_keys, submitted_by, streamlit_port=None, branch="main"):
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
                       branch = ?, submitted_by = ?, status = 'pending',
                       reviewed_by = NULL, reviewed_at = NULL,
                       submitted_at = datetime('now')
                   WHERE slug = ?""",
                (name, description, icon, port, streamlit_port, repo_url, repo_subdir or "", env_keys, branch or "main", submitted_by, slug),
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
                       branch = ?, submitted_by = ?, status = 'pending',
                       reviewed_by = NULL, reviewed_at = NULL,
                       submitted_at = datetime('now')
                   WHERE slug = ?""",
                (name, description, icon, port, streamlit_port, repo_url, repo_subdir or "", env_keys, branch or "main", submitted_by, slug),
            )
            conn.commit()
            conn.close()
            return {"status": "submitted", "resubmit": True}

    try:
        conn.execute(
            """INSERT INTO app_submissions (slug, name, description, icon, port, streamlit_port, repo_url, repo_subdir, env_keys, branch, submitted_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, name, description, icon, port, streamlit_port, repo_url, repo_subdir or "", env_keys, branch or "main", submitted_by),
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
    # Register the app — but skip if it's an internal/background service.
    # init_db's reconciliation enforces the same rule on every boot; this
    # check just avoids momentarily exposing the slug as a permission target
    # between approval and the next reconciliation.
    is_internal = bool(row["is_internal"]) if "is_internal" in row.keys() else False
    if not is_internal:
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
    """Delete an app and every trace of it.

    Cascade by slug across all tables that reference the app — leaving any
    one behind causes the launcher drawer (reads app_submissions), the
    Permissions page columns (reads app_registry), or per-user toggles
    (reads app_permissions) to keep showing the deleted app. Also clears
    webhook_seen so a future submission with the same repo gets a fresh
    "first push" treatment instead of inheriting stale state.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT slug, status, repo_url FROM app_submissions WHERE id = ?",
        (submission_id,),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": "Submission not found"}

    slug = row["slug"]
    repo_url = row["repo_url"] or ""
    permissions_removed = conn.execute(
        "SELECT COUNT(*) AS n FROM app_permissions WHERE app_slug = ?", (slug,)
    ).fetchone()["n"]

    # Cascade by slug, not id, so any duplicate submission rows for the same
    # app (shouldn't happen given the UNIQUE constraint, but defensive)
    # don't survive the delete and reanimate the app on next page load.
    conn.execute("DELETE FROM app_permissions WHERE app_slug = ?", (slug,))
    conn.execute("DELETE FROM app_registry WHERE slug = ?", (slug,))
    conn.execute("DELETE FROM app_submissions WHERE slug = ?", (slug,))
    if repo_url:
        conn.execute("DELETE FROM webhook_seen WHERE repo_url = ?",
                     (repo_url.lower().rstrip("/").removesuffix(".git"),))
    conn.commit()
    conn.close()
    return {
        "status": "deleted",
        "slug": slug,
        "was_live": row["status"] == "live",
        "permissions_removed": permissions_removed,
    }


def edit_submission(submission_id, slug=None, name=None, description=None, icon=None, port=None, streamlit_port=None, repo_url=None, env_keys=None, is_internal=None):
    """Edit fields on an existing submission. Supports slug changes ONLY for
    apps that haven't been approved yet — see "slug immutability" note below.

    `is_internal` toggles whether the app is treated as a background service.
    Setting it to 1 removes the row from app_registry (so it disappears from
    the Permissions matrix and the launcher); 0 puts it back. The init_db
    reconciliation also enforces this on every boot, so the in-place change
    here is just to make the UI feel responsive between deploys.

    ── Slug immutability after approval ────────────────────────────────────
    Once an app reaches `approved` / `live` / `error` status the slug is
    wired into many places that this DB update does NOT touch:
      - /var/www/aihub-admin/apps/<slug>/         (clone target)
      - /var/www/aihub-admin/secrets/<slug>.env   (per-app secrets file)
      - /etc/nginx/apps/<slug>.conf               (per-app nginx route)
      - aihub-<slug>                              (docker container name)
      - APP_SLUG build arg                        (baked into the image — sets
                                                   Next.js assetPrefix, Flask
                                                   url_prefix, etc.)
      - Slack Event Subscription URLs the app exposed
      - ClickUp / Slack / Linear references to old URL paths

    Renaming any of those requires a coordinated migration that this endpoint
    cannot perform safely. So we reject slug changes once the app is past
    pending state — display Name remains freely editable, which is what most
    "I want to rename this app" requests actually mean. If a true slug
    migration is needed, the recovery path is: delete the app, resubmit
    with the new slug.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM app_submissions WHERE id = ?", (submission_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "Submission not found"}

    old_slug = row["slug"]
    new_slug = slug if (slug and slug != old_slug) else None

    # Validate new slug if changing
    if new_slug:
        # Reject slug edits after approval — see docstring for the
        # full list of artifacts the slug is wired into.
        if row["status"] in ("approved", "live", "error"):
            conn.close()
            return {
                "error": (
                    f"Slug is locked once the app is deployed (current status: {row['status']}). "
                    "Slugs are wired into the container name, nginx route, secrets file path, "
                    "docker image build args, and any Slack/ClickUp references to the app's URL — "
                    "this endpoint can't safely rename all of them in lockstep. "
                    "You can still edit the display Name freely (that's what shows in the banner). "
                    "If you truly need to change the URL slug, delete the app and resubmit with the new slug."
                )
            }
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
    if is_internal is not None:
        # Coerce to 0/1 — accept truthy/falsy from the API.
        updates["is_internal"] = 1 if is_internal else 0

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
    # Reflect is_internal toggle in app_registry immediately so the UI feels
    # responsive — otherwise the change wouldn't show up until init_db's next
    # reconciliation (i.e. next platform boot).
    if "is_internal" in updates:
        target_slug = new_slug or old_slug
        if updates["is_internal"]:
            conn.execute("DELETE FROM app_registry WHERE slug = ?", (target_slug,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO app_registry (slug, name, description) VALUES (?, ?, ?)",
                (target_slug, updates.get("name") or row["name"], updates.get("description") or (row["description"] or "")),
            )

    conn.commit()
    conn.close()

    # Always include the current slug so callers (e.g. /admin/api/apps/edit's
    # log_event) can identify the row without falling back to the numeric
    # submission id — that fallback was leaking ids like "16", "18", "19"
    # into incubator_logs as if they were app slugs.
    result = {"status": "updated", "slug": new_slug or old_slug}
    if new_slug:
        result["old_slug"] = old_slug
        result["new_slug"] = new_slug
        result["note"] = "Slug changed. If this app is deployed, undeploy and redeploy with the new slug."
    return result


# Initialize on import
init_db()
