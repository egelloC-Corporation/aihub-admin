"""
Incubator Deploy Service

Standalone Flask app (port 5001) that handles deploy/undeploy requests
from the admin panel. Called by server.py when an app submission is approved.

Wraps scripts/deploy.py, scripts/nginx_config.py, and scripts/db_provision.py.
"""

import os
import re
import sys
import json
import shlex
import logging
import subprocess

from flask import Flask, request, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
from deploy import deploy_app, undeploy_app, test_app, validate_submission

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "deploy"})


@app.route("/deploy", methods=["POST"])
def deploy():
    """
    Deploy an app.

    Expected JSON from server.py's approve endpoint:
        {"slug": "myapp", "port": 3005, "submission_id": 1}

    Also accepts the expanded format:
        {"app_name": "myapp", "port": 3005, "repo_url": "...", "dry_run": false}
    """
    body = request.get_json(silent=True) or {}

    app_name = body.get("app_name") or body.get("slug")
    port = body.get("port")
    streamlit_port = body.get("streamlit_port")
    repo_url = body.get("repo_url")
    local_path = body.get("local_path")
    repo_subdir = body.get("repo_subdir")
    dry_run = body.get("dry_run", False)

    if not app_name:
        return jsonify({"error": "app_name or slug is required"}), 400
    if not port:
        return jsonify({"error": "port is required"}), 400

    log.info("Deploy request: app=%s port=%s streamlit_port=%s dry_run=%s", app_name, port, streamlit_port, dry_run)

    result = deploy_app(
        app_name=app_name,
        port=int(port),
        repo_url=repo_url,
        local_path=local_path,
        repo_subdir=repo_subdir,
        streamlit_port=int(streamlit_port) if streamlit_port else None,
        dry_run=dry_run,
    )

    log.info("Deploy result: %s", result.get("status"))

    status_code = 200 if result["status"] in ("deployed", "dry_run") else 500
    return jsonify(result), status_code


@app.route("/validate", methods=["POST"])
def validate():
    """
    Submission-time validation. Checks things the submitter can fix.
    Called by server.py when a user submits an app for review.
    """
    body = request.get_json(silent=True) or {}

    app_name = body.get("app_name") or body.get("slug")
    port = body.get("port")
    repo_url = body.get("repo_url")

    if not app_name or not port:
        return jsonify({"error": "app_name and port are required"}), 400

    log.info("Validate request: app=%s port=%s", app_name, port)

    result = validate_submission(
        app_name=app_name,
        port=int(port),
        repo_url=repo_url,
    )

    log.info("Validate result: %s", result.get("result"))
    return jsonify(result), 200


@app.route("/test", methods=["POST"])
def test():
    """
    Pre-deploy validation. Runs checks without building or deploying.

    Expected JSON:
        {"app_name": "myapp", "port": 3005, "repo_url": "..."}
    """
    body = request.get_json(silent=True) or {}

    app_name = body.get("app_name") or body.get("slug")
    port = body.get("port")
    repo_url = body.get("repo_url")
    local_path = body.get("local_path")

    if not app_name:
        return jsonify({"error": "app_name or slug is required"}), 400
    if not port:
        return jsonify({"error": "port is required"}), 400

    log.info("Test request: app=%s port=%s", app_name, port)

    result = test_app(
        app_name=app_name,
        port=int(port),
        repo_url=repo_url,
        local_path=local_path,
    )

    log.info("Test result: %s", result.get("result"))
    return jsonify(result), 200


@app.route("/undeploy", methods=["POST"])
def undeploy():
    """
    Remove a deployed app.

    Expected JSON:
        {"app_name": "myapp"}
    """
    body = request.get_json(silent=True) or {}

    app_name = body.get("app_name") or body.get("slug")
    dry_run = body.get("dry_run", False)

    if not app_name:
        return jsonify({"error": "app_name or slug is required"}), 400

    log.info("Undeploy request: app=%s dry_run=%s", app_name, dry_run)

    result = undeploy_app(app_name=app_name, dry_run=dry_run)

    log.info("Undeploy result: %s", result.get("status"))

    status_code = 200 if result["status"] in ("removed", "dry_run") else 500
    return jsonify(result), status_code


