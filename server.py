"""
Coaching Briefer API Server

Serves the frontend and provides:
- /preload: pre-caches student context when their card is clicked
- /ask: streams an AI answer grounded in DB + Fathom data
- Google OAuth SSO restricted to @egelloc.com
"""

import json
import os
import sys
import logging
import time
import functools
import threading
import queue

from flask import Flask, request, jsonify, send_from_directory, Response, redirect, session, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

sys.path.insert(0, os.path.dirname(__file__))

# MySQL config for staff list (Nest DB)
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", ""),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "egelloc"),
}
from permissions import (
    get_user_permissions, user_has_permission, grant_permission,
    revoke_permission, get_all_permissions, get_all_apps, get_egelloc_staff,
    add_custom_user, remove_custom_user, get_custom_users,
    submit_app, get_all_submissions,
    approve_submission, mark_submission_live, mark_submission_error,
    reject_submission, delete_submission, edit_submission,
    APPS,
)
import mysql.connector.pooling
import requests as http_requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder=".")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
app.config["SESSION_COOKIE_PATH"] = "/"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app)

# When behind a reverse proxy at /briefer/, Nginx sets SCRIPT_NAME via X-Forwarded-Prefix
# so url_for() generates correct absolute URLs (e.g. /briefer/auth/callback)
APP_PREFIX = os.environ.get("APP_PREFIX", "")  # set to "/briefer" in production

# Deploy service URL — separate infra service on port 5001
# In Docker Compose: http://deploy-service:5001 | Native: http://localhost:5001
DEPLOY_SERVICE_URL = os.environ.get("DEPLOY_SERVICE_URL", "http://localhost:5001")

# ── Audit log — background writer ──
# Writes to incubator_logs in a background thread so requests aren't blocked
# by a DB round-trip. Uses a single persistent connection with auto-reconnect.

_audit_queue = queue.Queue(maxsize=1000)


def _audit_worker():
    """Background thread that drains the audit queue and writes to PostgreSQL."""
    import psycopg2
    conn = None
    while True:
        entry = _audit_queue.get()
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(
                    host=os.environ.get("ACQ_DB_HOST", ""),
                    port=int(os.environ.get("ACQ_DB_PORT", "25060")),
                    dbname=os.environ.get("ACQ_DB_NAME", "defaultdb"),
                    user=os.environ.get("INCUBATOR_LOG_DB_USER", ""),
                    password=os.environ.get("INCUBATOR_LOG_DB_PASSWORD", ""),
                    sslmode="require",
                )
                conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO incubator_logs
                   (event_type, user_email, user_name, app_slug, action,
                    detail, metadata, ip_address, user_agent)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (entry["event_type"], entry.get("user_email"), entry.get("user_name"),
                 entry.get("app_slug"), entry["action"], entry.get("detail"),
                 json.dumps(entry.get("metadata") or {}),
                 entry.get("ip_address"), entry.get("user_agent")),
            )
            cur.close()
        except Exception as e:
            log.warning("Audit log write failed: %s", e)
            try:
                conn.close()
            except Exception:
                pass
            conn = None


_audit_thread = threading.Thread(target=_audit_worker, daemon=True)
_audit_thread.start()


def log_event(event_type, action, **kwargs):
    """Queue an audit log entry (non-blocking)."""
    try:
        _audit_queue.put_nowait({"event_type": event_type, "action": action, **kwargs})
    except queue.Full:
        pass  # Drop rather than block the request


# Build known slugs from the app registry so new apps are tracked automatically.
# Falls back to a static set if the DB isn't available yet at startup.
_known_slugs = {"briefer", "knowledge", "admin", "launcher"}
_known_slugs_loaded = False


def _load_known_slugs():
    """Populate known_slugs from app_submissions (live/approved apps)."""
    global _known_slugs, _known_slugs_loaded
    try:
        for s in get_all_submissions():
            if s.get("status") in ("live", "approved"):
                _known_slugs.add(s["slug"])
        _known_slugs_loaded = True
    except Exception:
        pass  # DB not ready — use static fallback


@app.after_request
def audit_log_request(response):
    """Log every authenticated request to the incubator_logs table.

    For requests that go directly to deployed apps (bypassing Flask),
    we capture the Referer header from /auth/me calls — hub-navbar.js
    calls /auth/me on every page load across ALL apps, so parsing the
    Referer gives us coverage for apps routed directly by nginx.
    """
    global _known_slugs_loaded
    if not _known_slugs_loaded:
        _load_known_slugs()

    user = session.get("user")
    if not user:
        return response

    # For /auth/me requests, extract the app from Referer instead of
    # skipping — this is the only signal we get for nginx-direct apps.
    if request.path == "/auth/me":
        referer = request.headers.get("Referer", "")
        ref_parts = [p for p in referer.split("/") if p]
        # Referer looks like "https://aihub.egelloc.com/sales-kpi/..."
        # After split: ['https:', 'aihub.egelloc.com', 'sales-kpi', ...]
        ref_slug = ref_parts[2] if len(ref_parts) > 2 else None
        if ref_slug in _known_slugs:
            log_event(
                "app_access",
                f"page_view {referer}",
                user_email=user.get("email"),
                user_name=user.get("name"),
                app_slug=ref_slug,
                ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
                user_agent=request.headers.get("User-Agent"),
            )
        return response

    skip = ("/static", "/health", "/hub-navbar.js", "/favicon")
    if any(request.path.startswith(p) for p in skip):
        return response

    # Extract app slug from /{slug}/ routes
    parts = [p for p in request.path.strip("/").split("/") if p]
    app_slug = parts[0] if parts and parts[0] in _known_slugs else None

    log_event(
        "app_access",
        f"{request.method} {request.path}",
        user_email=user.get("email"),
        user_name=user.get("name"),
        app_slug=app_slug,
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        user_agent=request.headers.get("User-Agent"),
    )
    return response


