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

from flask import Flask, request, jsonify, send_from_directory, Response, redirect, session, url_for
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

sys.path.insert(0, os.path.dirname(__file__))

from coach_briefing import (
    get_student_profile, fetch_fathom_transcripts, match_fathom_to_student,
    strip_html, DB_CONFIG, ANTHROPIC_API_KEY
)
from permissions import (
    get_user_permissions, user_has_permission, grant_permission,
    revoke_permission, get_all_permissions, get_all_apps, get_egelloc_staff,
    add_custom_user, remove_custom_user, get_custom_users,
    APPS,
)
import anthropic
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
        return Response(
            f"Access denied. Only @{ALLOWED_DOMAIN} accounts are allowed.",
            status=403,
            content_type="text/plain",
        )

    session["user"] = {
        "email": email,
        "name": user_info.get("name", ""),
        "picture": user_info.get("picture", ""),
    }
    session.pop("next_url", None)

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
    target = f"http://localhost:3004/{target_path}"
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
SUPER_ADMINS = ["victor@egelloc.com", "tony@egelloc.com", "art@egelloc.com", "dollie@egelloc.com"]


def admin_required(f):
    """Decorator that enforces admin access — must be in SUPER_ADMINS AND have 'admin' permission."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = session.get("user")
        if not user:
            return redirect(url_for("login"))
        email = user["email"].lower()
        if email not in [e.lower() for e in SUPER_ADMINS] or not user_has_permission(email, "admin"):
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
@admin_required
def admin_page():
    return send_from_directory(".", "admin.html")


@app.route("/admin/api/users")
@admin_required
def admin_users():
    """Get all egelloC staff with their current permissions."""
    conn = pool.get_connection()
    try:
        staff = get_egelloc_staff(conn)
    finally:
        conn.close()

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
    if not email.endswith("@egelloc.com"):
        return jsonify({"error": "Only @egelloc.com emails allowed"}), 400

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


# ── Infrastructure access management ──

SSH_AUTHORIZED_KEYS_FILE = os.path.expanduser("~/.ssh/authorized_keys")
SSH_ALIASES_FILE = os.path.join(os.path.dirname(__file__), "ssh_aliases.json")


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


# ── Connection pool (reuses connections instead of opening new ones) ──
pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="briefer",
    pool_size=5,
    **DB_CONFIG
)

# ── Caches ──
fathom_cache = {}       # coach_email -> list of fathom meetings
student_context_cache = {}  # student_id -> { context_str, student_name, timestamp }
clickup_cache = {}      # student_name_lower -> { task_url, drive_url, journey_url, timestamp }

CACHE_TTL = 600  # 10 minutes

# ── ClickUp config ──
CLICKUP_API_KEY = os.environ.get("CLICKUP_API_KEY", "pk_82273715_9P2SGZT15H4YD12FAGZZE93K8ZNMZPR7")
CLICKUP_TEAM_ID = "9014258787"
CLICKUP_HEADERS = {"Authorization": CLICKUP_API_KEY}

# Grade (in 2026) -> graduation year -> space IDs to search
# Students are spread across multiple spaces per grad year
GRAD_YEAR_SPACES = {
    2029: ["90140154381", "90142722483", "90142394488"],  # Class of 2029 + CAB
    2030: ["90141126558", "90142738024", "90142394488"],  # Class of 2030 + CAB
    2031: ["90140906642", "90142394488"],                 # Class of 2031
    2032: ["90144310458"],                                # Class of 2032
    2028: ["90100465292", "90142252727", "90142252742", "90142697687"],  # Class of 2028
    2027: ["90100463417", "90141624922", "90141625025", "90142695093"],  # Class of 2027
    2026: ["90100463408", "90140154380", "90141187706", "90142394484"],  # Class of 2026
}

def grade_to_grad_year(grade):
    """Convert current grade (in 2026) to expected graduation year."""
    return 2026 + (12 - int(grade))


def lookup_clickup_student(student_name, grade):
    """Find a student's ClickUp task, Google Drive, and Journey links."""
    name_lower = student_name.lower().strip()

    # Check cache
    cached = clickup_cache.get(name_lower)
    if cached and (time.time() - cached["timestamp"]) < CACHE_TTL:
        return cached

    result = {"task_url": None, "drive_url": None, "journey_url": None, "timestamp": time.time()}

    try:
        grad_year = grade_to_grad_year(grade)
        spaces = GRAD_YEAR_SPACES.get(grad_year, [])

        # Search each space's folders for a folder matching student name
        folder_id = None
        task_id = None
        for space_id in spaces:
            try:
                resp = http_requests.get(
                    f"https://api.clickup.com/api/v2/space/{space_id}/folder",
                    headers=CLICKUP_HEADERS, timeout=10
                )
                resp.raise_for_status()
                for folder in resp.json().get("folders", []):
                    fname = folder["name"].lower()
                    # Match by last name + first name substring
                    name_parts = name_lower.split()
                    if len(name_parts) >= 2 and name_parts[-1] in fname and name_parts[0] in fname:
                        folder_id = folder["id"]
                        # Extract task ID from folder name (e.g., "Name 2029 BP+- 86b6k2zkh")
                        parts = folder["name"].split("- ")
                        if len(parts) > 1:
                            task_id = parts[-1].strip()
                        break
                if folder_id:
                    break
            except Exception as e:
                log.warning("ClickUp space %s error: %s", space_id, e)

        if task_id:
            result["task_url"] = f"https://app.clickup.com/t/{task_id}"

            # Fetch the task to get custom field URLs
            try:
                resp = http_requests.get(
                    f"https://api.clickup.com/api/v2/task/{task_id}",
                    headers=CLICKUP_HEADERS, timeout=10
                )
                resp.raise_for_status()
                task_data = resp.json()
                for cf in task_data.get("custom_fields", []):
                    val = cf.get("value")
                    if not val:
                        continue
                    name = cf["name"]
                    if name == "Google Drive":
                        result["drive_url"] = val
                    elif name in ("Life Plan", "FAE Plan"):
                        result["journey_url"] = val
            except Exception as e:
                log.warning("ClickUp task %s error: %s", task_id, e)

        clickup_cache[name_lower] = result
        return result

    except Exception as e:
        log.warning("ClickUp lookup error for %s: %s", student_name, e)
        clickup_cache[name_lower] = result
        return result