@app.route("/self-deploy", methods=["POST"])
def self_deploy():
    """
    Pull latest main on the platform repo and rebuild admin-panel.

    Used by server.py's GitHub webhook handler when aihub-admin is pushed.
    Runs in a detached subprocess (start_new_session=True) and returns
    immediately, because the rebuild can take 30–60s and `docker compose
    up --build` would otherwise tie up this request.

    Important: rebuilds ONLY admin-panel (--no-deps) — NOT deploy-service or
    postgres. deploy-service can't safely rebuild itself (it would kill the
    subprocess running the rebuild). If deploy_service.py / Dockerfile.deploy
    changes, manual rebuild on the droplet is required:
        cd /var/www/aihub-admin && docker compose up --build -d deploy-service

    Requires:
    - /var/www/aihub-admin mounted at same path as host (docker-compose.production.yml)
    - docker compose plugin installed (Dockerfile.deploy)
    - /var/run/docker.sock mounted (already there for /deploy)
    """
    repo_path = "/var/www/aihub-admin"
    if not os.path.exists(f"{repo_path}/.git"):
        return jsonify({"error": f"{repo_path}/.git not found — mount missing"}), 500

    log.info("Self-deploy: rebuilding admin-panel from latest main")
    # Project name MUST match the original (`aihub-admin`, from /var/www/aihub-admin
    # which was the cwd for the original `docker compose up`). Without -p, compose
    # derives the name from the cwd and treats existing containers as a different
    # project, hitting a name conflict.
    # The repo is mounted at the same path as on the host (/var/www/aihub-admin) so
    # compose-generated bind mount paths (e.g. /var/www/aihub-admin/permissions.db)
    # resolve correctly on the host daemon.
    # Capture stdout/stderr to a log file so failures aren't silent.
    cmd = (
        f"cd {repo_path} && "
        "git fetch origin && "
        # Use fetch + reset to update tracked files, but preserve untracked
        # directories like apps/briefer/ (a separately cloned repo) and state
        # files (permissions.db, etc.). git reset --hard only touches tracked
        # files; git clean would nuke untracked dirs, so we skip it.
        "git reset --hard origin/main && "
        "cp docker-compose.production.yml docker-compose.yml && "
        # Ensure host-side state files exist as files before compose bind-mounts them.
        # `touch` is a no-op if the file already exists, so existing data is safe.
        "touch permissions.db readonly_db_users.json ip_labels.json ssh_aliases.json && "
        "docker compose -p aihub-admin up --build -d --no-deps admin-panel"
    )
    # Log to /tmp inside the deploy-service container — survives the
    # subprocess being orphaned. Wiped on container restart, which is fine.
    log_path = "/tmp/self-deploy.log"
    log_fh = open(log_path, "ab")
    log_fh.write(f"\n=== {__import__('datetime').datetime.utcnow().isoformat()}Z self-deploy ===\n".encode())
    log_fh.flush()
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return jsonify({"status": "triggered", "pid": proc.pid, "target": "admin-panel", "log": log_path}), 200


@app.route("/kb-deploy", methods=["POST"])
def kb_deploy():
    """
    Pull latest main on the knowledge-base repo and rebuild + restart via pm2.

    Used by server.py's GitHub webhook handler when egelloc-ai-hub is pushed.
    Runs in a detached subprocess (start_new_session=True) and returns
    immediately — `npm run build` takes 1–3 minutes.

    Strategy: `docker run --pid=host --privileged alpine` with nsenter into
    PID 1's mount namespace gives the container full access to the host's
    node, npm, and pm2 binaries. The alpine image only needs util-linux
    (for nsenter) which is ~5 s to install and is cached after the first run.

    Requires:
    - /var/run/docker.sock mounted (already there for /deploy)
    - egelloc-ai-hub repo at /var/www/egelloc-ai-hub on the host
    - pm2 running as root with process name 'ai-hub'
    """
    kb_path = "/var/www/egelloc-ai-hub"
    # Guard: the path is on the HOST, not inside this container, so we
    # can't os.path.exists it. Skip the check and let the build fail if absent.

    log.info("KB deploy: pulling + rebuilding knowledge base")
    # nsenter -t 1 -m enters PID 1 (systemd)'s mount namespace — the full
    # host filesystem including /usr/bin/node, /usr/bin/npm, /usr/bin/pm2.
    inner_cmd = f"cd {kb_path} && git pull && npm run build && pm2 restart ai-hub"
    cmd = (
        "docker run --rm --pid=host --privileged alpine "
        "sh -c 'apk add --no-cache util-linux -q && "
        f"nsenter -t 1 -m -u -i -n -p -- sh -c \"{inner_cmd}\"'"
    )
    log_path = "/tmp/kb-deploy.log"
    log_fh = open(log_path, "ab")
    log_fh.write(f"\n=== {__import__('datetime').datetime.utcnow().isoformat()}Z kb-deploy ===\n".encode())
    log_fh.flush()
    proc = subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return jsonify({"status": "triggered", "pid": proc.pid, "target": "knowledge-base", "log": log_path}), 200