# ── Google OAuth SSO ──
ALLOWED_DOMAIN = "egelloc.com"

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def login_required(f):
    """Decorator that enforces @egelloc.com Google SSO on routes."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user")
        if not user:
            session["next_url"] = request.url
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def app_permission_required(app_slug):
    """Decorator that checks if user has permission for a specific app."""
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            user = session.get("user")
            if not user:
                return redirect(url_for("login"))
            if not user_has_permission(user["email"], app_slug):
                return Response(
                    '<!DOCTYPE html><html><head><title>Access Denied</title>'
                    '<style>body{background:#0f1117;color:#e4e6eb;font-family:-apple-system,sans-serif;'
                    'display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;}'
                    'a{color:#4f8ff7;text-decoration:none;padding:10px 24px;border:1px solid #4f8ff7;'
                    'border-radius:8px;margin-top:16px;}a:hover{background:rgba(79,143,247,0.12);}</style></head>'
                    '<body><h2>Access Denied</h2><p style="color:#8b8fa3;margin-top:8px;">'
                    f'You don\'t have permission to access this application.</p>'
                    '<a href="/launcher">Back to AI Hub</a></body></html>',
                    status=403, content_type="text/html",
                )
            return f(*args, **kwargs)
        return decorated
    return decorator


@app.route("/login")
def login():
    # Preserve the original URL the user was trying to reach
    next_url = request.args.get("next") or "/launcher"
    # Never redirect back to logout/logged-out pages
    if "logout" in next_url or "logged-out" in next_url or "login" in next_url:
        next_url = "/launcher"
    session["next_url"] = next_url
    redirect_uri = url_for("auth_callback", _external=True)
    return google.authorize_redirect(redirect_uri, prompt="select_account")


@app.route("/auth/callback")
def auth_callback():
    # Save next_url before authorize_access_token() modifies the session
    next_url = session.get("next_url", "/")

    token = google.authorize_access_token()
    user_info = token.get("userinfo") or google.userinfo()

    email = user_info.get("email", "")
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        # Allow custom users added by admins (consultants, partners)
        from permissions import get_custom_users
        allowed_emails = [u["email"].lower() for u in get_custom_users()]
        if email.lower() not in allowed_emails:
            return Response(
                f"Access denied. Only @{ALLOWED_DOMAIN} accounts or invited users are allowed.",
                status=403,
                content_type="text/plain",
            )

    session["user"] = {
        "email": email,
        "name": user_info.get("name", ""),
        "picture": user_info.get("picture", ""),
    }
    session.pop("next_url", None)

    log_event("auth", "login", user_email=email,
              user_name=user_info.get("name"),
              ip_address=request.headers.get("X-Forwarded-For", request.remote_addr))

    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("logged_out"))


@app.route("/logged-out")
def logged_out():
    return Response(
        """<!DOCTYPE html>
        <html><head><title>Logged Out</title>
        <style>body{background:#0f1117;color:#e4e6eb;font-family:-apple-system,sans-serif;display:flex;
        justify-content:center;align-items:center;height:100vh;flex-direction:column;}
        a{color:#4f8ff7;text-decoration:none;padding:10px 24px;border:1px solid #4f8ff7;border-radius:8px;margin-top:16px;}
        a:hover{background:rgba(79,143,247,0.12);}</style></head>
        <body><h2>You've been logged out</h2><a href="login?next=/launcher">Sign back in</a></body></html>""",
        content_type="text/html",
    )


@app.route("/auth/me")
def auth_me():
    """Return current user info (for the frontend to display)."""
    user = session.get("user")
    if not user:
        return jsonify({"authenticated": False}), 401
    perms = get_user_permissions(user["email"])
    return jsonify({"authenticated": True, **user, "permissions": perms})


@app.route("/hub-navbar.js")
def hub_navbar_js():
    """Serve the universal app-switcher navbar script."""
    return send_from_directory(".", "hub-navbar.js", mimetype="application/javascript")


@app.route("/")
@app.route("/launcher")
@login_required
def launcher():
    return send_from_directory(".", "launcher.html")


@app.route("/launcher/api/apps")
@login_required
def launcher_apps():
    """Return deployed apps for the launcher. Any authenticated user can call this."""
    from permissions import get_db
    conn = get_db()
    rows = conn.execute(
        """SELECT s.slug, s.name, s.description, s.icon, s.port
           FROM app_submissions s
           WHERE s.status = 'live'
           ORDER BY s.name"""
    ).fetchall()
    conn.close()
    return jsonify({"apps": [dict(r) for r in rows]})


@app.route("/knowledge", strict_slashes=False)
@app.route("/knowledge/<path:path>")
@login_required
@app_permission_required("hub")
def knowledge_proxy(path=""):
    """Proxy to the Next.js knowledge base running on port 3004."""
    import requests as proxy_requests
    # Next.js with basePath=/knowledge: /knowledge (no slash) returns 200,
    # /knowledge/ (with slash) returns 308 redirect. So always strip trailing slash.
    target_path = f"knowledge/{path}".rstrip("/")
    target = f"http://host.docker.internal:3004/{target_path}"
    if request.query_string:
        target += f"?{request.query_string.decode()}"
    resp = proxy_requests.request(
        method=request.method,
        url=target,
        headers={k: v for k, v in request.headers if k.lower() != "host"},
        data=request.get_data(),
        allow_redirects=False,
    )
    excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    headers = [(k, v) for k, v in resp.raw.headers.items() if k.lower() not in excluded_headers]
    content = resp.content
    # Inject hub-navbar into HTML pages
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type and b"</body>" in content:
        content = content.replace(b"</body>", b'<script src="/hub-navbar.js" defer></script></body>')
    return Response(content, resp.status_code, headers)


@app.route("/auth/check")
def auth_check():
    """Used by Nginx auth_request to gate all of aihub.egelloc.com."""
    user = session.get("user")
    if user:
        resp = Response("OK", status=200)
        resp.headers["X-Auth-User"] = user.get("email", "")
        return resp
    return Response("Unauthorized", status=401)


# ── Admin: who can access the admin panel ──
# Access is controlled by the 'admin' permission toggle in the Permissions tab.
# No hardcoded list needed — toggle it on for anyone who needs admin access.


