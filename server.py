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
# Parent-scope the session cookie so it's valid across sibling subdomains
# (aihub.egelloc.com and incubator.egelloc.com during the domain rename).
app.config["SESSION_COOKIE_DOMAIN"] = os.environ.get("SESSION_COOKIE_DOMAIN", ".egelloc.com")
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
        # Referer looks like "https://incubator.egelloc.com/sales-kpi/..."
        # After split: ['https:', 'incubator.egelloc.com', 'sales-kpi', ...]
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

    skip = ("/static", "/health", "/hub-navbar.js", "/favicon", "/apple-touch-icon", "/assets/")
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
ALLOWED_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "egelloc.com")

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
                    f'<a href="/launcher">Back to {_brand_config()["name"]}</a></body></html>',
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
    user = session.get("user") or {}
    if user.get("email"):
        log_event("auth", "logout",
                  user_email=user.get("email"),
                  user_name=user.get("name"),
                  app_slug="admin",
                  ip_address=request.headers.get("X-Forwarded-For", request.remote_addr))
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


def _brand_config():
    """Instance branding — lets the same codebase render as Incubator or Playground."""
    return {
        "name":     os.environ.get("INSTANCE_NAME", "Incubator"),
        "tagline":  os.environ.get("INSTANCE_TAGLINE", "Where tools hatch."),
        "logo_url": os.environ.get("INSTANCE_LOGO_URL", "/assets/incubator-logo.png"),
        "domain":   os.environ.get("APP_DOMAIN", "incubator.egelloc.com"),
    }


def _features_config():
    """Per-instance feature toggles.

    Defaults preserve incubator behavior. Playground disables internal-only
    surfaces (e.g. infra_access manages DO firewall + Nest/Acq DB users —
    irrelevant for a student-facing box).
    """
    def bool_env(key, default=True):
        return os.environ.get(key, "true" if default else "false") \
            .strip().lower() in ("1", "true", "yes", "on")
    return {
        "infra_access": bool_env("FEATURES_INFRA_ACCESS", True),
    }


def feature_required(feature):
    """404 the route when the named feature is disabled for this instance."""
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not _features_config().get(feature, False):
                return jsonify({"error": "Not available on this instance"}), 404
            return f(*args, **kwargs)
        return decorated
    return decorator


@app.route("/auth/me")
def auth_me():
    """Return current user info (for the frontend to display)."""
    user = session.get("user")
    if not user:
        return jsonify({"authenticated": False,
                        "brand": _brand_config(),
                        "features": _features_config()}), 401
    perms = get_user_permissions(user["email"])
    return jsonify({"authenticated": True, **user, "permissions": perms,
                    "brand": _brand_config(),
                    "features": _features_config()})


@app.route("/config/brand.js")
def brand_js():
    """Synchronous brand+features config so pages can render without flicker."""
    body = (
        f"window.AIHUB_BRAND = {json.dumps(_brand_config())};\n"
        f"window.AIHUB_FEATURES = {json.dumps(_features_config())};\n"
    )
    return Response(body, mimetype="application/javascript")


@app.route("/hub-navbar.js")
def hub_navbar_js():
    """Serve the app-switcher navbar script with instance config prepended.

    Apps include <script src="/hub-navbar.js"> directly — they don't also
    load /config/brand.js. Prepend window.AIHUB_BRAND + AIHUB_FEATURES so
    the banner wordmark, favicon, and feature-gated UI pieces render with
    the correct instance name. `|| existing value` preserves anything the
    host page already set (e.g. admin panel's synchronous /config/brand.js).
    """
    path = os.path.join(os.path.dirname(__file__), "hub-navbar.js")
    try:
        with open(path) as f:
            body = f.read()
    except IOError:
        return Response("// hub-navbar.js not found\n", status=500,
                        mimetype="application/javascript")
    prefix = (
        f"window.AIHUB_BRAND = window.AIHUB_BRAND || {json.dumps(_brand_config())};\n"
        f"window.AIHUB_FEATURES = window.AIHUB_FEATURES || {json.dumps(_features_config())};\n"
    )
    resp = Response(prefix + body, mimetype="application/javascript")
    # Don't let browsers cache across instances — cheap to re-send.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/favicon.ico")
