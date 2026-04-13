#!/usr/bin/env python3
"""
Deploy script for AI Hub apps.

Handles the full deploy lifecycle:
  1. Clone repo (or copy local path) into apps/<app_name>/
  2. Build Docker image
  3. Start container on the aihub network
  4. Generate Nginx route
  5. Provision scoped DB user
  6. Health check

Usage:
    python scripts/deploy.py deploy --app-name myapp --port 3005 --repo-url https://github.com/org/myapp.git
    python scripts/deploy.py deploy --app-name myapp --port 3005 --local-path ./starter-template --dry-run
    python scripts/deploy.py undeploy --app-name myapp
    python scripts/deploy.py undeploy --app-name myapp --dry-run
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

# Resolve paths relative to project root
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPS_DIR = os.environ.get("APPS_DIR", os.path.join(PROJECT_ROOT, "apps"))
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "aihub")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Import sibling scripts
sys.path.insert(0, os.path.dirname(__file__))
from nginx_config import add_app as nginx_add, remove_app as nginx_remove
from db_provision import create_app_user, drop_app_user


def _inject_github_token(url):
    """Inject GitHub token into HTTPS clone URLs. Never logs the token."""
    if not GITHUB_TOKEN:
        return url
    if url and "github.com" in url and url.startswith("https://"):
        return url.replace("https://github.com/", f"https://{GITHUB_TOKEN}@github.com/")
    return url


def _safe_url(url):
    """Strip credentials from a URL for logging."""
    if not url:
        return url
    import re
    return re.sub(r'https://[^@]+@', 'https://', url)


def _run(cmd, dry_run=False, check=True):
    """Run a shell command. In dry-run mode, just print it."""
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
    if dry_run:
        return f"Would run: {_safe_url(cmd_str)}"

    result = subprocess.run(
        cmd, capture_output=True, text=True,
        shell=isinstance(cmd, str),
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {_safe_url(cmd_str)}\n{result.stderr}")
    return result.stdout.strip()


def _clone_or_copy(app_name, repo_url=None, local_path=None, repo_subdir=None, dry_run=False):
    """Clone a repo or copy a local directory into apps/<app_name>/.
    If repo_subdir is set, only that subdirectory is used as the app root."""
    dest = os.path.join(APPS_DIR, app_name)

    if os.path.exists(dest) and not dry_run:
        shutil.rmtree(dest)

    if repo_url:
        auth_url = _inject_github_token(repo_url)
        if repo_subdir:
            # Clone to a temp dir, then move the subdirectory
            tmp_dest = dest + "_tmp"
            if os.path.exists(tmp_dest) and not dry_run:
                shutil.rmtree(tmp_dest)
            msg = _run(["git", "clone", "--depth", "1", auth_url, tmp_dest], dry_run=dry_run)
            if not dry_run:
                subdir_path = os.path.join(tmp_dest, repo_subdir)
                if not os.path.isdir(subdir_path):
                    shutil.rmtree(tmp_dest)
                    raise RuntimeError(f"Subdirectory '{repo_subdir}' not found in repo")
                shutil.copytree(subdir_path, dest, dirs_exist_ok=True)
                shutil.rmtree(tmp_dest)
            return msg or f"Cloned {_safe_url(repo_url)} (subdir: {repo_subdir}) → {dest}"
        else:
            msg = _run(["git", "clone", "--depth", "1", auth_url, dest], dry_run=dry_run)
            return msg or f"Cloned {_safe_url(repo_url)} → {dest}"
    elif local_path:
        abs_path = os.path.abspath(local_path)
        if dry_run:
            return f"Would copy {abs_path} → {dest}"
        shutil.copytree(abs_path, dest, dirs_exist_ok=True)
        return f"Copied {abs_path} → {dest}"
    else:
        raise ValueError("Either repo_url or local_path is required")


DOCKERFILE_NODE = """\
FROM node:20-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY . .
EXPOSE {port}
CMD ["node", "{entry}"]
"""

DOCKERFILE_PYTHON = """\
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
CMD ["python", "{entry}"]
"""


def _generate_dockerfile(build_dir, port=3000):
    """Auto-generate a Dockerfile based on project files. Returns a description of what was generated."""
    has_package_json = os.path.exists(os.path.join(build_dir, "package.json"))
    has_requirements = os.path.exists(os.path.join(build_dir, "requirements.txt"))

    if has_package_json:
        # Node.js — detect entry point
        entry = "server.js"
        for candidate in ["server.js", "index.js", "app.js", "src/index.js"]:
            if os.path.exists(os.path.join(build_dir, candidate)):
                entry = candidate
                break
        content = DOCKERFILE_NODE.format(port=port, entry=entry)
        desc = f"Generated Dockerfile (Node.js, entry: {entry})"
    elif has_requirements:
        # Python — detect entry point
        entry = "server.py"
        for candidate in ["server.py", "app.py", "main.py", "wsgi.py"]:
            if os.path.exists(os.path.join(build_dir, candidate)):
                entry = candidate
                break
        content = DOCKERFILE_PYTHON.format(port=port, entry=entry)
        desc = f"Generated Dockerfile (Python, entry: {entry})"
    else:
        return None

    with open(os.path.join(build_dir, "Dockerfile"), "w") as f:
        f.write(content)
    return desc


def _build_image(app_name, port=3000, dry_run=False):
    """Build Docker image for the app. Auto-generates Dockerfile if missing."""
    build_dir = os.path.join(APPS_DIR, app_name)
    image_name = f"aihub-{app_name}"

    # Auto-generate Dockerfile if missing
    if not dry_run and not os.path.exists(os.path.join(build_dir, "Dockerfile")):
        result = _generate_dockerfile(build_dir, port=port)
        if not result:
            raise RuntimeError(f"No Dockerfile found in {build_dir} and could not auto-generate one (no package.json or requirements.txt)")
        print(f"  {result}")

    msg = _run(["docker", "build", "-t", image_name, build_dir], dry_run=dry_run)
    return msg or f"Built image {image_name}"


def _start_container(app_name, port, streamlit_port=None, dry_run=False):
    """Start the app container on the aihub network.

    streamlit_port: optional second host port to publish (e.g. 8501 for Streamlit
    apps whose WebSocket runs on a separate internal port from Flask). Published
    symmetrically as -p {streamlit_port}:{streamlit_port}, so the container's
    Streamlit process must bind to that same port internally.
    """
    container_name = f"aihub-{app_name}"
    image_name = f"aihub-{app_name}"

    # Stop and remove if already running
    if not dry_run:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, text=True,
        )

    # Auth URL so deployed apps can verify sessions against the admin panel
    auth_url = os.environ.get("AIHUB_AUTH_URL", "http://admin-panel:5051/auth/me")

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", DOCKER_NETWORK,
        "-p", f"{port}:{port}",
    ]
    if streamlit_port:
        cmd += ["-p", f"{streamlit_port}:{streamlit_port}"]
    cmd += [
        "-e", f"PORT={port}",
        "-e", f"APP_SLUG={app_name}",
        "-e", f"AIHUB_AUTH_URL={auth_url}",
        "-e", f"AIHUB_LOGIN_URL=/login?next=/{app_name}/",
        "-e", f"HOST=0.0.0.0",
        "--memory", "512m",
        "--cpus", "0.5",
        "--restart", "unless-stopped",
        image_name,
    ]

    # Pass platform .env (shared secrets like OAuth, API keys)
    platform_env = os.environ.get("PLATFORM_ENV_FILE", "/app/platform.env")
    if os.path.exists(platform_env):
        cmd.insert(-1, "--env-file")
        cmd.insert(-1, platform_env)

    # Pass app-specific .env (DB credentials from provisioning)
    env_path = os.path.join(APPS_DIR, app_name, ".env")
    if os.path.exists(env_path):
        cmd.insert(-1, "--env-file")
        cmd.insert(-1, env_path)

    msg = _run(cmd, dry_run=dry_run)
    return msg or f"Started container {container_name} on port {port}"


def _health_check(app_name, port, retries=3, dry_run=False):
    """Check if the app is responding. Tries Docker network name first, then localhost."""
    container_name = f"aihub-{app_name}"
    urls = [f"http://{container_name}:{port}/health", f"http://localhost:{port}/health"]

    if dry_run:
        return True, f"Would health check {urls[0]}"

    for attempt in range(1, retries + 1):
        for url in urls:
            try:
                import urllib.request
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return True, f"Health check passed (attempt {attempt}, {url})"
            except Exception:
                continue
        if attempt < retries:
            time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, 8s

    return False, f"Health check failed after {retries} retries at {urls[0]}"


def validate_submission(app_name, port, repo_url=None):
    """
    Submission-time validation. Checks things the submitter can fix BEFORE an admin sees it.
    Called when a user submits or updates an app for review.
    """
    import re
    checks = []

    # 1. App name format
    if re.match(r'^[a-z][a-z0-9-]*$', app_name):
        checks.append({"check": "App name format", "status": "pass"})
    else:
        checks.append({"check": "App name format", "status": "fail",
                        "detail": "Must be lowercase, start with a letter, only a-z, 0-9, hyphens"})

    # 2. Port range
    if 1024 < port < 65536:
        checks.append({"check": "Port range", "status": "pass", "detail": str(port)})
    else:
        checks.append({"check": "Port range", "status": "fail",
                        "detail": f"Port {port} must be between 1025-65535"})

    # 3. Port conflict with another app (not counting self — allows updates)
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True, text=True,
        )
        port_str = f":{port}->"
        for line in result.stdout.strip().splitlines():
            if port_str in line and f"aihub-{app_name}" not in line:
                container = line.split("\t")[0]
                checks.append({"check": "Port not in use", "status": "fail",
                                "detail": f"Port {port} already used by '{container}'"})
                break
        else:
            checks.append({"check": "Port not in use", "status": "pass"})
    except Exception:
        # Docker not available — skip this check at submission time
        pass

    # 4. Repo accessible (the big one — catches private repo issues immediately)
    if repo_url:
        try:
            auth_url = _inject_github_token(repo_url)
            result = subprocess.run(
                ["git", "ls-remote", "--exit-code", auth_url],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                checks.append({"check": "Repo accessible", "status": "pass", "detail": _safe_url(repo_url)})
            else:
                detail = f"Cannot access {_safe_url(repo_url)}"
                if "github.com" in repo_url and not GITHUB_TOKEN:
                    detail += " — if private, ask an admin to configure the GitHub token"
                checks.append({"check": "Repo accessible", "status": "fail", "detail": detail})
        except subprocess.TimeoutExpired:
            checks.append({"check": "Repo accessible", "status": "fail", "detail": "Connection timed out"})
        except Exception as e:
            checks.append({"check": "Repo accessible", "status": "warn", "detail": str(e)})

    # Summarize
    failed = [c for c in checks if c["status"] == "fail"]
    passed = [c for c in checks if c["status"] == "pass"]

    return {
        "status": "validation",
        "app_name": app_name,
        "result": "fail" if failed else "pass",
        "checks": checks,
        "summary": f"{len(passed)} passed, {len(failed)} failed",
    }


def test_app(app_name, port, repo_url=None, local_path=None):
    """
    Full pre-deploy check for admins. Runs submission checks + infra readiness.
    Understands updates (existing container is expected, not a warning).
    """
    import re
    checks = []
    warnings = []
    is_update = False

    # Detect if this is an update (container already running for this app)
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name=aihub-{app_name}", "--format", "{{.Names}}"],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            is_update = True
            checks.append({"check": "Existing app detected", "status": "pass",
                            "detail": f"This is an update — current container will be replaced"})
    except Exception:
        pass

    # Run submission-level checks first
    sub_result = validate_submission(app_name, port, repo_url=repo_url)
    checks.extend(sub_result["checks"])

    # Additional source checks
    if local_path:
        if os.path.isdir(local_path):
            checks.append({"check": "Local path exists", "status": "pass", "detail": local_path})
            if os.path.exists(os.path.join(local_path, "Dockerfile")):
                checks.append({"check": "Dockerfile found", "status": "pass"})
            else:
                # Check if we can auto-generate
                has_pkg = os.path.exists(os.path.join(local_path, "package.json"))
                has_req = os.path.exists(os.path.join(local_path, "requirements.txt"))
                if has_pkg or has_req:
                    lang = "Node.js" if has_pkg else "Python"
                    checks.append({"check": "Dockerfile", "status": "pass",
                                    "detail": f"Will be auto-generated ({lang})"})
                else:
                    checks.append({"check": "Dockerfile found", "status": "fail",
                                    "detail": f"No Dockerfile, package.json, or requirements.txt in {local_path}"})
        else:
            checks.append({"check": "Local path exists", "status": "fail",
                            "detail": f"{local_path} not found"})

    # Infra readiness checks
    # Docker network
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", DOCKER_NETWORK],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            checks.append({"check": "Docker network", "status": "pass", "detail": DOCKER_NETWORK})
        else:
            checks.append({"check": "Docker network", "status": "fail",
                            "detail": f"Network '{DOCKER_NETWORK}' not found — is Docker Compose running?"})
    except Exception:
        checks.append({"check": "Docker network", "status": "warn", "detail": "Could not check"})

    # Nginx config
    from nginx_config import _config_path
    if os.path.exists(_config_path(app_name)):
        if is_update:
            checks.append({"check": "Nginx route", "status": "pass", "detail": "Existing route will be updated"})
        else:
            warnings.append(f"Nginx config for '{app_name}' already exists — will be overwritten")
    else:
        checks.append({"check": "Nginx route available", "status": "pass"})

    # Postgres connectivity + DB user
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "aihub"),
            user=os.environ.get("POSTGRES_USER", "aihub_admin"),
            password=os.environ.get("POSTGRES_PASSWORD", "aihub_local_dev"),
            connect_timeout=5,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (f"app_{app_name}",))
        if cursor.fetchone():
            if is_update:
                checks.append({"check": "DB user", "status": "pass", "detail": f"app_{app_name} exists (reused for update)"})
            else:
                warnings.append(f"DB user 'app_{app_name}' already exists — will be skipped")
        else:
            checks.append({"check": "DB user available", "status": "pass"})
        cursor.close()
        conn.close()
        checks.append({"check": "Postgres connection", "status": "pass"})
    except Exception as e:
        checks.append({"check": "Postgres connection", "status": "warn",
                        "detail": f"Cannot connect: {e}"})

    # Summarize
    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    passed = [c for c in checks if c["status"] == "pass"]

    if failed:
        overall = "fail"
    elif warned or warnings:
        overall = "warn"
    else:
        overall = "pass"

    return {
        "status": "test",
        "app_name": app_name,
        "is_update": is_update,
        "result": overall,
        "checks": checks,
        "warnings": warnings,
        "summary": f"{len(passed)} passed, {len(failed)} failed, {len(warned)} warnings",
    }


def deploy_app(app_name, port, repo_url=None, local_path=None, repo_subdir=None, streamlit_port=None, dry_run=False):
    """
    Full deploy pipeline. Returns a result dict.

    Steps: clone → build → DB provision → start → nginx → health check

    streamlit_port: optional second host port for Streamlit apps (see _start_container).
    """
    steps = []
    try:
        # 1. Clone / copy source
        msg = _clone_or_copy(app_name, repo_url=repo_url, local_path=local_path, repo_subdir=repo_subdir, dry_run=dry_run)
        steps.append(msg)

        # 2. Build Docker image
        msg = _build_image(app_name, port=port, dry_run=dry_run)
        steps.append(msg)

        # 3. Provision DB user
        db_result = create_app_user(app_name, dry_run=dry_run)
        if "error" in db_result:
            steps.append(f"DB provisioning failed: {db_result['error']}")
            # Non-fatal — app might not need a database
        else:
            steps.append(f"DB user: {db_result.get('db_user', 'n/a')}")

        # 4. Start container
        msg = _start_container(app_name, port, streamlit_port=streamlit_port, dry_run=dry_run)
        steps.append(msg)

        # 5. Add Nginx route
        ok, msg = nginx_add(app_name, port, dry_run=dry_run)
        steps.append(msg)

        # 6. Health check
        healthy, msg = _health_check(app_name, port, dry_run=dry_run)
        steps.append(msg)

        if dry_run:
            return {
                "status": "dry_run",
                "app_name": app_name,
                "steps": steps,
            }

        return {
            "status": "deployed",
            "app_name": app_name,
            "url": f"/{app_name}/",
            "port": port,
            "db_user": db_result.get("db_user", ""),
            "healthy": healthy,
            "steps": steps,
        }

    except Exception as e:
        # Determine which step failed based on steps completed
        step_names = ["clone", "build", "db_provision", "start", "nginx", "health_check"]
        failed_step = step_names[len(steps)] if len(steps) < len(step_names) else "unknown"

        return {
            "status": "failed",
            "app_name": app_name,
            "step": failed_step,
            "error": str(e),
            "steps": steps,
        }


def undeploy_app(app_name, dry_run=False):
    """
    Reverse a deploy: stop container, remove Nginx route, drop DB user.
    """
    steps = []
    container_name = f"aihub-{app_name}"

    try:
        # 1. Stop and remove container
        msg = _run(["docker", "rm", "-f", container_name], dry_run=dry_run, check=False)
        steps.append(msg or f"Removed container {container_name}")

        # 2. Remove Docker image
        image_name = f"aihub-{app_name}"
        msg = _run(["docker", "rmi", image_name], dry_run=dry_run, check=False)
        steps.append(msg or f"Removed image {image_name}")

        # 3. Remove Nginx route
        ok, msg = nginx_remove(app_name, dry_run=dry_run)
        steps.append(msg)

        # 4. Drop DB user
        db_result = drop_app_user(app_name, dry_run=dry_run)
        if "error" in db_result:
            steps.append(f"DB cleanup note: {db_result['error']}")
        else:
            steps.append(f"Dropped DB user app_{app_name}")

        if dry_run:
            return {
                "status": "dry_run",
                "app_name": app_name,
                "steps": steps,
            }

        return {
            "status": "removed",
            "app_name": app_name,
            "steps": steps,
        }

    except Exception as e:
        return {
            "status": "failed",
            "app_name": app_name,
            "error": str(e),
            "steps": steps,
        }


def main():
    parser = argparse.ArgumentParser(description="Deploy and manage AI Hub apps")
    sub = parser.add_subparsers(dest="command", required=True)

    dep = sub.add_parser("deploy", help="Deploy an app")
    dep.add_argument("--app-name", required=True, help="App slug")
    dep.add_argument("--port", type=int, required=True, help="Port the app listens on")
    dep.add_argument("--streamlit-port", type=int, default=None, help="Optional second port for Streamlit WebSocket")
    src = dep.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo-url", help="Git repository URL to clone")
    src.add_argument("--local-path", help="Local directory path to copy")
    dep.add_argument("--dry-run", action="store_true", help="Print steps without executing")

    undep = sub.add_parser("undeploy", help="Remove a deployed app")
    undep.add_argument("--app-name", required=True, help="App slug")
    undep.add_argument("--dry-run", action="store_true", help="Print steps without executing")

    args = parser.parse_args()

    if args.command == "deploy":
        result = deploy_app(
            app_name=args.app_name,
            port=args.port,
            repo_url=getattr(args, "repo_url", None),
            local_path=getattr(args, "local_path", None),
            streamlit_port=getattr(args, "streamlit_port", None),
            dry_run=args.dry_run,
        )
    elif args.command == "undeploy":
        result = undeploy_app(app_name=args.app_name, dry_run=args.dry_run)

    # Print result
    print(f"\nResult: {result['status']}")
    if "steps" in result:
        for step in result["steps"]:
            print(f"  • {step}")
    if "error" in result:
        print(f"  Error: {result['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