@app.route("/kb-deploy/log", methods=["GET"])
def kb_deploy_log():
    """Return the last N lines of the kb-deploy log for inspection."""
    log_path = "/tmp/kb-deploy.log"
    try:
        with open(log_path) as f:
            lines = f.readlines()
        tail = int(request.args.get("lines", 50))
        return jsonify({"log": "".join(lines[-tail:]), "path": log_path}), 200
    except FileNotFoundError:
        return jsonify({"log": "", "note": "No deploy has run yet"}), 200


# ── Host user management (VPS Users feature) ──
# All four endpoints shell out to the host via the same nsenter pattern
# /kb-deploy uses (see the comment on that route). Admin panel gates the
# mutations behind @admin_required and does input validation; we just
# execute here.

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
PROTECTED_USERNAMES = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "proxy", "www-data", "backup", "list", "irc", "_apt", "nobody",
    "systemd-network", "systemd-resolve", "messagebus", "sshd", "ubuntu",
}
VALID_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256",
                   "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")


def run_on_host(inner_cmd, timeout=30):
    """Execute inner_cmd on the host via the alpine+nsenter pattern.

    Returns (returncode, stdout, stderr). Callers MUST pre-quote any
    interpolated values with shlex.quote — this function does no escaping.
    """
    outer = (
        "apk add --no-cache util-linux -q && "
        f"nsenter -t 1 -m -u -i -n -p -- sh -c {shlex.quote(inner_cmd)}"
    )
    cmd = ["docker", "run", "--rm", "--pid=host", "--privileged",
           "alpine", "sh", "-c", outer]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _validate_username(name):
    if not name or not USERNAME_RE.match(name):
        return "invalid username (must match ^[a-z_][a-z0-9_-]{0,30}$)"
    if name in PROTECTED_USERNAMES:
        return f"'{name}' is a protected system username"
    return None


def _validate_pubkey(key):
    key = (key or "").strip()
    if not key:
        return "public key is required"
    parts = key.split()
    if len(parts) < 2 or parts[0] not in VALID_KEY_TYPES:
        return "unsupported or malformed SSH public key"
    # base64 sanity — don't try to decode, just length and charset
    if not re.match(r"^[A-Za-z0-9+/=]+$", parts[1]) or len(parts[1]) < 40:
        return "malformed SSH public key payload"
    return None