def build_student_context(student_id):
    """Build the full context string for a student. Returns (context_str, student_name)."""
    conn = pool.get_connection()
    try:
        profile = get_student_profile(conn, student_id)

        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT first_name, last_name, email FROM users WHERE id = %s LIMIT 1",
            (student_id,)
        )
        student_user = cursor.fetchone()
        student_name = f"{student_user['first_name']} {student_user['last_name']}" if student_user else "Unknown"
        student_email = student_user.get("email", "") if student_user else ""

        # Meeting notes
        cursor.execute("""
            SELECT mn.date, mn.subject, mn.meeting_type, mn.note, mn.internal_note,
                   c.first_name AS coach_first, c.last_name AS coach_last, c.email AS coach_email
            FROM meeting_notes mn
            JOIN users c ON mn.coach_id = c.id
            WHERE mn.student_id = %s
            ORDER BY mn.date DESC
            LIMIT 20
        """, (student_id,))
        notes = cursor.fetchall()

        # Fathom transcripts
        coach_email = notes[0].get("coach_email") if notes else None
        fathom_meetings = []
        if coach_email:
            if coach_email not in fathom_cache:
                fathom_cache[coach_email] = fetch_fathom_transcripts(coach_email)
            fathom_meetings = match_fathom_to_student(
                fathom_cache[coach_email], student_email, student_name
            )

        # Build context string
        profile_text = "No profile on file."
        if profile:
            profile_text = (
                f"School: {profile.get('school_name', 'N/A')} ({profile.get('school_state', '')})\n"
                f"Grade: {profile.get('current_grade', 'N/A')} | Grad Year: {profile.get('expected_graduation_year', 'N/A')}\n"
                f"GPA: {profile.get('current_gpa', 'N/A')} ({profile.get('gpa_type', '')})\n"
                f"Career Aspirations: {profile.get('career_aspirations', 'N/A')}\n"
                f"Possible Major: {profile.get('possible_major', 'N/A')}\n"
                f"College Goals: {profile.get('college_goals', 'N/A')}\n"
                f"College Factors: {profile.get('college_factors', 'N/A')}"
            )

        notes_text = ""
        for i, note in enumerate(notes):
            coach = f"{note.get('coach_first', '')} {note.get('coach_last', '')}"
            notes_text += f"\n--- Note {i+1} ({note['date']}, {note.get('meeting_type', 'N/A')}, Coach: {coach}) ---\n"
            notes_text += f"Subject: {note.get('subject', 'N/A')}\n"
            notes_text += f"Notes: {strip_html(note.get('note', ''))}\n"
            if note.get("internal_note"):
                notes_text += f"Internal Coach Note: {strip_html(note['internal_note'])}\n"

        fathom_text = ""
        for i, meeting in enumerate(fathom_meetings):
            fathom_text += f"\n--- Fathom Recording {i+1}: {meeting.get('title', 'Untitled')} ({meeting.get('created_at', '')}) ---\n"
            summary = meeting.get("default_summary")
            if summary and isinstance(summary, dict):
                fathom_text += f"Summary: {summary.get('markdown_formatted', '')}\n"
            if meeting.get("action_items"):
                items = meeting["action_items"]
                if isinstance(items, list):
                    fathom_text += "Action Items:\n"
                    for item in items:
                        assignee = item.get("assignee", {}).get("name", "Unassigned") if isinstance(item, dict) else ""
                        desc = item.get("description", str(item)) if isinstance(item, dict) else str(item)
                        done = " (DONE)" if (isinstance(item, dict) and item.get("completed")) else ""
                        fathom_text += f"  - [{assignee}] {desc}{done}\n"
            if meeting.get("transcript"):
                transcript = meeting["transcript"]
                if isinstance(transcript, list):
                    segments = transcript[:80]
                    fathom_text += "Transcript (excerpt):\n"
                    for seg in segments:
                        speaker = seg.get("speaker", {}).get("display_name", "Unknown")
                        fathom_text += f"  [{speaker}]: {seg.get('text', '')}\n"

        context = f"""STUDENT: {student_name}

STUDENT PROFILE:
{profile_text}

MEETING NOTES (most recent first):
{notes_text if notes_text.strip() else "No meeting notes on file."}

FATHOM CALL RECORDINGS (most recent first):
{fathom_text if fathom_text.strip() else "No Fathom recordings found."}"""

        return context, student_name

    finally:
        conn.close()


