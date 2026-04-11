"""
AI Hub Auth SDK — Python

Validates requests against AI Hub's SSO.
Works with Flask, FastAPI, or any WSGI/ASGI framework.

In production, forwards the session cookie to the central auth server.
For local dev, set AIHUB_DEV_EMAIL in your .env to bypass SSO entirely.

Usage (Flask):
    from aihub_auth import verify_user, login_required

    @app.route("/protected")
    @login_required
    def protected():
        return "Hello, authenticated user!"

Usage (manual):
    user = verify_user(request)
    # Returns {"email": "...", "name": "...", "permissions": [...]} or None
"""

import os
import functools
import logging
import requests as http_requests

log = logging.getLogger(__name__)

# AI Hub auth endpoint — override with AIHUB_AUTH_URL for local dev
AIHUB_AUTH_URL = os.environ.get("AIHUB_AUTH_URL", "http://localhost:5000/auth/me")
AIHUB_DEV_EMAIL = os.environ.get("AIHUB_DEV_EMAIL", "")

if AIHUB_DEV_EMAIL:
    log.info("aihub-auth: Dev mode — all requests authenticate as %s", AIHUB_DEV_EMAIL)


def verify_user(request):
    """
    Verify the current request against AI Hub SSO.

    In dev mode (AIHUB_DEV_EMAIL set), returns a mock user immediately.
    In production, forwards the session cookie to AI Hub's /auth/me endpoint.

    Args:
        request: A Flask/Werkzeug request object (needs request.cookies).

    Returns:
        dict with {email, name, picture, permissions} or None.
    """
    # Dev mode — skip SSO, return mock user
    if AIHUB_DEV_EMAIL:
        return {
            "email": AIHUB_DEV_EMAIL,
            "name": AIHUB_DEV_EMAIL.split("@")[0],
            "picture": "",
            "permissions": [os.environ.get("APP_SLUG", "dev")],
        }

    cookie = request.cookies.get("session")
    if not cookie:
        return None

    try:
        resp = http_requests.get(
            AIHUB_AUTH_URL,
            cookies={"session": cookie},
            timeout=5,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("authenticated"):
            return None
        return {
            "email": data["email"],
            "name": data.get("name", ""),
            "picture": data.get("picture", ""),
            "permissions": data.get("permissions", []),
        }
    except Exception:
        return None


def require_permission(app_slug):
    """
    Flask decorator — verifies user is authenticated AND has permission for the given app.

    Usage:
        @app.route("/my-endpoint")
        @require_permission("my-app")
        def my_endpoint():
            user = g.user  # available after auth
            return f"Hello {user['email']}"
    """
    def decorator(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            from flask import g, jsonify
            user = verify_user(_get_flask_request())
            if not user:
                return jsonify({"error": "Unauthorized"}), 401
            if app_slug not in user.get("permissions", []):
                return jsonify({"error": "Forbidden — no access to this app"}), 403
            g.user = user
            return f(*args, **kwargs)
        return decorated
    return decorator


def login_required(f):
    """
    Flask decorator — verifies user is authenticated (any app permission).

    Usage:
        @app.route("/my-endpoint")
        @login_required
        def my_endpoint():
            user = g.user
            return f"Hello {user['email']}"
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        from flask import g, jsonify
        user = verify_user(_get_flask_request())
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        g.user = user
        return f(*args, **kwargs)
    return decorated


def _get_flask_request():
    from flask import request
    return request
