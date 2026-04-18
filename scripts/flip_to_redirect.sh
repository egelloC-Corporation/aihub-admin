#!/usr/bin/env bash
# Phase 3 of the domain rename: flip aihub.egelloc.com from dual-serve to a
# 301 redirect pointing at incubator.egelloc.com.
#
# Prereqs (all completed by scripts/migrate_domain.sh in phase 1):
#   - Cert at /etc/letsencrypt/live/aihub.egelloc.com/ covers both SANs
#   - Main server block has `server_name aihub incubator`
#   - Port-80 block already covers both hostnames and redirects to HTTPS
#
# After this script runs:
#   - https://aihub.egelloc.com/anything  → 301 → https://incubator.egelloc.com/anything
#   - http://aihub.egelloc.com/anything   → 301 → https://incubator.egelloc.com/anything
#   - http://incubator.egelloc.com/...    → 301 → https://incubator.egelloc.com/...
#   - https://incubator.egelloc.com/...   → serves content
#
# Run BEFORE this script: scripts/update_github_webhooks.py --apply.
# GitHub webhook deliveries don't follow redirects — if any webhook still
# points at aihub.egelloc.com when this flips, auto-deploy breaks silently.
#
# Idempotent. Backs up the config with a timestamp before editing.

set -euo pipefail

OLD_HOST="aihub.egelloc.com"
NEW_HOST="incubator.egelloc.com"
CONF="/etc/nginx/sites-available/ai-hub"
BACKUP="/etc/nginx/sites-available/ai-hub.backup.$(date +%Y%m%d-%H%M%S)"

log() { printf "\033[1;34m[flip]\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m[ok]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[err]\033[0m %s\n" "$*" >&2; }

[[ $EUID -eq 0 ]] || { err "must run as root"; exit 1; }
[[ -f "$CONF" ]] || { err "config not found: $CONF"; exit 1; }

# Sanity check — is webhook migration done?
if command -v curl >/dev/null; then
    log "sanity check: verifying aihub currently serves (not already redirecting)..."
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 \
        --resolve "${OLD_HOST}:443:127.0.0.1" "https://${OLD_HOST}/" || echo "ERR")
    if [[ "$code" == "301" ]] || [[ "$code" == "302" ]]; then
        ok "aihub is already redirecting (HTTP $code) — nothing to do"
        exit 0
    fi
fi

cp -a "$CONF" "$BACKUP"
ok "backed up to $BACKUP"

OLD_HOST="$OLD_HOST" NEW_HOST="$NEW_HOST" CONF="$CONF" python3 <<'PY'
import os, re, pathlib, sys
OLD = os.environ["OLD_HOST"]; NEW = os.environ["NEW_HOST"]; CONF = os.environ["CONF"]
p = pathlib.Path(CONF)
src = p.read_text()
original = src

# Parse top-level `server { ... }` blocks. The config only has a few blocks
# and no nested braces beyond `location { ... }` inside server, so a simple
# balanced-brace scan is sufficient.
def parse_server_blocks(text):
    """Yield (start, end, body) for each top-level server{} block."""
    i = 0
    while True:
        m = re.search(r"server\s*\{", text[i:])
        if not m:
            return
        start = i + m.start()
        # Walk braces from the opening { to find the matching close.
        depth = 0
        j = i + m.end() - 1  # position of the opening '{'
        while j < len(text):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    yield start, end, text[start:end]
                    i = end
                    break
            j += 1
        else:
            return  # unmatched — bail

blocks = list(parse_server_blocks(src))

