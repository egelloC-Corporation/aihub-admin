#!/usr/bin/env python3
"""
Bulk-update GitHub webhook URLs for the domain rename:
  https://aihub.egelloc.com/webhook/github  →  https://incubator.egelloc.com/webhook/github

Reads repos from apps/notes/apps_data.json (plus the admin repo itself).
For each repo, finds any webhook whose config.url matches the old host and
PATCHes only the URL field via the /hooks/{id}/config sub-endpoint — this
preserves the secret, content_type, and SSL settings.

Requires a fine-grained PAT with Repository permission "Webhooks: Read and write"
on all target repos, exposed via the GH_PAT env var.

Usage:
  export GH_PAT="github_pat_..."
  python scripts/update_github_webhooks.py            # dry run (default)
  python scripts/update_github_webhooks.py --apply    # actually update

The dry run lists every hook it would change without calling PATCH. Review
before running --apply.
"""

import argparse
import json
import os
import subprocess
import sys
from urllib.parse import urlparse
import urllib.request
import urllib.error

OLD_HOST = "aihub.egelloc.com"
NEW_HOST = "incubator.egelloc.com"
WEBHOOK_PATH = "/webhook/github"

HERE = os.path.dirname(os.path.abspath(__file__))
APPS_JSON = os.path.join(HERE, "..", "apps", "notes", "apps_data.json")
PROD_HOST = "root@165.232.155.132"


def _api(method, path, token, body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _repo_slug_from_url(github_url):
    """https://github.com/owner/repo → (owner, repo)"""
    p = urlparse(github_url)
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1].removesuffix(".git")


def _load_repos_from_prod():
    """Query the production admin-panel DB for live apps' repo URLs.
    Authoritative — apps_data.json (the fallback) can drift."""
    cmd = [
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", PROD_HOST,
        'docker exec aihub-admin-panel python3 -c '
        '"import sqlite3, json; '
        'c=sqlite3.connect(\\"/app/permissions.db\\"); '
        "rows=c.execute(\\\"SELECT repo_url FROM app_submissions WHERE status='live' AND repo_url!=''\\\").fetchall(); "
        'print(json.dumps([r[0] for r in rows]))"',
    ]
    out = subprocess.check_output(cmd, text=True, timeout=15)
    urls = json.loads(out.strip())
    pairs = set()
    for u in urls:
        owner, repo = _repo_slug_from_url(u)
        if owner and repo:
            pairs.add((owner, repo))
    return sorted(pairs)


def _load_repos_from_json():
    """Fallback: read apps_data.json if SSH to prod is unavailable."""
    with open(APPS_JSON) as f:
        apps = json.load(f)
    pairs = set()
    for a in apps:
        owner, repo = _repo_slug_from_url(a.get("github") or "")
        if owner and repo:
            pairs.add((owner, repo))
    pairs.add(("egelloC-Corporation", "aihub-admin"))
    return sorted(pairs)


def _load_repos(source):
    if source == "prod":
        return _load_repos_from_prod()
    if source == "json":
        return _load_repos_from_json()
    # auto: try prod first, fall back to json
    try:
        return _load_repos_from_prod()
    except Exception as e:
        print(f"  (prod DB unavailable: {e}; falling back to apps_data.json)")
        return _load_repos_from_json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually PATCH the webhooks (default: dry run)")
    parser.add_argument("--source", choices=["auto", "prod", "json"], default="auto",
                        help="Where to get the repo list from (default: auto)")
    args = parser.parse_args()

    token = os.environ.get("GH_PAT")
    if not token:
        print("ERROR: GH_PAT env var not set.", file=sys.stderr)
        print("  export GH_PAT='github_pat_...'", file=sys.stderr)
        sys.exit(2)

    repos = _load_repos(args.source)
    print(f"Scanning {len(repos)} repos for webhooks pointing at {OLD_HOST}{WEBHOOK_PATH}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print()

    updates = []  # (owner, repo, hook_id, old_url, new_url)
    errors = []

    for owner, repo in repos:
        code, hooks = _api("GET", f"/repos/{owner}/{repo}/hooks", token)
        if code != 200:
            msg = hooks.get("message") if isinstance(hooks, dict) else str(hooks)
            errors.append(f"{owner}/{repo}: list hooks → {code} {msg}")
            print(f"  [skip] {owner}/{repo}: {code} {msg}")
            continue

        for h in hooks or []:
            cfg = h.get("config") or {}
            url = cfg.get("url") or ""
            if OLD_HOST not in url or WEBHOOK_PATH not in url:
                continue
            new_url = url.replace(OLD_HOST, NEW_HOST)
            updates.append((owner, repo, h["id"], url, new_url))
            print(f"  {owner}/{repo}  hook {h['id']}")
            print(f"    old: {url}")
            print(f"    new: {new_url}")

    print()
    if not updates:
        print("Nothing to update.")
        for e in errors:
            print(f"  error: {e}")
        return

    if not args.apply:
        print(f"{len(updates)} webhook(s) would be updated. Re-run with --apply to execute.")
        for e in errors:
            print(f"  error: {e}")
        return

    # Apply.
    print(f"Updating {len(updates)} webhook(s)...")
    ok_count = 0
    patched = []  # (owner, repo, hook_id) for successful PATCHes — we'll ping these
    for owner, repo, hook_id, _old, new_url in updates:
        code, resp = _api(
            "PATCH",
            f"/repos/{owner}/{repo}/hooks/{hook_id}/config",
            token,
            body={"url": new_url},
        )
        if 200 <= code < 300:
            ok_count += 1
            patched.append((owner, repo, hook_id))
            print(f"  [ok] {owner}/{repo} hook {hook_id}")
        else:
            msg = resp.get("message") if isinstance(resp, dict) else str(resp)
            errors.append(f"{owner}/{repo} hook {hook_id}: PATCH → {code} {msg}")
            print(f"  [fail] {owner}/{repo} hook {hook_id}: {code} {msg}")

    # Ping each updated webhook to confirm it reaches the new URL.
    # GitHub accepts POST /hooks/{id}/pings and returns 204 immediately, then
    # fires an async `ping` event. Our webhook handler records the ping in
    # webhook_seen, which powers the registry's "Auto-deploy active" indicator.
    if patched:
        print()
        print(f"Pinging {len(patched)} updated webhook(s) to verify delivery...")
        for owner, repo, hook_id in patched:
            code, resp = _api(
                "POST",
                f"/repos/{owner}/{repo}/hooks/{hook_id}/pings",
                token,
            )
            if 200 <= code < 300:
                print(f"  [ping] {owner}/{repo} hook {hook_id} → {code}")
            else:
                msg = resp.get("message") if isinstance(resp, dict) else str(resp)
                print(f"  [ping-fail] {owner}/{repo} hook {hook_id}: {code} {msg}")
                errors.append(f"{owner}/{repo} hook {hook_id}: ping → {code} {msg}")

    print()
    print(f"Updated {ok_count}/{len(updates)}")
    if patched:
        print(f"Check the registry UI — every app should show 'Auto-deploy active' within a few seconds.")
    for e in errors:
        print(f"  error: {e}")
    sys.exit(0 if ok_count == len(updates) and not errors else 1)


if __name__ == "__main__":
    main()