def admin_required(f):
    """Decorator that enforces admin access — user must have the 'admin' permission."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user")
        if not user:
            return redirect(url_for("login"))
        email = user["email"].lower()
        if not user_has_permission(email, "admin"):
            return Response(
                '<!DOCTYPE html><html><head><title>Access Denied</title>'
                '<style>body{background:#0f1117;color:#e4e6eb;font-family:-apple-system,sans-serif;'
                'display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;}'
                'a{color:#4f8ff7;text-decoration:none;padding:10px 24px;border:1px solid #4f8ff7;'
                'border-radius:8px;margin-top:16px;}a:hover{background:rgba(79,143,247,0.12);}</style></head>'
                '<body><h2>Access Denied</h2><p style="color:#8b8fa3;margin-top:8px;">'
                'You don\'t have permission to access the Admin Panel.</p>'
                '<a href="/launcher">Back to AI Hub</a></body></html>',
                status=403, content_type="text/html",
            )
        return f(*args, **kwargs)
    return decorated


@app.route("/admin")
@app.route("/admin/infrastructure")
@app.route("/admin/apps")
@admin_required
def admin_page():
    resp = send_from_directory(".", "admin.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/admin/api/users")
@admin_required
def admin_users():
    """Get all egelloC staff with their current permissions."""
    if pool:
        conn = pool.get_connection()
        try:
            staff = get_egelloc_staff(conn)
        finally:
            conn.close()
    else:
        staff = []

    all_perms = get_all_permissions()
    apps = get_all_apps()

    seen_emails = set()
    users = []
    for s in staff:
        email = s["email"].lower()
        seen_emails.add(email)
        user_perms = [p["app_slug"] for p in all_perms.get(email, [])]
        users.append({
            "email": s["email"],
            "first_name": s["first_name"],
            "last_name": s["last_name"],
            "roles": s["roles"],
            "avatar": s["avatar"],
            "permissions": user_perms,
            "source": "nest",
        })

    # Merge custom users not already in MySQL
    for cu in get_custom_users():
        email = cu["email"].lower()
        if email not in seen_emails:
            user_perms = [p["app_slug"] for p in all_perms.get(email, [])]
            users.append({
                "email": cu["email"],
                "first_name": cu["first_name"],
                "last_name": cu["last_name"],
                "roles": cu.get("role", "custom"),
                "avatar": None,
                "permissions": user_perms,
                "source": "custom",
            })

    return jsonify({"users": users, "apps": apps})


@app.route("/admin/api/grant", methods=["POST"])
@admin_required
def admin_grant():
    body = request.get_json()
    email = body.get("email", "").strip()
    app_slug = body.get("app_slug", "").strip()
    if not email or not app_slug:
        return jsonify({"error": "email and app_slug required"}), 400

    admin_email = session["user"]["email"]
    grant_permission(email, app_slug, admin_email)
    return jsonify({"status": "granted"})


PROTECTED_ADMINS = ["victor@egelloc.com", "tony@egelloc.com"]


@app.route("/admin/api/revoke", methods=["POST"])
@admin_required
def admin_revoke():
    body = request.get_json()
    email = body.get("email", "").strip().lower()
    app_slug = body.get("app_slug", "").strip()
    if not email or not app_slug:
        return jsonify({"error": "email and app_slug required"}), 400

    if app_slug == "admin" and email in [e.lower() for e in PROTECTED_ADMINS]:
        return jsonify({"error": f"Cannot remove admin access from {email}"}), 403

    revoke_permission(email, app_slug)
    return jsonify({"status": "revoked"})


@app.route("/admin/api/add-user", methods=["POST"])
@admin_required
def admin_add_user():
    body = request.get_json()
    email = body.get("email", "").strip().lower()
    first_name = body.get("first_name", "").strip()
    last_name = body.get("last_name", "").strip()
    role = body.get("role", "").strip()

    if not email or not first_name or not last_name:
        return jsonify({"error": "email, first_name, and last_name are required"}), 400

    admin_email = session["user"]["email"]
    add_custom_user(email, first_name, last_name, role, admin_email)
    return jsonify({"status": "added"})


@app.route("/admin/api/remove-user", methods=["POST"])
@admin_required
def admin_remove_user():
    body = request.get_json()
    email = body.get("email", "").strip()
    if not email:
        return jsonify({"error": "email required"}), 400

    remove_custom_user(email)
    return jsonify({"status": "removed"})


@app.route("/admin/api/bulk", methods=["POST"])
@admin_required
def admin_bulk():
    """Bulk grant/revoke permissions."""
    body = request.get_json()
    actions = body.get("actions", [])
    admin_email = session["user"]["email"]

    for action in actions:
        email = action.get("email", "").strip()
        app_slug = action.get("app_slug", "").strip()
        op = action.get("action", "")
        if not email or not app_slug:
            continue
        if op == "grant":
            grant_permission(email, app_slug, admin_email)
        elif op == "revoke":
            if app_slug == "admin" and email.lower() in [e.lower() for e in PROTECTED_ADMINS]:
                continue
            revoke_permission(email, app_slug)

    return jsonify({"status": "ok", "processed": len(actions)})


# ── App Registry: submission + approval ──
# Deploy/undeploy endpoints are built by the infra session on this same server.
# The UI calls POST /admin/api/deploy and POST /admin/api/undeploy directly.
# This section handles the submission workflow: submit → approve/reject.
# On approve, the app is registered. Deploy is a separate admin action.

import re


def _auto_assign_port(is_streamlit=False):
    """Find the next available port starting from 4000 for regular apps.

    Checks both Docker containers and host-level listeners (via the deploy
    service validate endpoint). Also checks existing submissions so two
    pending apps don't get the same port.
    """
    # Collect all ports already in use or reserved
    used = set()

    # From existing submissions (pending, approved, live)
    for s in get_all_submissions():
        if s.get("port"):
            used.add(int(s["port"]))
        if s.get("streamlit_port"):
            used.add(int(s["streamlit_port"]))

    # Well-known platform ports
    used.update({80, 443, 3000, 3004, 5051, 5052, 5432, 6001, 8000, 8888})

    start = 4000
    for candidate in range(start, 9000):
        if candidate not in used:
            if is_streamlit:
                # Need two consecutive ports: app + websocket
                if candidate + 1 not in used:
                    return candidate, candidate + 1
            else:
                return candidate, None
    return None, None


@app.route("/admin/api/apps/submit", methods=["POST"])
@login_required
def api_submit_app():
    """Submit a new app for admin review. Any authenticated user can submit."""
    body = request.get_json()
    slug = (body.get("slug") or "").strip().lower()
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    icon = (body.get("icon") or "").strip()
    port = body.get("port")
    streamlit_port = body.get("streamlit_port")
    is_streamlit = body.get("is_streamlit", False)
    repo_url = (body.get("repo_url") or "").strip()
    env_keys = (body.get("env_keys") or "").strip()

    if not slug or not name:
        return jsonify({"error": "slug and name are required"}), 400
    if not repo_url:
        return jsonify({"error": "Git repository URL is required for deployment"}), 400

    # Auto-assign port if not provided
    if not port:
        port, auto_streamlit = _auto_assign_port(is_streamlit=is_streamlit)
        if port is None:
            return jsonify({"error": "Could not find an available port"}), 500
        if is_streamlit and not streamlit_port:
            streamlit_port = auto_streamlit

    # Auto-detect subdirectory from GitHub tree URLs
    # e.g. https://github.com/org/repo/tree/main/my-app → repo=org/repo.git, subdir=my-app
    tree_match = re.match(r"https://github\.com/([^/]+/[^/]+)/tree/[^/]+/(.+?)/?$", repo_url)
    if tree_match:
        repo_url = f"https://github.com/{tree_match.group(1)}.git"
        repo_subdir = tree_match.group(2)
    else:
        repo_subdir = None
    # Ensure repo_url ends with .git
    if repo_url and "github.com" in repo_url and not repo_url.endswith(".git"):
        repo_url = repo_url.rstrip("/") + ".git"
    if not re.match(r"^[a-z][a-z0-9-]{1,30}$", slug):
        return jsonify({"error": "Slug must be lowercase letters, numbers, hyphens. 2-31 chars, start with letter."}), 400
    try:
        port = int(port)
        if not (1024 <= port <= 65535):
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "Port must be a number between 1024 and 65535"}), 400

    # Optional streamlit_port
    if streamlit_port not in (None, "", 0, "0"):
        try:
            streamlit_port = int(streamlit_port)
            if not (1024 <= streamlit_port <= 65535):
                raise ValueError
            if streamlit_port == port:
                return jsonify({"error": "streamlit_port must differ from port"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "streamlit_port must be a number between 1024 and 65535"}), 400
    else:
        streamlit_port = None

    # Run pre-submission validation via deploy service
    validation = None
    try:
        val_resp = http_requests.post(
            f"{DEPLOY_SERVICE_URL}/validate",
            json={"app_name": slug, "port": port, "repo_url": repo_url},
            timeout=15,
        )
        if val_resp.ok:
            validation = val_resp.json()
            # Block submission if validation fails
            if validation.get("result") == "fail":
                return jsonify({
                    "error": "Submission failed validation",
                    "validation": validation,
                }), 422
    except Exception:
        # Deploy service unavailable — allow submission without validation
        pass

    submitted_by = session["user"]["email"]
    result = submit_app(slug, name, description, icon, port, repo_url, repo_subdir, env_keys, submitted_by, streamlit_port=streamlit_port)
    if "error" in result:
        return jsonify(result), 409
    if validation:
        result["validation"] = validation
    return jsonify(result)


@app.route("/admin/api/apps/submissions")
@admin_required
def api_app_submissions():
    """Get all app submissions (admin only)."""
    return jsonify({"submissions": get_all_submissions()})


@app.route("/admin/api/apps/my-submissions")
@login_required
def api_my_submissions():
    """Get submissions by the current user. Any authenticated user can check their own."""
    from permissions import get_db
    email = session["user"]["email"].lower()
    conn = get_db()
    rows = conn.execute(
        "SELECT slug, name, status, submitted_at, reviewed_at FROM app_submissions WHERE submitted_by = ? COLLATE NOCASE ORDER BY submitted_at DESC",
        (email,),
    ).fetchall()
    conn.close()
    return jsonify({"submissions": [dict(r) for r in rows]})


@app.route("/admin/api/apps/approve", methods=["POST"])
@admin_required
def api_approve_app():
    """Approve a pending app submission and trigger auto-deploy."""
    body = request.get_json()
    submission_id = body.get("id")
    if not submission_id:
        return jsonify({"error": "id is required"}), 400

    reviewed_by = session["user"]["email"]
    result = approve_submission(submission_id, reviewed_by)
    if "error" in result:
        return jsonify(result), 404

    # Auto-deploy after approval
    try:
        from permissions import get_db
        conn = get_db()
        row = conn.execute("SELECT slug, port, streamlit_port, repo_url, repo_subdir FROM app_submissions WHERE id = ?", (submission_id,)).fetchone()
        conn.close()
        if row and row["repo_url"]:
            deploy_payload = {
                "app_name": row["slug"],
                "port": row["port"],
                "streamlit_port": row["streamlit_port"],
                "repo_url": row["repo_url"],
                "repo_subdir": row["repo_subdir"] or None,
            }
            http_requests.post(f"{DEPLOY_SERVICE_URL}/deploy", json=deploy_payload, timeout=5)
            result["auto_deploy"] = "triggered"
    except Exception:
        result["auto_deploy"] = "skipped"

    return jsonify(result)


@app.route("/admin/api/apps/edit", methods=["POST"])
@admin_required
def api_edit_app():
    """Edit an app submission's details."""
    body = request.get_json()
    submission_id = body.get("id")
    if not submission_id:
        return jsonify({"error": "id is required"}), 400

    result = edit_submission(
        submission_id,
        slug=body.get("slug"),
        name=body.get("name"),
        description=body.get("description"),
        icon=body.get("icon"),
        port=body.get("port"),
        streamlit_port=body.get("streamlit_port"),
        repo_url=body.get("repo_url"),
        env_keys=body.get("env_keys"),
    )
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route("/admin/api/apps/reject", methods=["POST"])
@admin_required
def api_reject_app():
    """Reject a pending app submission."""
    body = request.get_json()
    submission_id = body.get("id")
    if not submission_id:
        return jsonify({"error": "id is required"}), 400

    reason = (body.get("reason") or "").strip()
    reviewed_by = session["user"]["email"]
    result = reject_submission(submission_id, reviewed_by, reason=reason)
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result)


