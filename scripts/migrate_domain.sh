#!/usr/bin/env bash
# Domain rename: aihub.egelloc.com → incubator.egelloc.com (dual-serve phase).
#
# Idempotent. Safe to re-run. Never removes the old hostname in this phase —
# only adds the new one so both resolve during the migration window.
#
# Run on the host (egelloc-main) as root.

set -euo pipefail

OLD_HOST="aihub.egelloc.com"
NEW_HOST="incubator.egelloc.com"
CONF="/etc/nginx/sites-available/ai-hub"
BACKUP="/etc/nginx/sites-available/ai-hub.backup.$(date +%Y%m%d-%H%M%S)"
LE_LIVE="/etc/letsencrypt/live/${OLD_HOST}"

log() { printf "\033[1;34m[migrate]\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m[ok]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[err]\033[0m %s\n" "$*" >&2; }

# 0. Sanity checks
[[ $EUID -eq 0 ]] || { err "must run as root"; exit 1; }
[[ -f "$CONF" ]] || { err "config not found: $CONF"; exit 1; }
[[ -d "$LE_LIVE" ]] || { err "letsencrypt live dir missing: $LE_LIVE"; exit 1; }
command -v certbot >/dev/null || { err "certbot not installed"; exit 1; }
command -v nginx >/dev/null || { err "nginx not installed"; exit 1; }

# 1. Show current state
log "current server_name lines in $CONF:"
grep -n "server_name" "$CONF" || true
echo

# 2. Backup (always — cheap insurance)
cp -a "$CONF" "$BACKUP"
ok "backed up to $BACKUP"

# 3. Add new hostname idempotently.
#    Matches "server_name aihub.egelloc.com;" and appends new host before the ;
#    Only rewrites if the new host isn't already present.
if grep -qE "server_name[[:space:]]+[^;]*\b${NEW_HOST}\b" "$CONF"; then
    ok "$NEW_HOST already in server_name — skipping edit"
else
    # Use sed with a pattern that matches any server_name line containing OLD_HOST
    # and appends NEW_HOST before the semicolon.
    sed -i -E "s/(server_name[[:space:]]+[^;]*\b${OLD_HOST}\b[^;]*);/\1 ${NEW_HOST};/" "$CONF"
    ok "added $NEW_HOST to server_name"
    log "new server_name lines:"
    grep -n "server_name" "$CONF"
fi
echo

# 4. Test config before reload
if nginx -t 2>&1 | tail -5; then
    ok "nginx -t passed"
else
    err "nginx config test failed — restoring backup"
    cp -a "$BACKUP" "$CONF"
    exit 1
fi

# 5. Reload so the new hostname is accepted for HTTP-01 challenge
systemctl reload nginx
ok "nginx reloaded (pre-cert)"
echo

# 6. Expand the cert. --expand adds the new SAN to the existing cert lineage.
#    --nginx plugin handles the ACME HTTP-01 challenge via port 80.
#    --non-interactive --agree-tos is safe: Let's Encrypt TOS already accepted
#    previously for this server (existing certs prove this).
log "running certbot --expand (this may take 15-30s)..."
if certbot --nginx --expand \
    -d "$OLD_HOST" -d "$NEW_HOST" \
    --non-interactive --agree-tos \
    --redirect \
    --cert-name "$OLD_HOST" 2>&1 | tail -20; then
    ok "certbot --expand succeeded"
else
    err "certbot failed. nginx config still points at old cert — site stays up."
    err "check the output above. Backup at $BACKUP if you need to roll back server_name."
    exit 1
fi
echo

# 7. Final reload to pick up the new cert (certbot --nginx usually does this itself
#    but an extra reload is harmless and makes the final state explicit)
nginx -t && systemctl reload nginx
ok "nginx reloaded (post-cert)"
echo

# 8. Verify both hostnames serve HTTPS with the updated cert
log "verifying..."
for host in "$OLD_HOST" "$NEW_HOST"; do
    # -k is safe here — we're verifying reachability on the server itself via
    # --resolve, not trusting the cert blindly for production users.
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
        --resolve "${host}:443:127.0.0.1" "https://${host}/health" || echo "ERR")
    case "$code" in
        200|302|301|404) ok "$host → HTTP $code" ;;
        *)               err "$host → HTTP $code (investigate)" ;;
    esac
done

# 9. Confirm the cert covers both names
log "cert SAN check:"
openssl x509 -in "${LE_LIVE}/fullchain.pem" -noout -text \
    | grep -A1 "Subject Alternative Name" | tail -1 | sed 's/^[[:space:]]*/  /'
echo

ok "migration phase 1 (dual-serve) complete."
log "next: update GitHub webhook URLs, update app registry URLs, then flip old domain to 301."