@app.route("/host-users", methods=["GET"])
def host_users_list():
    """List login-capable host users (uid ≥ 1000) with sudo flag + key count."""
    inner = (
        "getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1 \"|\" $3 \"|\" $6}' | "
        "while IFS='|' read name uid home; do "
        "  sudo_flag=no; "
        "  if id -nG \"$name\" 2>/dev/null | grep -qw sudo; then sudo_flag=yes; fi; "
        "  keys=0; "
        "  if [ -f \"$home/.ssh/authorized_keys\" ]; then "
        "    keys=$(grep -cvE '^[[:space:]]*(#|$)' \"$home/.ssh/authorized_keys\" 2>/dev/null || echo 0); "
        "  fi; "
        "  echo \"$name|$uid|$sudo_flag|$keys\"; "
        "done"
    )
    try:
        code, out, err = run_on_host(inner, timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out enumerating host users"}), 504
    if code != 0:
        return jsonify({"error": "host enumeration failed", "stderr": err[-400:]}), 500

    users = []
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        name, uid, sudo_flag, keys = parts
        users.append({
            "name": name,
            "uid": int(uid) if uid.isdigit() else uid,
            "sudo": sudo_flag == "yes",
            "keys": int(keys) if keys.isdigit() else 0,
        })
    return jsonify({"users": sorted(users, key=lambda u: u["name"])})


@app.route("/host-users", methods=["POST"])
def host_users_create():
    """Create a login user, install one authorized_key, optionally add to sudo."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    pubkey = (body.get("pubkey") or "").strip()
    grant_sudo = bool(body.get("sudo", False))

    err = _validate_username(name) or _validate_pubkey(pubkey)
    if err:
        return jsonify({"error": err}), 400

    q_name = shlex.quote(name)
    q_key = shlex.quote(pubkey)
    inner = (
        f"set -e; "
        f"if id {q_name} >/dev/null 2>&1; then echo 'user exists' >&2; exit 2; fi; "
        f"useradd -m -s /bin/bash {q_name}; "
        f"mkdir -p /home/{q_name}/.ssh && chmod 700 /home/{q_name}/.ssh; "
        f"printf '%s\\n' {q_key} > /home/{q_name}/.ssh/authorized_keys; "
        f"chmod 600 /home/{q_name}/.ssh/authorized_keys; "
        f"chown -R {q_name}:{q_name} /home/{q_name}/.ssh; "
    )
    if grant_sudo:
        # SSH-key-only users have no password, so sudo group alone gives them
        # "sudo: a password is required" (useless). Install a sudoers.d entry
        # for NOPASSWD — matches the cloud-init `ubuntu` user pattern.
        # visudo -c validates syntax; if it fails we remove the file and
        # bail so we don't leave sudo in a broken state.
        #
        # name already passed the USERNAME_RE regex, so interpolating it
        # into the path is safe. We still shlex.quote the path before the shell.
        sudoers_path = shlex.quote(f"/etc/sudoers.d/aihub-{name}")
        inner += (
            f"usermod -aG sudo {q_name}; "
            f"printf '%s ALL=(ALL) NOPASSWD:ALL\\n' {q_name} > {sudoers_path}; "
            f"chmod 0440 {sudoers_path}; "
            f"visudo -cf {sudoers_path} >/dev/null || "
            f"{{ rm -f {sudoers_path}; echo 'sudoers validation failed' >&2; exit 5; }}; "
        )
    inner += "echo ok"

    try:
        code, out, err_out = run_on_host(inner, timeout=30)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out creating user"}), 504

    if code == 2:
        return jsonify({"error": f"user '{name}' already exists"}), 409
    if code != 0:
        return jsonify({"error": "user creation failed",
                        "stderr": err_out[-400:]}), 500
    return jsonify({"status": "created", "name": name, "sudo": grant_sudo}), 201


@app.route("/host-users/<name>", methods=["DELETE"])
def host_users_delete(name):
    """Remove a host user and their home directory."""
    err = _validate_username(name)
    if err:
        return jsonify({"error": err}), 400

    q_name = shlex.quote(name)
    # Evict any lingering sessions/processes before userdel — without this,
    # deletes issued seconds after the user's last SSH fail with
    # "user is currently used by process" (logind keeps sessions alive
    # briefly after disconnect). If there's no live session, these are no-ops.
    # No `|| true` on userdel itself — we want failures to surface, not be
    # masked into a 200 OK.
    inner = (
        f"set -e; "
        f"if ! id {q_name} >/dev/null 2>&1; then echo 'no such user' >&2; exit 2; fi; "
        # Belt-and-suspenders: refuse to touch uid < 1000 even if the name
        # passed the PROTECTED list. The validator should have caught it.
        f"uid=$(id -u {q_name}); "
        f"if [ \"$uid\" -lt 1000 ]; then echo 'refuse: system uid' >&2; exit 3; fi; "
        f"loginctl terminate-user {q_name} 2>/dev/null || true; "
        f"pkill -KILL -u {q_name} 2>/dev/null || true; "
        f"sleep 1; "
        f"userdel -r {q_name}; "
        # Remove any sudoers.d entry we installed on create. Safe if absent.
        f"rm -f /etc/sudoers.d/aihub-{name}; "
        f"echo ok"
    )
    try:
        code, out, err_out = run_on_host(inner, timeout=25)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out deleting user"}), 504

    if code == 2:
        return jsonify({"error": f"no such user '{name}'"}), 404
    if code == 3:
        return jsonify({"error": "refused: system uid"}), 403
    if code != 0:
        return jsonify({"error": "user deletion failed",
                        "stderr": err_out[-400:]}), 500
    return jsonify({"status": "deleted", "name": name})


if __name__ == "__main__":
    port = int(os.environ.get("DEPLOY_SERVICE_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
