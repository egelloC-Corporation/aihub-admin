"""
AI Hub Deploy Service

Standalone Flask app (port 5001) that handles deploy/undeploy requests
from the admin panel. Called by server.py when an app submission is approved.

Wraps scripts/deploy.py, scripts/nginx_config.py, and scripts/db_provision.py.
"""

import os
import sys
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
    - /platform-repo mounted (docker-compose.production.yml)
    - docker compose plugin installed (Dockerfile.deploy)
    - /var/run/docker.sock mounted (already there for /deploy)
    """
    if not os.path.exists("/platform-repo/.git"):
        return jsonify({"error": "/platform-repo/.git not found — mount missing"}), 500

    log.info("Self-deploy: rebuilding admin-panel from latest main")
    # Project name MUST match the original (`aihub-admin`, from /var/www/aihub-admin
    # which was the cwd for the original `docker compose up`). Without -p, compose
    # derives the name from the cwd (here /platform-repo → "platform-repo") and
    # treats existing containers as a different project, hitting a name conflict.
    # Capture stdout/stderr to a log file so failures aren't silent like before.
    cmd = (
        "cd /platform-repo && "
        "git fetch origin && "
        "git reset --hard origin/main && "
        "cp docker-compose.production.yml docker-compose.yml && "
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


if __name__ == "__main__":
    port = int(os.environ.get("DEPLOY_SERVICE_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