@app.route("/admin/api/apps/delete", methods=["POST"])
@admin_required
def api_delete_app():
    """Delete an app submission. If the app is live, undeploys it first."""
    body = request.get_json()
    submission_id = body.get("id")
    if not submission_id:
        return jsonify({"error": "id is required"}), 400

    result = delete_submission(submission_id)
    if "error" in result:
        return jsonify(result), 404

    # If the app was live, trigger undeploy to clean up containers/routes/DB
    if result.get("was_live"):
        try:
            http_requests.post(
                f"{DEPLOY_SERVICE_URL}/undeploy",
                json={"app_name": result["slug"]},
                timeout=30,
            )
        except Exception:
            pass  # Best effort — infra cleanup is non-blocking

    return jsonify(result)


# Track which repos have sent us a webhook delivery.
# Updated by the /webhook/github handler on every push event.
# Shows "unknown" until the first push after container restart.
_webhook_seen_repos = set()


@app.route("/admin/api/apps/webhook-status")
@admin_required
def api_webhook_status():
    """Check which apps have GitHub webhooks configured.
    Uses local tracking (repos that have sent us a webhook) instead of
    GitHub API (which requires admin:repo_hook scope we don't have)."""
    results = {}
    for s in get_all_submissions():
        if s.get("status") not in ("live", "approved"):
            continue
        repo_url = (s.get("repo_url") or "").lower().rstrip("/").replace(".git", "")
        if not repo_url:
            results[s["slug"]] = None
            continue
        results[s["slug"]] = repo_url in _webhook_seen_repos
    return jsonify(results)


