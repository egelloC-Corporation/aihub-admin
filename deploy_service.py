"""
AI Hub Deploy Service

Standalone Flask app (port 5001) that handles deploy/undeploy requests
from the admin panel. Called by server.py when an app submission is approved.

Wraps scripts/deploy.py, scripts/nginx_config.py, and scripts/db_provision.py.
"""

import os
import sys
import logging

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


if __name__ == "__main__":
    port = int(os.environ.get("DEPLOY_SERVICE_PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