def favicon_ico():
    # Most browsers fall back to /favicon.ico if they don't find an explicit
    # <link rel="icon">. Serve the 32×32 PNG — browsers accept PNG here.
    return send_from_directory("favicon", "32.png", mimetype="image/png")


@app.route("/favicon.svg")
def favicon_svg():
    return send_from_directory("favicon", "favicon.svg", mimetype="image/svg+xml")


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return send_from_directory("favicon", "180.png", mimetype="image/png")


@app.route("/favicon/<path:filename>")
def favicon_asset(filename):
    return send_from_directory("favicon", filename)


@app.route("/assets/<path:filename>")
def asset(filename):
    return send_from_directory("assets", filename)


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
    """Used by Nginx auth_request to gate all of incubator.egelloc.com."""
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
                f'<a href="/launcher">Back to {_brand_config()["name"]}</a></body></html>',
                status=403, content_type="text/html",
            )
        return f(*args, **kwargs)
    return decorated


@app.route("/admin")
@app.route("/admin/permissions")
@app.route("/admin/infrastructure")
@app.route("/admin/vps-users")
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

    # Load hidden users + name/role overrides from permissions.db
    from permissions import get_db as _get_pdb
    pconn = _get_pdb()
    try:
        hidden = {row["email"].lower() for row in
                  pconn.execute("SELECT email FROM hidden_users").fetchall()}
    except Exception:
        hidden = set()
    try:
        labels = {row["email"].lower(): row for row in
                  pconn.execute("SELECT email, first_name, last_name FROM user_labels").fetchall()}
    except Exception:
        labels = {}
    try:
        role_overrides = {row["email"].lower(): row["roles"] for row in
                          pconn.execute("SELECT email, roles FROM user_role_overrides").fetchall()}
    except Exception:
        role_overrides = {}
    pconn.close()

    # Filter out hidden users
    users = [u for u in users if u["email"].lower() not in hidden]

    for u in users:
        override = labels.get(u["email"].lower())
        if override:
            u["first_name"] = override["first_name"]
            u["last_name"] = override["last_name"]
            u["name_edited"] = True
        # Role override: when set, replaces Nest roles entirely. Keep the raw
        # value on u["roles_original"] so the UI can show "overridden" state.
        role_override = role_overrides.get(u["email"].lower())
        if role_override is not None:
            u["roles_original"] = u.get("roles", "") or u.get("role", "")
            u["roles"] = role_override
            u["roles_edited"] = True

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
    log_event("permission", "grant_app_access",
              user_email=admin_email,
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"{email} → {app_slug}",
              metadata={"target_email": email, "app_slug": app_slug})
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
    admin_email = session["user"]["email"]
    log_event("permission", "revoke_app_access",
              user_email=admin_email,
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"{email} → {app_slug}",
              metadata={"target_email": email, "app_slug": app_slug})
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
    log_event("user_management", "create_custom_user",
              user_email=admin_email,
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"{email} ({first_name} {last_name}, role={role or 'none'})",
              metadata={"target_email": email, "name": f"{first_name} {last_name}", "role": role})
    return jsonify({"status": "added"})


@app.route("/admin/api/remove-user", methods=["POST"])
@admin_required
def admin_remove_user():
    body = request.get_json()
    email = body.get("email", "").strip()
    if not email:
        return jsonify({"error": "email required"}), 400

    remove_custom_user(email)
    admin_email = session["user"]["email"]
    log_event("user_management", "delete_custom_user",
              user_email=admin_email,
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=email,
              metadata={"target_email": email})
    return jsonify({"status": "removed"})


DROPPED_ROLES = {"Strategist"}


def _normalize_role(raw: str) -> str:
    """Match admin.html's client-side role normalization exactly:
    replace underscores, title-case each word, merge synonyms
    (Super Admin → Admin, Cx → Client Experience), and drop retired
    roles (DROPPED_ROLES) so they disappear from every surface."""
    if not raw:
        return ""
    s = raw.replace("_", " ").strip()
    if not s:
        return ""
    s = " ".join(w[0].upper() + w[1:].lower() for w in s.split() if w)
    if s == "Super Admin":
        s = "Admin"
    if s == "Cx":
        s = "Client Experience"
    if s in DROPPED_ROLES:
        return ""
    return s