@app.route("/admin/api/apps/status", methods=["POST"])
@admin_required
def api_update_app_status():
    """Update a submission's status after deploy/undeploy.
    Called by the UI after the deploy endpoint responds."""
    body = request.get_json()
    slug = (body.get("slug") or "").strip()
    status = (body.get("status") or "").strip()
    if not slug or status not in ("live", "error", "approved"):
        return jsonify({"error": "slug and status (live|error|approved) required"}), 400

    from permissions import get_db
    conn = get_db()
    row = conn.execute("SELECT id FROM app_submissions WHERE slug = ?", (slug,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Submission not found"}), 404

    if status == "live":
        mark_submission_live(row["id"])
    elif status == "error":
        mark_submission_error(row["id"])
    else:
        conn.execute("UPDATE app_submissions SET status = 'approved' WHERE id = ?", (row["id"],))
        conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/admin/api/deploy", methods=["POST"])
@admin_required
def api_deploy_app():
    """Deploy an approved app via the deploy service."""
    body = request.get_json()
    app_name = (body.get("app_name") or body.get("slug") or "").strip()
    port = body.get("port")
    streamlit_port = body.get("streamlit_port")
    repo_url = body.get("repo_url")
    local_path = body.get("local_path")
    repo_subdir = body.get("repo_subdir")
    dry_run = body.get("dry_run", False)

    if not app_name or not port:
        return jsonify({"error": "app_name and port are required"}), 400

    # If streamlit_port isn't provided by caller, look it up from the submission row
    if streamlit_port in (None, "", 0, "0"):
        try:
            from permissions import get_db
            conn = get_db()
            row = conn.execute("SELECT streamlit_port FROM app_submissions WHERE slug = ?", (app_name,)).fetchone()
            conn.close()
            if row and row["streamlit_port"]:
                streamlit_port = row["streamlit_port"]
        except Exception:
            pass

    try:
        resp = http_requests.post(
            f"{DEPLOY_SERVICE_URL}/deploy",
            json={
                "app_name": app_name,
                "port": port,
                "streamlit_port": streamlit_port,
                "repo_url": repo_url,
                "local_path": local_path,
                "repo_subdir": repo_subdir,
                "dry_run": dry_run,
            },
            timeout=120,
        )
        return jsonify(resp.json()), resp.status_code
    except http_requests.ConnectionError:
        return jsonify({"error": "Deploy service unavailable"}), 503
    except http_requests.Timeout:
        return jsonify({"error": "Deploy timed out"}), 504


@app.route("/admin/api/undeploy", methods=["POST"])
@admin_required
def api_undeploy_app():
    """Remove a deployed app via the deploy service."""
    body = request.get_json()
    app_name = (body.get("app_name") or body.get("slug") or "").strip()

    if not app_name:
        return jsonify({"error": "app_name is required"}), 400

    try:
        resp = http_requests.post(
            f"{DEPLOY_SERVICE_URL}/undeploy",
            json={"app_name": app_name},
            timeout=60,
        )
        return jsonify(resp.json()), resp.status_code
    except http_requests.ConnectionError:
        return jsonify({"error": "Deploy service unavailable"}), 503
    except http_requests.Timeout:
        return jsonify({"error": "Undeploy timed out"}), 504


@app.route("/admin/api/test-deploy", methods=["POST"])
@admin_required
def api_test_deploy():
    """Pre-deploy validation — checks without building or deploying."""
    body = request.get_json()
    app_name = (body.get("app_name") or body.get("slug") or "").strip()
    port = body.get("port")
    repo_url = body.get("repo_url")
    local_path = body.get("local_path")

    if not app_name or not port:
        return jsonify({"error": "app_name and port are required"}), 400

    try:
        resp = http_requests.post(
            f"{DEPLOY_SERVICE_URL}/test",
            json={
                "app_name": app_name,
                "port": port,
                "repo_url": repo_url,
                "local_path": local_path,
            },
            timeout=30,
        )
        return jsonify(resp.json()), resp.status_code
    except http_requests.ConnectionError:
        return jsonify({"error": "Deploy service unavailable"}), 503
    except http_requests.Timeout:
        return jsonify({"error": "Test timed out"}), 504


# ── GitHub Webhook: auto-deploy on push ──

import hashlib
import hmac

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


def verify_webhook_signature(payload, signature):
    """Verify GitHub webhook HMAC signature."""
    if not WEBHOOK_SECRET:
        return True  # No secret configured — allow (for dev)
    if not signature:
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.route("/webhook/github", methods=["POST"])
def github_webhook():
    """Auto-deploy apps when code is pushed to GitHub."""
    # Verify signature
    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(payload, signature):
        return jsonify({"error": "Invalid signature"}), 403

    event = request.headers.get("X-GitHub-Event", "")
    if event != "push":
        return jsonify({"status": "ignored", "event": event}), 200

    body = request.get_json(silent=True) or {}
    repo_url = body.get("repository", {}).get("clone_url", "")
    repo_name = body.get("repository", {}).get("full_name", "")
    branch = body.get("ref", "").replace("refs/heads/", "")

    # Only deploy from main/master branch
    if branch not in ("main", "master"):
        return jsonify({"status": "ignored", "branch": branch}), 200

    log.info("Webhook push: %s (branch: %s)", repo_name, branch)

    # Track that this repo has a working webhook
    if repo_url:
        _webhook_seen_repos.add(repo_url.lower().rstrip("/").replace(".git", ""))

    results = []

    # Check if any deployed apps use this repo
    from permissions import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT slug, port, streamlit_port, repo_url, repo_subdir FROM app_submissions WHERE status = 'live' AND repo_url != ''"
    ).fetchall()
    conn.close()

    # Slugs whose deploy is handled by a repo-specific special case below — skip
    # in the generic loop to avoid double-deploys that fight the platform itself.
    # admin = the admin panel (full docker compose rebuild handler below)
    # hub   = the knowledge base (PM2/Next.js, served on host:3004 via host.docker.internal)
    SPECIAL_CASE_SLUGS = {"admin", "hub"}

    for row in rows:
        if row["slug"] in SPECIAL_CASE_SLUGS:
            continue
        # Match by repo URL (normalize trailing .git)
        app_repo = row["repo_url"].rstrip("/").removesuffix(".git")
        push_repo = repo_url.rstrip("/").removesuffix(".git")
        if app_repo.lower() == push_repo.lower():
            try:
                resp = http_requests.post(
                    f"{DEPLOY_SERVICE_URL}/deploy",
                    json={
                        "app_name": row["slug"],
                        "port": row["port"],
                        "streamlit_port": row["streamlit_port"],
                        "repo_url": row["repo_url"],
                        "repo_subdir": row["repo_subdir"] or None,
                    },
                    timeout=5,
                )
                results.append({"app": row["slug"], "status": "triggered"})
                log.info("Auto-deploy triggered for %s", row["slug"])
            except Exception as e:
                results.append({"app": row["slug"], "status": "error", "detail": str(e)})

    # Special case: knowledge base — route through deploy-service /kb-deploy
    # which uses docker run --pid=host --privileged + nsenter to reach the
    # host's node/npm/pm2 and run the build in the background.
    if "egelloc-ai-hub" in repo_name.lower():
        try:
            resp = http_requests.post(f"{DEPLOY_SERVICE_URL}/kb-deploy", timeout=5)
            if resp.ok:
                results.append({"app": "knowledge-base", "status": "triggered"})
                log.info("KB deploy triggered via deploy-service")
            else:
                results.append({"app": "knowledge-base", "status": "error",
                                "detail": resp.text[:200]})
        except Exception as e:
            results.append({"app": "knowledge-base", "status": "error", "detail": str(e)})

    # Special case: admin panel itself — route through deploy-service which has
    # docker.sock mounted + /platform-repo mounted + docker compose plugin.
    if "aihub-admin" in repo_name.lower():
        try:
            resp = http_requests.post(f"{DEPLOY_SERVICE_URL}/self-deploy", timeout=5)
            if resp.ok:
                results.append({"app": "admin-panel", "status": "triggered"})
                log.info("Self-deploy triggered for admin-panel via deploy-service")
            else:
                results.append({"app": "admin-panel", "status": "error",
                                "detail": f"deploy-service returned {resp.status_code}: {resp.text[:200]}"})
        except Exception as e:
            results.append({"app": "admin-panel", "status": "error", "detail": str(e)})

    if not results:
        return jsonify({"status": "no_match", "repo": repo_name}), 200

    return jsonify({"status": "ok", "deploys": results}), 200


# ── Infrastructure access management ──

SSH_AUTHORIZED_KEYS_FILE = os.path.expanduser("~/.ssh/authorized_keys")
SSH_ALIASES_FILE = os.path.join(os.path.dirname(__file__), "ssh_aliases.json")
IP_LABELS_FILE = os.path.join(os.path.dirname(__file__), "ip_labels.json")


def load_ip_labels():
    try:
        with open(IP_LABELS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_ip_labels(labels):
    with open(IP_LABELS_FILE, "w") as f:
        json.dump(labels, f, indent=2)


@app.route("/admin/api/ip-labels")
@admin_required
def api_ip_labels():
    return jsonify(load_ip_labels())


@app.route("/admin/api/ip-labels", methods=["POST"])
@admin_required
def api_set_ip_label():
    body = request.get_json()
    key = body.get("key", "")
    label = body.get("label", "")
    date = body.get("date", "")
    labels = load_ip_labels()
    labels[key] = {"label": label, "date": date}
    save_ip_labels(labels)
    return jsonify({"status": "ok"})


@app.route("/admin/api/ip-labels/remove", methods=["POST"])
@admin_required
def api_remove_ip_label():
    body = request.get_json()
    key = body.get("key", "")
    labels = load_ip_labels()
    labels.pop(key, None)
    save_ip_labels(labels)
    return jsonify({"status": "ok"})


def load_ssh_aliases():
    try:
        with open(SSH_ALIASES_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_ssh_aliases(aliases):
    with open(SSH_ALIASES_FILE, "w") as f:
        json.dump(aliases, f, indent=2)


@app.route("/admin/api/ssh-keys")
@admin_required
def admin_ssh_keys():
    """List all SSH keys with access to this server."""
    aliases = load_ssh_aliases()
    keys = []
    try:
        with open(SSH_AUTHORIZED_KEYS_FILE, "r") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                key_type = parts[0] if len(parts) >= 1 else "unknown"
                key_hash = parts[1][:20] + "..." if len(parts) >= 2 else "?"
                comment = parts[2] if len(parts) >= 3 else "no-comment"
                keys.append({
                    "id": i,
                    "type": key_type,
                    "hash_preview": key_hash,
                    "comment": comment,
                    "alias": aliases.get(str(i), ""),
                    "full_line": line,
                })
    except FileNotFoundError:
        pass
    return jsonify({"keys": keys})


@app.route("/admin/api/ssh-keys/alias", methods=["POST"])
@admin_required
def admin_ssh_alias():
    """Set or update an alias for an SSH key."""
    body = request.get_json()
    key_id = body.get("id")
    alias = body.get("alias", "").strip()

    if key_id is None:
        return jsonify({"error": "Key ID is required"}), 400

    aliases = load_ssh_aliases()
    if alias:
        aliases[str(key_id)] = alias
    else:
        aliases.pop(str(key_id), None)
    save_ssh_aliases(aliases)

    return jsonify({"status": "ok"})


@app.route("/admin/api/ssh-keys/add", methods=["POST"])
@admin_required
def admin_add_ssh_key():
    """Add an SSH public key."""
    body = request.get_json()
    key = body.get("key", "").strip()
    comment = body.get("comment", "").strip()

    if not key:
        return jsonify({"error": "SSH public key is required"}), 400
    if not key.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-sha2")):
        return jsonify({"error": "Invalid SSH public key format"}), 400

    # Append comment if not already in the key
    parts = key.split()
    if len(parts) == 2 and comment:
        key = f"{key} {comment}"

    with open(SSH_AUTHORIZED_KEYS_FILE, "a") as f:
        f.write(f"\n{key}\n")

    log.info("SSH key added by %s: %s", session["user"]["email"], comment or parts[-1])
    return jsonify({"status": "ok"})


@app.route("/admin/api/ssh-keys/remove", methods=["POST"])
@admin_required
def admin_remove_ssh_key():
    """Remove an SSH key by its line index."""
    body = request.get_json()
    key_id = body.get("id")
    confirm_comment = body.get("comment", "")

    if key_id is None:
        return jsonify({"error": "Key ID is required"}), 400

    lines = []
    removed = None
    try:
        with open(SSH_AUTHORIZED_KEYS_FILE, "r") as f:
            lines = f.readlines()

        key_idx = 0
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if key_idx == key_id:
                removed = stripped
                # Skip this line (remove it)
            else:
                new_lines.append(line)
            key_idx += 1

        if removed is None:
            return jsonify({"error": "Key not found"}), 404

        with open(SSH_AUTHORIZED_KEYS_FILE, "w") as f:
            f.writelines(new_lines)

        log.info("SSH key removed by %s: %s", session["user"]["email"], confirm_comment or removed[:40])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok", "removed": confirm_comment or removed[:40]})


# ── Read-only DB user management ──
# Tracks users created through this panel in a local JSON file.
# Only these users are shown/manageable — system users are never touched.

# Database configs for user management
MANAGED_DATABASES = {
    "nest": {
        "label": "Nest (Student Data)",
        "description": "Student profiles, bookings, meeting notes, check-ins, coaching data",
        "host": DB_CONFIG["host"],
        "port": DB_CONFIG["port"],
        "database": DB_CONFIG["database"],
        "admin_user": os.environ.get("DB_ADMIN_USER", "doadmin"),
        "admin_password": os.environ.get("DB_ADMIN_PASSWORD", ""),
    },
    "acquisition": {
        "label": "Acquisition (Sales & Marketing)",
        "description": "Leads, pipeline, revenue ops, marketing attribution",
        "host": os.environ.get("ACQ_DB_HOST", "egelloc-ai-db-do-user-33607902-0.g.db.ondigitalocean.com"),
        "port": int(os.environ.get("ACQ_DB_PORT", "25060")),
        "database": os.environ.get("ACQ_DB_NAME", "defaultdb"),
        "admin_user": os.environ.get("ACQ_DB_ADMIN_USER", "doadmin"),
        "admin_password": os.environ.get("ACQ_DB_ADMIN_PASSWORD", ""),
        "engine": "pg",
    },
}

# DigitalOcean API for database firewall management
DO_CONFIGS = {
    "nest": {
        "token": os.environ.get("DO_API_TOKEN_NEST", ""),
        "cluster_id": os.environ.get("DO_DB_CLUSTER_NEST", ""),
    },
    "acquisition": {
        "token": os.environ.get("DO_API_TOKEN_ACQ", ""),
        "cluster_id": os.environ.get("DO_DB_CLUSTER_ACQ", ""),
    },
}
PROTECTED_IPS = ["165.232.155.132"]  # Droplet — never removable

READONLY_USERS_FILE = os.path.join(os.path.dirname(__file__), "readonly_db_users.json")


def load_readonly_users():
    try:
        with open(READONLY_USERS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_readonly_users(users):
    with open(READONLY_USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


@app.route("/admin/api/network-access")
@admin_required
def admin_network_access():
    """List trusted sources (firewall rules) for a database."""
    db_slug = request.args.get("db", "nest")
    cfg = DO_CONFIGS.get(db_slug)
    if not cfg or not cfg["token"] or not cfg["cluster_id"]:
        return jsonify({"rules": [], "error": "Not configured for this database"})

    try:
        resp = http_requests.get(
            f"https://api.digitalocean.com/v2/databases/{cfg['cluster_id']}/firewall",
            headers={"Authorization": f"Bearer {cfg['token']}"},
            timeout=10,
        )
        resp.raise_for_status()
        rules = resp.json().get("rules", [])
        return jsonify({"rules": [
            {
                "uuid": r["uuid"],
                "type": r["type"],
                "value": r["value"],
                "protected": r["value"] in PROTECTED_IPS,
            } for r in rules
        ]})
    except Exception as e:
        return jsonify({"rules": [], "error": str(e)})


@app.route("/admin/api/network-access/add", methods=["POST"])
@admin_required
def admin_add_trusted_source():
    """Add an IP to the database trusted sources."""
    body = request.get_json()
    db_slug = body.get("db", "nest")
    ip = body.get("ip", "").strip()

    if not ip:
        return jsonify({"error": "IP address is required"}), 400

    cfg = DO_CONFIGS.get(db_slug)
    if not cfg or not cfg["token"] or not cfg["cluster_id"]:
        return jsonify({"error": "Not configured for this database"}), 400

    try:
        # Get current rules
        resp = http_requests.get(
            f"https://api.digitalocean.com/v2/databases/{cfg['cluster_id']}/firewall",
            headers={"Authorization": f"Bearer {cfg['token']}"},
            timeout=10,
        )
        resp.raise_for_status()
        current_rules = resp.json().get("rules", [])

        # Check if already exists
        if any(r["value"] == ip for r in current_rules):
            return jsonify({"error": f"{ip} is already in trusted sources"})

        # Build new rules list (existing + new)
        new_rules = [{"type": r["type"], "value": r["value"]} for r in current_rules]
        new_rules.append({"type": "ip_addr", "value": ip})

        # Update
        resp = http_requests.put(
            f"https://api.digitalocean.com/v2/databases/{cfg['cluster_id']}/firewall",
            headers={
                "Authorization": f"Bearer {cfg['token']}",
                "Content-Type": "application/json",
            },
            json={"rules": new_rules},
            timeout=10,
        )
        resp.raise_for_status()

        log.info("Trusted source added by %s: %s on %s", session["user"]["email"], ip, db_slug)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/network-access/remove", methods=["POST"])
@admin_required
def admin_remove_trusted_source():
    """Remove an IP from the database trusted sources."""
    body = request.get_json()
    db_slug = body.get("db", "nest")
    ip = body.get("ip", "").strip()

    if not ip:
        return jsonify({"error": "IP address is required"}), 400
    if ip in PROTECTED_IPS:
        return jsonify({"error": f"Cannot remove {ip} — this is the application server"}), 400

    cfg = DO_CONFIGS.get(db_slug)
    if not cfg or not cfg["token"] or not cfg["cluster_id"]:
        return jsonify({"error": "Not configured for this database"}), 400

    try:
        # Get current rules
        resp = http_requests.get(
            f"https://api.digitalocean.com/v2/databases/{cfg['cluster_id']}/firewall",
            headers={"Authorization": f"Bearer {cfg['token']}"},
            timeout=10,
        )
        resp.raise_for_status()
        current_rules = resp.json().get("rules", [])

        # Remove the specified IP
        new_rules = [{"type": r["type"], "value": r["value"]} for r in current_rules if r["value"] != ip]

        if len(new_rules) == len(current_rules):
            return jsonify({"error": f"{ip} not found in trusted sources"})

        # Update
        resp = http_requests.put(
            f"https://api.digitalocean.com/v2/databases/{cfg['cluster_id']}/firewall",
            headers={
                "Authorization": f"Bearer {cfg['token']}",
                "Content-Type": "application/json",
            },
            json={"rules": new_rules},
            timeout=10,
        )
        resp.raise_for_status()

        log.info("Trusted source removed by %s: %s from %s", session["user"]["email"], ip, db_slug)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/my-ip")
@admin_required
def admin_my_ip():
    """Return the caller's public IP address."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if "," in ip:
        ip = ip.split(",")[0].strip()
    return jsonify({"ip": ip})


@app.route("/admin/api/databases")
@admin_required
def admin_databases():
    """List all managed databases and their info."""
    dbs = []
    for slug, cfg in MANAGED_DATABASES.items():
        dbs.append({
            "slug": slug,
            "label": cfg["label"],
            "description": cfg["description"],
            "host": cfg["host"],
            "port": cfg["port"],
            "database": cfg["database"],
        })
    return jsonify({"databases": dbs})


@app.route("/admin/api/db-users")
@admin_required
def admin_db_users():
    """List read-only database users created through this panel."""
    db_slug = request.args.get("db", "nest")
    all_users = load_readonly_users()
    users = [u for u in all_users if u.get("db", "nest") == db_slug]
    cfg = MANAGED_DATABASES.get(db_slug, {})
    return jsonify({
        "users": users,
        "host": cfg.get("host", ""),
        "port": cfg.get("port", 25060),
        "database": cfg.get("database", ""),
    })


@app.route("/admin/api/db-users/create", methods=["POST"])
@admin_required
def admin_create_db_user():
    """Create a read-only (SELECT only) database user."""
    body = request.get_json()
    username = body.get("username", "").strip().lower()
    db_slug = body.get("db", "nest")

    cfg = MANAGED_DATABASES.get(db_slug)
    if not cfg:
        return jsonify({"error": f"Unknown database: {db_slug}"}), 400

    db_name = cfg["database"]

    if not username:
        return jsonify({"error": "Username is required"}), 400
    if not username.replace("_", "").replace("-", "").isalnum():
        return jsonify({"error": "Username must be alphanumeric (underscores and hyphens allowed)"}), 400
    if len(username) > 32:
        return jsonify({"error": "Username must be 32 characters or less"}), 400

    existing = load_readonly_users()
    if any(u["username"] == username and u.get("db", "nest") == db_slug for u in existing):
        return jsonify({"error": f"User '{username}' already exists on {cfg['label']}"}), 400

    try:
        import secrets
        password = secrets.token_urlsafe(16)
        engine = cfg.get("engine", "mysql")

        if engine == "pg":
            import psycopg2
            conn = psycopg2.connect(
                host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
                user=cfg["admin_user"], password=cfg["admin_password"],
                sslmode="require",
            )
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(f"CREATE USER \"{username}\" WITH PASSWORD %s", (password,))
            cursor.execute(f"GRANT CONNECT ON DATABASE \"{db_name}\" TO \"{username}\"")
            cursor.execute(f"GRANT USAGE ON SCHEMA public TO \"{username}\"")
            cursor.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO \"{username}\"")
            cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO \"{username}\"")
            conn.close()
        else:
            conn = mysql.connector.pooling.MySQLConnection(
                host=cfg["host"], port=cfg["port"], database=cfg["database"],
                user=cfg["admin_user"], password=cfg["admin_password"],
            )
            cursor = conn.cursor()
            cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (username, password))
            cursor.execute(f"GRANT SELECT ON {db_name}.* TO %s@'%%'", (username,))
            cursor.execute("FLUSH PRIVILEGES")
            conn.commit()
            conn.close()

        existing.append({
            "username": username,
            "db": db_slug,
            "created_by": session["user"]["email"],
            "created_at": time.strftime("%b %d, %Y"),
        })
        save_readonly_users(existing)

        log.info("Read-only DB user created by %s: %s on %s", session["user"]["email"], username, db_slug)

        return jsonify({
            "status": "ok",
            "username": username,
            "password": password,
            "host": cfg["host"],
            "port": cfg["port"],
            "database": db_name,
            "access": "read-only (SELECT only)",
            "connection_string": f"mysql -h {cfg['host']} -P {cfg['port']} -u {username} -p'{password}' {db_name}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/db-users/drop", methods=["POST"])
@admin_required
def admin_drop_db_user():
    """Drop a read-only database user (only users created through this panel)."""
    body = request.get_json()
    username = body.get("username", "").strip()
    db_slug = body.get("db", "nest")

    if not username:
        return jsonify({"error": "Username is required"}), 400

    cfg = MANAGED_DATABASES.get(db_slug)
    if not cfg:
        return jsonify({"error": f"Unknown database: {db_slug}"}), 400

    existing = load_readonly_users()
    if not any(u["username"] == username and u.get("db", "nest") == db_slug for u in existing):
        return jsonify({"error": "Can only remove users created through this panel"}), 400

    try:
        engine = cfg.get("engine", "mysql")

        if engine == "pg":
            import psycopg2
            conn = psycopg2.connect(
                host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
                user=cfg["admin_user"], password=cfg["admin_password"],
                sslmode="require",
            )
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM \"{username}\"")
            cursor.execute(f"REVOKE USAGE ON SCHEMA public FROM \"{username}\"")
            cursor.execute(f"REVOKE CONNECT ON DATABASE \"{cfg['database']}\" FROM \"{username}\"")
            cursor.execute(f"DROP USER IF EXISTS \"{username}\"")
            conn.close()
        else:
            conn = mysql.connector.pooling.MySQLConnection(
                host=cfg["host"], port=cfg["port"], database=cfg["database"],
                user=cfg["admin_user"], password=cfg["admin_password"],
            )
            cursor = conn.cursor()
            cursor.execute("DROP USER IF EXISTS %s@'%%'", (username,))
            cursor.execute("FLUSH PRIVILEGES")
            conn.commit()
            conn.close()

        existing = [u for u in existing if not (u["username"] == username and u.get("db", "nest") == db_slug)]
        save_readonly_users(existing)

        log.info("Read-only DB user dropped by %s: %s from %s", session["user"]["email"], username, db_slug)
        return jsonify({"status": "ok", "dropped": username})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Connection pool (for admin user management — reads staff from Nest DB) ──
try:
    pool = mysql.connector.pooling.MySQLConnectionPool(
        pool_name="admin",
        pool_size=2,
        **DB_CONFIG
    )
except Exception as e:
    log.warning("MySQL pool failed to initialize (expected in local dev without MySQL): %s", e)
    pool = None


# ── Briefer removed — now runs as standalone app at egelloC-Corporation/coaching-briefer ──


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5051)), debug=False)
