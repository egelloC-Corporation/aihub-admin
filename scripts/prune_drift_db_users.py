#!/usr/bin/env python3
"""Prune drift entries from readonly_db_users.json.

A "drift" entry is a panel-tracked read-only user that the DO managed-DB API
no longer lists — usually because the user was dropped outside the panel, or
because the cluster was rebuilt. Since DROP USER IF EXISTS no-ops cleanly on
the live DB, the only thing to clean up is the JSON ledger.

Run from the repo root on the server (env vars must be available):
  python3 scripts/prune_drift_db_users.py --dry-run
  python3 scripts/prune_drift_db_users.py            # actually writes
"""
import argparse
import json
import os
import sys

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO_ROOT, "readonly_db_users.json")

# Same set as server.py's _DB_SYSTEM_USERS — we never count these as drift
# even if they show up in the ledger.
SYSTEM_USERS = {
    "doadmin", "_dodb", "postgres", "rdsadmin", "replication", "repmgr",
    "mysql.sys", "mysql.session", "mysql.infoschema", "mariadb.sys",
    "rdsrepladmin", "root",
}

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


def live_users(slug):
    cfg = DO_CONFIGS.get(slug, {})
    if not cfg.get("token") or not cfg.get("cluster_id"):
        print(f"  [{slug}] DO API not configured — skipping (would treat all as drift, refusing)")
        return None
    r = requests.get(
        f"https://api.digitalocean.com/v2/databases/{cfg['cluster_id']}/users",
        headers={"Authorization": f"Bearer {cfg['token']}"},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  [{slug}] DO API {r.status_code}: {r.text[:200]}")
        return None
    names = {u.get("name", "") for u in r.json().get("users", []) if u.get("name")}
    return names - SYSTEM_USERS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would be removed without writing")
    args = ap.parse_args()

    with open(LEDGER) as f:
        ledger = json.load(f)

    # Group ledger by slug, look up live users per slug once
    by_slug = {}
    for entry in ledger:
        by_slug.setdefault(entry.get("db", "nest"), []).append(entry)

    keep = []
    drift = []
    for slug, entries in by_slug.items():
        live = live_users(slug)
        if live is None:
            # Couldn't get a definitive answer — keep everything, don't risk
            # wiping the ledger on a transient DO API hiccup.
            print(f"  [{slug}] no live state available, keeping all {len(entries)} entries")
            keep.extend(entries)
            continue
        for e in entries:
            if e["username"] in live:
                keep.append(e)
            else:
                drift.append(e)

    print(f"\nTracked: {len(ledger)}  Keep: {len(keep)}  Drift: {len(drift)}")
    for e in drift:
        print(f"  drift: {e.get('db', 'nest')}/{e['username']}  (created_by={e.get('created_by', '?')}, at={e.get('created_at', '?')})")

    if not drift:
        print("Nothing to prune.")
        return 0

    if args.dry_run:
        print("\n--dry-run: not writing.")
        return 0

    with open(LEDGER, "w") as f:
        json.dump(keep, f, indent=2)
    print(f"\nWrote {LEDGER} ({len(keep)} entries).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