@app.route("/admin/api/permission-groups")
@admin_required
def admin_permission_groups():
    """Groups of users by role for the Incubator Logs multi-user filter's
    preset dropdown.

    Returns:
        {"groups": [{"id": "role-<slug>", "label": "<Role>", "emails": [...]}, ...]}

    The Incubator Logs dashboard fetches this via its own /api/user-groups
    proxy, which forwards the caller's session cookie so this admin-only
    endpoint stays gated.

    Role source: `roles` column on nest MySQL staff + `role` on custom users,
    with user_role_overrides (from this panel's role editor) taking
    precedence. Everything flows through _normalize_role so both dropdowns
    share vocabulary and DROPPED_ROLES/synonym merges apply uniformly.

    Served no-store so role edits reach the Incubator Logs dropdown on the
    next refetch instead of sitting in an HTTP cache.
    """
    # Fetch staff + custom users (same sources admin_users() uses)
    if pool:
        conn = pool.get_connection()
        try:
            staff = get_egelloc_staff(conn)
        finally:
            conn.close()
    else:
        staff = []
    custom = get_custom_users()

    # Collect hidden users + role overrides from permissions.db. Overrides
    # replace Nest roles entirely (same shape as admin_users), so a user
    # whose Nest role is wrong can be re-grouped without waiting for the
    # Nest side to update.
    from permissions import get_db as _get_pdb
    pconn = _get_pdb()
    try:
        hidden = {row["email"].lower() for row in
                  pconn.execute("SELECT email FROM hidden_users").fetchall()}
    except Exception:
        hidden = set()
    try:
        role_overrides = {row["email"].lower(): row["roles"] for row in
                          pconn.execute("SELECT email, roles FROM user_role_overrides").fetchall()}
    except Exception:
        role_overrides = {}
    pconn.close()

    # Invert: {normalized_role: set(emails)}
    by_role: dict[str, set[str]] = {}

    def _add(email: str, raw_roles: str):
        if not email:
            return
        if email.lower() in hidden:
            return
        # Prefer override if present
        effective = role_overrides.get(email.lower(), raw_roles)
        for chunk in (effective or "").split(","):
            norm = _normalize_role(chunk)
            if norm:
                by_role.setdefault(norm, set()).add(email)

    for s in staff:
        _add(s.get("email", ""), s.get("roles", ""))
    for cu in custom:
        # Custom users store role singular; treat it as a one-role list.
        _add(cu.get("email", ""), cu.get("role", ""))

    groups = []
    for label in sorted(by_role.keys()):
        emails = sorted(by_role[label])
        groups.append({
            "id": f"role-{label.lower().replace(' ', '-')}",
            "label": label,
            "emails": emails,
        })

    resp = jsonify({"groups": groups})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/admin/api/bulk", methods=["POST"])
@admin_required
def admin_bulk():
    """Bulk grant/revoke permissions. Logs each grant/revoke individually so
    the audit trail matches single-action calls; an auditor can reconstruct
    what changed without interpreting an opaque 'bulk_change' row."""
    body = request.get_json()
    actions = body.get("actions", [])
    admin_email = session["user"]["email"]
    admin_name = session["user"].get("name")

    processed = 0
    for action in actions:
        email = action.get("email", "").strip()
        app_slug = action.get("app_slug", "").strip()
        op = action.get("action", "")
        if not email or not app_slug:
            continue
        if op == "grant":
            grant_permission(email, app_slug, admin_email)
            log_event("permission", "grant_app_access",
                      user_email=admin_email, user_name=admin_name, app_slug="admin",
                      detail=f"{email} → {app_slug}",
                      metadata={"target_email": email, "app_slug": app_slug, "source": "bulk"})
            processed += 1
        elif op == "revoke":
            if app_slug == "admin" and email.lower() in [e.lower() for e in PROTECTED_ADMINS]:
                continue
            revoke_permission(email, app_slug)
            log_event("permission", "revoke_app_access",
                      user_email=admin_email, user_name=admin_name, app_slug="admin",
                      detail=f"{email} → {app_slug}",
                      metadata={"target_email": email, "app_slug": app_slug, "source": "bulk"})
            processed += 1

    return jsonify({"status": "ok", "processed": processed})


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
    log_event("app_registry", "submit_app",
              user_email=submitted_by,
              user_name=session["user"].get("name"),
              app_slug=slug,
              detail=f"{name} ({repo_url})",
              metadata={"port": port, "repo_url": repo_url, "repo_subdir": repo_subdir})
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

    log_event("app_registry", "approve_app",
              user_email=reviewed_by,
              user_name=session["user"].get("name"),
              app_slug=result.get("slug") or str(submission_id),
              metadata={"submission_id": submission_id})

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
    log_event("app_registry", "edit_app",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug=result.get("slug") or str(submission_id),
              metadata={"submission_id": submission_id,
                        "fields": [k for k in ("slug","name","description","icon","port","streamlit_port","repo_url","env_keys") if body.get(k) is not None]})
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
    log_event("app_registry", "reject_app",
              user_email=reviewed_by,
              user_name=session["user"].get("name"),
              app_slug=result.get("slug") or str(submission_id),
              detail=reason or "(no reason)",
              metadata={"submission_id": submission_id, "reason": reason})
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

    log_event("app_registry", "delete_app",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug=result.get("slug") or str(submission_id),
              metadata={"submission_id": submission_id, "was_live": bool(result.get("was_live"))})

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