def get_cached_context(student_id):
    """Get student context from cache, or build and cache it."""
    cached = student_context_cache.get(student_id)
    if cached and (time.time() - cached["timestamp"]) < CACHE_TTL:
        return cached["context"], cached["student_name"]

    context, student_name = build_student_context(student_id)
    student_context_cache[student_id] = {
        "context": context,
        "student_name": student_name,
        "timestamp": time.time(),
    }
    return context, student_name


@app.route("/briefer/")
@app.route("/briefer/<coach_name>/")
@login_required
@app_permission_required("briefer")
def briefer_index(coach_name=None):
    return send_from_directory(".", "index_prod.html")


@app.route("/briefer/assets/<path:filename>")
@app.route("/assets/<path:filename>")
@login_required
def assets(filename):
    return send_from_directory("assets", filename)


@app.route("/briefer/clickup", methods=["POST"])
@app.route("/clickup", methods=["POST"])
@login_required
@app_permission_required("briefer")
def clickup_lookup():
    """Look up a student's ClickUp task, Google Drive, and Journey links."""
    body = request.get_json()
    student_name = body.get("student_name", "").strip()
    grade = body.get("grade", "")

    if not student_name or not grade:
        return jsonify({"error": "student_name and grade are required"}), 400

    try:
        result = lookup_clickup_student(student_name, grade)
        return jsonify(result)
    except Exception as e:
        log.exception("Error in ClickUp lookup for %s", student_name)
        return jsonify({"error": str(e)}), 500


@app.route("/briefer/preload", methods=["POST"])
@app.route("/preload", methods=["POST"])
@login_required
@app_permission_required("briefer")
def preload():
    """Pre-cache student context when their card is clicked."""
    body = request.get_json()
    student_id = body.get("student_id", "").strip()
    if not student_id:
        return jsonify({"error": "student_id required"}), 400

    try:
        get_cached_context(student_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.exception("Error preloading student %s", student_id)
        return jsonify({"error": str(e)}), 500


@app.route("/briefer/ask", methods=["POST"])
@app.route("/ask", methods=["POST"])
@login_required
@app_permission_required("briefer")
def ask():
    """Stream an AI answer grounded in student data."""
    body = request.get_json()
    question = body.get("question", "").strip()
    student_id = body.get("student_id", "").strip()

    if not question or not student_id:
        return jsonify({"error": "question and student_id are required"}), 400

    try:
        context, student_name = get_cached_context(student_id)

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        def generate():
            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""You are an AI assistant for college admissions coaches at egelloC. A coach is asking a question about their student. Answer based ONLY on the data provided below. If the information isn't available in the data, say so honestly.

{context}

COACH'S QUESTION: {question}

Answer concisely and directly. Reference specific dates, notes, or transcript moments when relevant. If you're citing something from a call transcript, mention the approximate context. Format your response with markdown for readability."""
                }]
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
            yield "data: [DONE]\n\n"

        return Response(generate(), mimetype="text/event-stream")

    except Exception as e:
        log.exception("Error processing /ask")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5051)), debug=False)
