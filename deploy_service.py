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
import base64
import hashlib
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
    # Pre-cleanup: compose's own stop/recreate is not atomic. A prior
    # deploy that died mid-recreate can leave a container named
    # "<hash>_aihub-admin-panel" holding the real name slot, which makes
    # the next `compose up` fail with "name already in use" and leave
    # admin-panel in Created (stopped) state — outage. Seen on incubator
    # 2026-04-23. Remove any container whose name matches the admin-panel
    # service before compose tries to recreate.
    #
    # docker ps --filter "name=X" does a substring match, so it catches
    # both the canonical name and any prefixed orphan form. `|| true` is
    # fine here — nothing to clean up is a valid state.
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
        # Ghost-container cleanup (see comment above).
        "docker ps -aq --filter 'name=aihub-admin-panel' | xargs -r docker rm -f >/dev/null 2>&1 || true && "
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
    """List host accounts with SSH access: root + human users (uid ≥ 1000).

    Returns the effective "who can log in" set, not just users created via
    this panel — root and ubuntu/cloud-init accounts are surfaced as
    read-only so the admin sees the full picture. `managed=true` flags
    users this panel can delete (uid ≥ 1000 and not a protected name).
    """
    protected_csv = ",".join(sorted(PROTECTED_USERNAMES))
    inner = (
        f"PROTECTED={shlex.quote(protected_csv)}; "
        "getent passwd | awk -F: '$3 == 0 || ($3 >= 1000 && $3 < 65534) {print $1 \"|\" $3 \"|\" $6}' | "
        "while IFS='|' read name uid home; do "
        "  keys=0; "
        "  if [ -f \"$home/.ssh/authorized_keys\" ]; then "
        "    keys=$(grep -cvE '^[[:space:]]*(#|$)' \"$home/.ssh/authorized_keys\" 2>/dev/null || echo 0); "
        "  fi; "
        # Skip accounts with no SSH access to avoid listing disabled service
        # users that happen to have uid ≥ 1000. Root and anyone with ≥1 key
        # are always surfaced.
        "  if [ \"$uid\" != 0 ] && [ \"$keys\" = 0 ]; then continue; fi; "
        "  sudo_flag=no; "
        "  if [ \"$uid\" = 0 ]; then sudo_flag=yes; "
        "  elif id -nG \"$name\" 2>/dev/null | grep -qw sudo; then sudo_flag=yes; fi; "
        "  managed=yes; "
        "  if [ \"$uid\" -lt 1000 ]; then managed=no; fi; "
        "  case \",$PROTECTED,\" in *,\"$name\",*) managed=no ;; esac; "
        "  echo \"$name|$uid|$sudo_flag|$keys|$managed\"; "
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
        if len(parts) != 5:
            continue
        name, uid, sudo_flag, keys, managed = parts
        users.append({
            "name": name,
            "uid": int(uid) if uid.isdigit() else uid,
            "sudo": sudo_flag == "yes",
            "keys": int(keys) if keys.isdigit() else 0,
            "managed": managed == "yes",
        })
    return jsonify({"users": sorted(users, key=lambda u: u["name"])})


@app.route("/host-users/<name>/keys", methods=["GET"])
def host_user_keys(name):
    """Return the parsed authorized_keys for one host user.

    Each key has type, comment (rightmost "user@host" token from the key
    line), and SHA256 fingerprint — matches `ssh-keygen -lf` output and
    lets the admin identify which physical device each slot belongs to.
    Accepts `root` even though it's in PROTECTED_USERNAMES (the protected
    list gates mutation, not reads).
    """
    if not name or not USERNAME_RE.match(name):
        return jsonify({"error": "invalid username"}), 400
    q_name = shlex.quote(name)
    inner = (
        f"if ! id {q_name} >/dev/null 2>&1; then echo 'no such user' >&2; exit 2; fi; "
        f"home=$(getent passwd {q_name} | cut -d: -f6); "
        f"if [ -f \"$home/.ssh/authorized_keys\" ]; then cat \"$home/.ssh/authorized_keys\"; fi"
    )
    try:
        code, out, err_out = run_on_host(inner, timeout=15)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out reading keys"}), 504
    if code == 2:
        return jsonify({"error": f"no such user '{name}'"}), 404
    if code != 0:
        return jsonify({"error": "key read failed", "stderr": err_out[-400:]}), 500

    keys = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2 or parts[0] not in VALID_KEY_TYPES:
            continue
        fp = ""
        try:
            raw = base64.b64decode(parts[1], validate=False)
            fp = "SHA256:" + base64.b64encode(
                hashlib.sha256(raw).digest()
            ).decode().rstrip("=")
        except Exception:
            pass
        keys.append({
            "type": parts[0],
            "comment": parts[2].strip() if len(parts) >= 3 else "",
            "fingerprint": fp,
        })
    return jsonify({"keys": keys})


# ── Per-key add / remove (extends single-key-at-create into multi-key) ──

_DELETE_KEY_SCRIPT = r"""
import sys, os, base64, hashlib, pwd
name = sys.argv[1]
target_fp = sys.argv[2]
try:
    pw = pwd.getpwnam(name)
except KeyError:
    sys.exit(2)
path = os.path.join(pw.pw_dir, ".ssh", "authorized_keys")
if not os.path.exists(path):
    sys.exit(3)
with open(path) as f:
    lines = f.readlines()
kept, removed = [], 0
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        kept.append(line); continue
    parts = stripped.split(None, 2)
    if len(parts) < 2:
        kept.append(line); continue
    try:
        raw = base64.b64decode(parts[1], validate=False)
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    except Exception:
        kept.append(line); continue
    if fp == target_fp:
        removed += 1
        continue
    kept.append(line)
if removed == 0:
    sys.exit(4)
# Count remaining non-empty, non-comment key lines
key_lines = [l for l in kept if l.strip() and not l.strip().startswith("#")]
if len(key_lines) == 0:
    # Would lock the user out entirely — refuse. Use DELETE /host-users/<name>
    # if the intent is to remove the account.
    sys.exit(5)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    f.writelines(kept)
os.chmod(tmp, 0o600)
os.chown(tmp, pw.pw_uid, pw.pw_gid)
os.replace(tmp, path)
print(f"removed:{removed} remaining:{len(key_lines)}")
"""


_ADD_KEY_SCRIPT = r"""
import sys, os, base64, hashlib, pwd
name = sys.argv[1]
new_line = sys.argv[2]
try:
    pw = pwd.getpwnam(name)
except KeyError:
    sys.exit(2)
# Compute incoming fingerprint
parts = new_line.split(None, 2)
if len(parts) < 2:
    sys.exit(6)
try:
    raw = base64.b64decode(parts[1], validate=False)
    new_fp = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
except Exception:
    sys.exit(6)
ssh_dir = os.path.join(pw.pw_dir, ".ssh")
os.makedirs(ssh_dir, exist_ok=True)
os.chmod(ssh_dir, 0o700)
os.chown(ssh_dir, pw.pw_uid, pw.pw_gid)
path = os.path.join(ssh_dir, "authorized_keys")
# Dup check
if os.path.exists(path):
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"): continue
            p = stripped.split(None, 2)
            if len(p) < 2: continue
            try:
                r = base64.b64decode(p[1], validate=False)
                existing_fp = "SHA256:" + base64.b64encode(hashlib.sha256(r).digest()).decode().rstrip("=")
            except Exception:
                continue
            if existing_fp == new_fp:
                sys.exit(7)
# Ensure existing file ends with a newline before appending, so the
# new key doesn't concatenate onto the last line.
if os.path.exists(path) and os.path.getsize(path) > 0:
    with open(path, "rb") as rf:
        rf.seek(-1, 2)
        last = rf.read(1)
    if last != b"\n":
        with open(path, "a") as f:
            f.write("\n")
with open(path, "a") as f:
    f.write(new_line.rstrip("\n") + "\n")
os.chmod(path, 0o600)
os.chown(path, pw.pw_uid, pw.pw_gid)
print(f"added:{new_fp}")
"""


@app.route("/host-users/<name>/keys", methods=["POST"])
def host_user_add_key(name):
    """Append an additional authorized_key to an existing user."""
    if not USERNAME_RE.match(name):
        return jsonify({"error": "invalid username"}), 400
    body = request.get_json(silent=True) or {}
    pubkey = (body.get("pubkey") or "").strip()
    err = _validate_pubkey(pubkey)
    if err:
        return jsonify({"error": err}), 400

    inner = (
        f"python3 -c {shlex.quote(_ADD_KEY_SCRIPT)} "
        f"{shlex.quote(name)} {shlex.quote(pubkey)}"
    )
    try:
        code, out, err_out = run_on_host(inner, timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out adding key"}), 504
    if code == 2: return jsonify({"error": f"no such user '{name}'"}), 404
    if code == 6: return jsonify({"error": "malformed public key"}), 400
    if code == 7: return jsonify({"error": "this key is already installed"}), 409
    if code != 0:
        return jsonify({"error": "add key failed",
                        "stderr": err_out[-400:]}), 500
    return jsonify({"status": "added", "name": name,
                    "stdout": out.strip()}), 201


@app.route("/host-users/<name>/keys", methods=["DELETE"])
def host_user_delete_key(name):
    """Remove one key from a user's authorized_keys by SHA256 fingerprint."""
    if not USERNAME_RE.match(name):
        return jsonify({"error": "invalid username"}), 400
    body = request.get_json(silent=True) or {}
    fingerprint = (body.get("fingerprint") or "").strip()
    if not re.match(r"^SHA256:[A-Za-z0-9+/=]+$", fingerprint):
        return jsonify({"error": "invalid fingerprint (expect SHA256:…)"}), 400

    inner = (
        f"python3 -c {shlex.quote(_DELETE_KEY_SCRIPT)} "
        f"{shlex.quote(name)} {shlex.quote(fingerprint)}"
    )
    try:
        code, out, err_out = run_on_host(inner, timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "timed out removing key"}), 504
    if code == 2: return jsonify({"error": f"no such user '{name}'"}), 404
    if code == 3: return jsonify({"error": "user has no authorized_keys file"}), 404
    if code == 4: return jsonify({"error": "no key with that fingerprint"}), 404
    if code == 5:
        return jsonify({
            "error": "refused: that's the user's only key. "
                     "Delete the whole account via the Delete button instead, "
                     "or add a replacement key first.",
        }), 409
    if code != 0:
        return jsonify({"error": "key removal failed",
                        "stderr": err_out[-400:]}), 500
    return jsonify({"status": "removed", "name": name,
                    "fingerprint": fingerprint,
                    "stdout": out.strip()})


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


# ── App lifecycle shortcuts (restart) ──
# Redeploy reuses the existing /deploy endpoint — callers POST the same
# body. Restart is just `docker restart <container>`; cheap and adds
# its own endpoint so the admin panel's audit log can distinguish
# "operator bounced the container" from "operator triggered a full
# redeploy with a git pull".

APP_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


@app.route("/apps/<slug>/restart", methods=["POST"])
def app_restart(slug):
    """Restart the container for a live app — `docker restart aihub-<slug>`."""
    if not APP_SLUG_RE.match(slug):
        return jsonify({"error": "invalid slug"}), 400
    container = f"aihub-{slug}"
    try:
        proc = subprocess.run(
            ["docker", "restart", container],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "restart timed out"}), 504
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        status = 404 if "No such container" in stderr else 500
        return jsonify({"error": "restart failed",
                        "stderr": stderr[-400:]}), status
    return jsonify({"status": "restarted", "container": container})


if __name__ == "__main__":
    port = int(os.environ.get("DEPLOY_SERVICE_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