def _normalize_repo(url):
    return (url or "").lower().rstrip("/").removesuffix(".git")


@app.route("/admin/api/apps/webhook-status")
@admin_required
def api_webhook_status():
    """Check which apps have GitHub webhooks configured.
    Reads from webhook_seen table (persisted across restarts)."""
    from permissions import get_db as _get_pdb
    conn = _get_pdb()
    try:
        seen = {row["repo_url"] for row in conn.execute("SELECT repo_url FROM webhook_seen").fetchall()}
    except Exception as e:
        log.warning("webhook_seen read failed: %s", e)
        seen = set()
    finally:
        conn.close()

    results = {}
    for s in get_all_submissions():
        if s.get("status") not in ("live", "approved"):
            continue
        repo_url = _normalize_repo(s.get("repo_url"))
        if not repo_url:
            results[s["slug"]] = None
            continue
        results[s["slug"]] = repo_url in seen
    return jsonify(results)


@app.route("/admin/api/users/hide", methods=["POST"])
@admin_required
def api_hide_user():
    """Hide a user from the permissions list. For Nest DB users that can't
    be deleted from the source — hides them on the platform side."""
    body = request.get_json()
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    from permissions import get_db as _get_pdb
    conn = _get_pdb()
    conn.execute(
        "INSERT OR IGNORE INTO hidden_users (email, hidden_by) VALUES (?, ?)",
        (email, session["user"]["email"]),
    )
    conn.commit()
    conn.close()
    log_event("user_management", "hide_user",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin", detail=email,
              metadata={"target_email": email})
    return jsonify({"status": "ok"})


@app.route("/admin/api/users/rename", methods=["POST"])
@admin_required
def api_rename_user():
    """Override the display name for a user. Stored in user_labels table."""
    body = request.get_json()
    email = (body.get("email") or "").strip().lower()
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()

    if not email or not first_name or not last_name:
        return jsonify({"error": "email, first_name, and last_name are required"}), 400

    from permissions import get_db as _get_pdb
    conn = _get_pdb()
    conn.execute(
        """INSERT INTO user_labels (email, first_name, last_name, updated_by)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(email) DO UPDATE SET
             first_name = excluded.first_name,
             last_name = excluded.last_name,
             updated_by = excluded.updated_by,
             updated_at = datetime('now')""",
        (email, first_name, last_name, session["user"]["email"]),
    )
    conn.commit()
    conn.close()
    log_event("user_management", "rename_user",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"{email} → {first_name} {last_name}",
              metadata={"target_email": email, "new_name": f"{first_name} {last_name}"})
    return jsonify({"status": "ok"})


# Default picker options. Not a whitelist — the editor lets admins type
# new roles freely (validated by ROLE_NAME_RE below). Listed here so
# common roles appear as preset chips and the Incubator Logs Groups
# dropdown has a predictable ordering.
CANONICAL_ROLES = ["Admin", "Client Experience", "Coach", "Marketing", "Sales"]