# Step A: In any 443 block with BOTH hostnames in server_name, remove OLD.
#         Leave port-80 blocks alone.
new_src_parts = []
last_end = 0
for start, end, body in blocks:
    new_src_parts.append(src[last_end:start])
    is_port80 = bool(re.search(r"\blisten\s+80\b", body))
    is_port443 = bool(re.search(r"\blisten\s+443\b", body))
    new_body = body
    if is_port443 and not is_port80:
        # Shrink server_name to only NEW if it has both
        def _shrink(m):
            line = m.group(0)
            if OLD in line and NEW in line:
                inner = re.sub(rf"\s*\b{re.escape(OLD)}\b\s*", " ", line)
                inner = re.sub(r"\s+", " ", inner).replace(" ;", ";")
                return inner
            return line
        new_body = re.sub(r"server_name\s+[^;]+;", _shrink, new_body)
    new_src_parts.append(new_body)
    last_end = end
new_src_parts.append(src[last_end:])
src = "".join(new_src_parts)

# Step B: Ensure a 443 redirect block for OLD exists. Detect it by "server_name
# <OLD>;" combined with "return 301 https://<NEW>" inside the same block.
blocks_after_a = list(parse_server_blocks(src))
has_443_redirect_for_old = any(
    re.search(r"\blisten\s+443\b", b)
    and re.search(rf"server_name\s+{re.escape(OLD)}\s*;", b)
    and re.search(rf"return\s+301\s+https://{re.escape(NEW)}", b)
    for _s, _e, b in blocks_after_a
)
if not has_443_redirect_for_old:
    port80_re = re.compile(r"(?=server\s*\{[^}]*listen\s+80\b)", re.DOTALL)
    new_block = (
        "\nserver {\n"
        "    listen 443 ssl;\n"
        f"    server_name {OLD};\n"
        f"    ssl_certificate /etc/letsencrypt/live/{OLD}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{OLD}/privkey.pem;\n"
        "    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
        "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n"
        f"    return 301 https://{NEW}$request_uri;\n"
        "}\n\n"
    )
    src, n = port80_re.subn(new_block, src, count=1)
    if n != 1:
        print("ERROR: could not locate port-80 block to insert redirect before", file=sys.stderr)
        sys.exit(1)

# Step C: In the port-80 block, change `return 301 https://$host$request_uri`
# to `return 301 https://<NEW>$request_uri` so HTTP traffic for BOTH hostnames
# lands on HTTPS incubator. Server_name stays with both hostnames so nginx
# matches exactly (no relying on default-server fallback).
src = re.sub(
    r"(server\s*\{\s*listen\s+80;\s*server_name\s+[^;]+;\s*)"
    r"return\s+301\s+https://\$host\$request_uri;",
    rf"\1return 301 https://{NEW}$request_uri;",
    src,
)

p.write_text(src)
print(f"  file changed: {src != original}", flush=True)
PY

log "config after edit (server blocks):"
grep -nE "server_name|listen|return 301" "$CONF"
echo

if nginx -t 2>&1 | tail -5; then
    ok "nginx -t passed"
else
    err "nginx test failed — restoring backup"
    cp -a "$BACKUP" "$CONF"
    exit 1
fi

systemctl reload nginx
ok "nginx reloaded"
echo

log "verifying redirects..."
for url in "https://${OLD_HOST}/" "https://${OLD_HOST}/launcher" "http://${OLD_HOST}/" ; do
    loc=$(curl -sS -o /dev/null -w "%{http_code} %{redirect_url}" --max-time 10 \
        --resolve "${OLD_HOST}:443:127.0.0.1" --resolve "${OLD_HOST}:80:127.0.0.1" \
        "$url" || echo "ERR")
    ok "$url → $loc"
done

log "verifying new domain still serves..."
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
    --resolve "${NEW_HOST}:443:127.0.0.1" "https://${NEW_HOST}/" || echo "ERR")
case "$code" in
    200|301|302) ok "$NEW_HOST / → $code" ;;
    *)           err "$NEW_HOST / → $code (investigate!)" ;;
esac

echo
ok "flip complete. aihub.egelloc.com now 301-redirects to incubator.egelloc.com."
log "rollback: cp $BACKUP $CONF && nginx -t && systemctl reload nginx"