# Keep in sync with the client-side regex in admin.html. Allows letters,
# digits, spaces, and hyphens; must start with a letter; 2–40 chars.
ROLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 \-]{1,39}$")


@app.route("/admin/api/users/update-roles", methods=["POST"])
@admin_required
def api_update_user_roles():
    """Override a user's roles. Stored in user_role_overrides (takes
    precedence over Nest-sourced roles). Roles pass through the shared
    normalizer (title-case, synonym merge, DROPPED_ROLES filter) so the
    DB never stores retired or mis-cased values.
    Pass an empty list to remove the override and fall back to Nest."""
    body = request.get_json() or {}
    email = (body.get("email") or "").strip().lower()
    roles = body.get("roles")

    if not email:
        return jsonify({"error": "email required"}), 400
    if not isinstance(roles, list):
        return jsonify({"error": "roles must be a list"}), 400

    cleaned = []
    for r in roles:
        if not isinstance(r, str):
            continue
        trimmed = r.strip()
        if not trimmed:
            continue
        if not ROLE_NAME_RE.match(trimmed):
            return jsonify({"error": f"invalid role name: {r!r}"}), 400
        normalized = _normalize_role(trimmed)
        if not normalized:
            # Silently drop retired roles (DROPPED_ROLES) so the UI can
            # resubmit whatever it showed without failing.
            continue
        cleaned.append(normalized)
    # Deduplicate preserving order
    seen = set()
    cleaned = [r for r in cleaned if not (r in seen or seen.add(r))]

    from permissions import get_db as _get_pdb
    conn = _get_pdb()
    if not cleaned:
        # Empty list → remove the override, revert to Nest
        conn.execute("DELETE FROM user_role_overrides WHERE email = ?", (email,))
    else:
        conn.execute(
            """INSERT INTO user_role_overrides (email, roles, updated_by)
               VALUES (?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET
                 roles = excluded.roles,
                 updated_by = excluded.updated_by,
                 updated_at = datetime('now')""",
            (email, ",".join(cleaned), session["user"]["email"]),
        )
    conn.commit()
    conn.close()
    log_event("user_management", "update_user_roles",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"{email} → {', '.join(cleaned) if cleaned else '(reverted to Nest)'}",
              metadata={"target_email": email, "roles": cleaned})
    return jsonify({"status": "ok", "roles": cleaned})


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
    log_event("app_registry", "update_app_status",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug=slug, detail=status,
              metadata={"status": status})
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

    log_event("app_deploy", "deploy_start" if not dry_run else "deploy_dry_run",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug=app_name,
              detail=f"port {port}" + (" (dry run)" if dry_run else ""),
              metadata={"port": port, "streamlit_port": streamlit_port,
                        "repo_url": repo_url, "dry_run": bool(dry_run)})

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

    log_event("app_deploy", "undeploy_start",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug=app_name)

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


def _record_webhook_delivery(repo_url):
    """Persist that this repo has reached our webhook endpoint.
    Powers the "Auto-deploy active" indicator in the registry."""
    if not repo_url:
        return
    try:
        from permissions import get_db as _get_pdb
        conn = _get_pdb()
        conn.execute(
            "INSERT OR REPLACE INTO webhook_seen (repo_url, last_seen) VALUES (?, datetime('now'))",
            (_normalize_repo(repo_url),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("webhook_seen write failed for %s: %s", repo_url, e)


@app.route("/webhook/github", methods=["POST"])
def github_webhook():
    """Auto-deploy apps when code is pushed to GitHub."""
    # Verify signature
    payload = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(payload, signature):
        return jsonify({"error": "Invalid signature"}), 403

    event = request.headers.get("X-GitHub-Event", "")
    body = request.get_json(silent=True) or {}
    repo_url = body.get("repository", {}).get("clone_url", "")
    repo_name = body.get("repository", {}).get("full_name", "")

    # ping = GitHub's "Test delivery" click (also sent when a webhook is
    # first created). Record the signal so the registry can show the
    # webhook is reachable, but don't deploy.
    if event == "ping":
        _record_webhook_delivery(repo_url)
        log.info("Webhook ping: %s", repo_name)
        return jsonify({"status": "pong", "repo": repo_name}), 200

    if event != "push":
        return jsonify({"status": "ignored", "event": event}), 200

    branch = body.get("ref", "").replace("refs/heads/", "")

    # Only deploy from main/master branch
    if branch not in ("main", "master"):
        return jsonify({"status": "ignored", "branch": branch}), 200

    log.info("Webhook push: %s (branch: %s)", repo_name, branch)

    # Audit log: every valid push. Powers the Git Activity feed in incubator-logs.
    try:
        pusher = body.get("pusher") or {}
        head_commit = body.get("head_commit") or {}
        author = head_commit.get("author") or {}
        commits_arr = body.get("commits") or []

        # Pusher email can be a GitHub noreply — fall back to the human name
        # or the commit author's email so the feed isn't full of anonymous rows.
        pusher_email = pusher.get("email") or ""
        if pusher_email.endswith("@users.noreply.github.com"):
            pusher_email = pusher.get("name") or author.get("email") or pusher_email

        # Match the pushed repo to an app_submissions row (any status).
        push_app_slug = None
        try:
            from permissions import get_db as _gadb
            _c = _gadb()
            _srows = _c.execute(
                "SELECT slug, repo_url FROM app_submissions WHERE repo_url != ''"
            ).fetchall()
            _c.close()
            _nkey = _normalize_repo(repo_url)
            for _sr in _srows:
                if _normalize_repo(_sr["repo_url"]) == _nkey:
                    push_app_slug = _sr["slug"]
                    break
        except Exception as _e:
            log.warning("git_push app_slug lookup failed: %s", _e)

        head_sha = head_commit.get("id") or ""
        head_msg = (head_commit.get("message") or "").split("\n", 1)[0][:200]

        log_event(
            "git_push",
            f"push to {branch}",
            user_email=pusher_email or None,
            user_name=pusher.get("name") or author.get("name"),
            app_slug=push_app_slug,
            detail=head_msg or None,
            metadata={
                "sha": head_sha[:12],
                "full_sha": head_sha,
                "repo": repo_name,
                "branch": branch,
                "commit_count": len(commits_arr),
                "commit_url": head_commit.get("url"),
            },
        )
    except Exception as _e:
        log.warning("git_push log_event failed: %s", _e)

    # Must happen BEFORE any self-deploy trigger — otherwise the restart
    # that follows wipes the in-flight write.
    _record_webhook_delivery(repo_url)

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

    normalized_push = _normalize_repo(repo_url)
    for row in rows:
        if row["slug"] in SPECIAL_CASE_SLUGS:
            continue
        if _normalize_repo(row["repo_url"]) == normalized_push:
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
    log_event("infra_config", "set_ip_label",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"{key} → {label}",
              metadata={"key": key, "label": label})
    return jsonify({"status": "ok"})


@app.route("/admin/api/ip-labels/remove", methods=["POST"])
@admin_required
def api_remove_ip_label():
    body = request.get_json()
    key = body.get("key", "")
    labels = load_ip_labels()
    labels.pop(key, None)
    save_ip_labels(labels)
    log_event("infra_config", "remove_ip_label",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin", detail=key,
              metadata={"key": key})
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

    log_event("infra_access", "set_ssh_alias",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=f"key {key_id} → {alias or '(cleared)'}",
              metadata={"key_id": key_id, "alias": alias})
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
    log_event("infra_access", "add_ssh_key",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=comment or parts[-1],
              metadata={"comment": comment, "key_type": parts[0] if parts else ""})
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

    log_event("infra_access", "remove_ssh_key",
              user_email=session["user"]["email"],
              user_name=session["user"].get("name"),
              app_slug="admin",
              detail=confirm_comment or removed[:40],
              metadata={"key_id": key_id, "comment": confirm_comment})
    return jsonify({"status": "ok", "removed": confirm_comment or removed[:40]})


# ── VPS host users ──
# Thin proxies over deploy-service's /host-users endpoints. Deploy-service
# is the only component that can shell into the host (via the nsenter
# pattern), so all mutations route through it. All gated by @admin_required
# and audit-logged.

def _proxy_deploy(method, path, json_body=None, timeout=60):
    """Forward a request to deploy-service and mirror its (status, json)."""
    url = f"{DEPLOY_SERVICE_URL}{path}"
    try:
        resp = http_requests.request(method, url, json=json_body, timeout=timeout)
    except http_requests.RequestException as e:
        return jsonify({"error": f"deploy-service unreachable: {e}"}), 502
    try:
        return jsonify(resp.json()), resp.status_code
    except ValueError:
        return jsonify({"error": "deploy-service returned non-JSON",
                        "body": resp.text[:400]}), 502


@app.route("/admin/api/vps-users")
@admin_required
def admin_vps_users_list():
    """List VPS shell users (proxy → deploy-service)."""
    return _proxy_deploy("GET", "/host-users")


@app.route("/admin/api/vps-users", methods=["POST"])
@admin_required
def admin_vps_users_create():
    """Create a VPS shell user with one SSH pubkey."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    pubkey = (body.get("pubkey") or "").strip()
    grant_sudo = bool(body.get("sudo", False))

    resp, status = _proxy_deploy(
        "POST", "/host-users",
        json_body={"name": name, "pubkey": pubkey, "sudo": grant_sudo},
    )
    if status in (200, 201):
        log_event("infra_access", "create_vps_user",
                  user_email=session["user"]["email"],
                  user_name=session["user"].get("name"),
                  app_slug="admin",
                  detail=f"{name} (sudo={grant_sudo})",
                  metadata={"name": name, "sudo": grant_sudo,
                            "key_type": pubkey.split()[0] if pubkey else ""})
    return resp, status


@app.route("/admin/api/vps-users/<name>", methods=["DELETE"])
@admin_required
def admin_vps_users_delete(name):
    """Delete a VPS shell user (userdel -r)."""
    resp, status = _proxy_deploy("DELETE", f"/host-users/{name}")
    if status == 200:
        log_event("infra_access", "delete_vps_user",
                  user_email=session["user"]["email"],
                  user_name=session["user"].get("name"),
                  app_slug="admin",
                  detail=name,
                  metadata={"name": name})
    return resp, status


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
        # Read-replica endpoint. Users + credentials propagate from the
        # primary automatically (same DO cluster), so nothing to manage
        # here beyond surfacing the host for sync-check. Blank = disabled.
        "replica_host": os.environ.get("NEST_REPLICA_HOST", ""),
        "replica_port": int(os.environ.get("NEST_REPLICA_PORT", "25060")),
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
        "replica_cluster_id": os.environ.get("DO_DB_REPLICA_NEST", ""),
    },
    "acquisition": {
        "token": os.environ.get("DO_API_TOKEN_ACQ", ""),
        "cluster_id": os.environ.get("DO_DB_CLUSTER_ACQ", ""),
    },
}
PROTECTED_IPS = [ip.strip() for ip in os.environ.get(
    "PROTECTED_IPS", "165.232.155.132"
).split(",") if ip.strip()]  # Droplet IPs — never removable from DB firewall

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
@feature_required("infra_access")
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
@feature_required("infra_access")
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
        log_event("infra_network", "add_trusted_ip",
                  user_email=session["user"]["email"],
                  user_name=session["user"].get("name"),
                  app_slug="admin",
                  detail=f"{ip} on {db_slug}",
                  metadata={"ip": ip, "db": db_slug})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/network-access/remove", methods=["POST"])
@admin_required
@feature_required("infra_access")
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
        log_event("infra_network", "remove_trusted_ip",
                  user_email=session["user"]["email"],
                  user_name=session["user"].get("name"),
                  app_slug="admin",
                  detail=f"{ip} from {db_slug}",
                  metadata={"ip": ip, "db": db_slug})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/my-ip")
@admin_required
@feature_required("infra_access")
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
@feature_required("infra_access")
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
@feature_required("infra_access")
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
        log_event("db_user", "create_readonly_user",
                  user_email=session["user"]["email"],
                  user_name=session["user"].get("name"),
                  app_slug="admin",
                  detail=f"{username} on {db_slug}",
                  metadata={"username": username, "db": db_slug})

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
@feature_required("infra_access")
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
        log_event("db_user", "drop_readonly_user",
                  user_email=session["user"]["email"],
                  user_name=session["user"].get("name"),
                  app_slug="admin",
                  detail=f"{username} from {db_slug}",
                  metadata={"username": username, "db": db_slug})
        return jsonify({"status": "ok", "dropped": username})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/api/db-sync-check")
@admin_required
def admin_db_sync_check():
    """Verify primary/replica parity for a managed DB. Read-only. Returns a
    diff of:
      - mysql.user: auth_string fingerprint per user (users are replicated
        in-cluster, so this should always be empty — but DO occasionally
        has replication edge cases and this flags them loudly).
      - DO firewall rules: the panel manages rules per cluster; replica
        firewall is independent, so drift is expected and this surfaces it.

    Only implemented for 'nest' today since that's the only cluster with
    a configured replica. Quietly returns empty diffs for others.
    """
    db_slug = request.args.get("db", "nest")
    cfg = MANAGED_DATABASES.get(db_slug)
    if not cfg:
        return jsonify({"error": f"Unknown database: {db_slug}"}), 400
    if cfg.get("engine") == "pg":
        return jsonify({"error": "Sync check only supported for MySQL clusters"}), 400

    replica_host = cfg.get("replica_host", "")
    if not replica_host:
        return jsonify({"error": f"No replica configured for {db_slug}. Set NEST_REPLICA_HOST in platform.env."}), 400

    do_cfg = DO_CONFIGS.get(db_slug, {})
    primary_cluster = do_cfg.get("cluster_id", "")
    replica_cluster = do_cfg.get("replica_cluster_id", "")

    out = {
        "db": db_slug,
        "primary_host": cfg["host"],
        "replica_host": replica_host,
        "users": {"primary_only": [], "replica_only": [], "hash_mismatch": [], "match_count": 0},
        "firewall": {"primary_only": [], "replica_only": [], "match_count": 0},
        "errors": [],
    }

    # --- mysql.user parity ---
    def _mysql_users(host):
        conn = mysql.connector.connect(
            host=host, port=cfg["port"], database="mysql",
            user=cfg["admin_user"], password=cfg["admin_password"],
            ssl_disabled=False, connection_timeout=10,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT User, Host, plugin, SHA1(authentication_string) FROM mysql.user "
            "WHERE User NOT IN ('mysql.sys','mysql.session','mysql.infoschema') "
            "ORDER BY User, Host"
        )
        rows = cur.fetchall()
        conn.close()
        return {(u, h): (plugin, fp) for u, h, plugin, fp in rows}

    try:
        p_users = _mysql_users(cfg["host"])
        r_users = _mysql_users(replica_host)
        p_keys, r_keys = set(p_users), set(r_users)
        out["users"]["primary_only"] = [f"{u}@{h}" for u, h in sorted(p_keys - r_keys)]
        out["users"]["replica_only"] = [f"{u}@{h}" for u, h in sorted(r_keys - p_keys)]
        common = p_keys & r_keys
        mismatch = [f"{u}@{h}" for (u, h) in sorted(common) if p_users[(u, h)] != r_users[(u, h)]]
        out["users"]["hash_mismatch"] = mismatch
        out["users"]["match_count"] = len(common) - len(mismatch)
    except Exception as e:
        out["errors"].append(f"mysql.user diff failed: {type(e).__name__}: {e}")

    # --- DO firewall parity ---
    if do_cfg.get("token") and primary_cluster and replica_cluster:
        def _do_rules(cluster_id):
            resp = http_requests.get(
                f"https://api.digitalocean.com/v2/databases/{cluster_id}/firewall",
                headers={"Authorization": f"Bearer {do_cfg['token']}"},
                timeout=10,
            )
            resp.raise_for_status()
            return sorted({r["value"] for r in resp.json().get("rules", [])})
        try:
            p_ips = set(_do_rules(primary_cluster))
            r_ips = set(_do_rules(replica_cluster))
            out["firewall"]["primary_only"] = sorted(p_ips - r_ips)
            out["firewall"]["replica_only"] = sorted(r_ips - p_ips)
            out["firewall"]["match_count"] = len(p_ips & r_ips)
        except Exception as e:
            out["errors"].append(f"firewall diff failed: {type(e).__name__}: {e}")
    else:
        out["errors"].append("DO API token or replica_cluster_id not configured — firewall check skipped")

    u = out["users"]
    f = out["firewall"]
    out["in_sync"] = (
        not u["primary_only"] and not u["replica_only"] and not u["hash_mismatch"]
        and not f["primary_only"] and not f["replica_only"]
    )
    return jsonify(out)


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
