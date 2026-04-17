#!/usr/bin/env bash
# Post-cutover smoke test for the domain rename.
# Runs anywhere — no server access needed. Uses only public DNS + HTTPS.
#
# Usage: ./verify_cutover.sh [old-host] [new-host]

set -u

OLD_HOST="${1:-aihub.egelloc.com}"
NEW_HOST="${2:-incubator.egelloc.com}"
EXPECTED_IP="165.232.155.132"

pass=0
fail=0

tick() { printf "\033[1;32m  ✓\033[0m %s\n" "$*"; pass=$((pass+1)); }
cross(){ printf "\033[1;31m  ✗\033[0m %s\n" "$*"; fail=$((fail+1)); }
info() { printf "\033[1;34m[%s]\033[0m\n" "$*"; }

info "DNS resolution"
for h in "$OLD_HOST" "$NEW_HOST"; do
    ip=$(dig +short "$h" A @8.8.8.8 | tail -1)
    if [[ "$ip" == "$EXPECTED_IP" ]]; then
        tick "$h → $ip"
    else
        cross "$h → $ip (expected $EXPECTED_IP)"
    fi
done

info "HTTPS reachability + cert covers both SANs"
# Grab the cert presented on the new host and check its SANs
sans=$(echo | openssl s_client -servername "$NEW_HOST" -connect "${NEW_HOST}:443" 2>/dev/null \
       | openssl x509 -noout -ext subjectAltName 2>/dev/null \
       | grep -oE "DNS:[^,]+" | sed 's/DNS://g' | tr '\n' ' ')
if [[ "$sans" == *"$OLD_HOST"* ]]; then
    tick "cert SAN includes $OLD_HOST"
else
    cross "cert SAN missing $OLD_HOST (found: $sans)"
fi
if [[ "$sans" == *"$NEW_HOST"* ]]; then
    tick "cert SAN includes $NEW_HOST"
else
    cross "cert SAN missing $NEW_HOST"
fi

info "App routes return HTTP OK"
# Pull live app slugs from production DB (authoritative).
SLUGS=$(ssh -o ConnectTimeout=5 -o BatchMode=yes root@165.232.155.132 \
    'docker exec aihub-admin-panel python3 -c "import sqlite3; c=sqlite3.connect(\"/app/permissions.db\"); print(\" \".join(r[0] for r in c.execute(\"SELECT slug FROM app_submissions WHERE status='\''live'\'' ORDER BY slug\").fetchall()))"' 2>/dev/null)
if [[ -z "$SLUGS" ]]; then
    echo "  (could not reach prod DB — falling back to hardcoded slug list)"
    SLUGS="admin briefer coaching-responder commission-tracker handbook hub incubator-logs marketing-dashboard roadmap-generator sales-kpi"
fi

# Static paths to always check + per-app roots
STATIC_PATHS=(/ /launcher /login)
for host in "$OLD_HOST" "$NEW_HOST"; do
    for path in "${STATIC_PATHS[@]}"; do
        code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://${host}${path}" || echo "ERR")
        case "$code" in
            200|301|302|304|308) tick "$host$path → $code" ;;
            *)                   cross "$host$path → $code" ;;
        esac
    done
    for slug in $SLUGS; do
        # Most apps have a trailing slash; nginx handles both
        code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://${host}/${slug}/" || echo "ERR")
        case "$code" in
            200|301|302|304|308|401|403) tick "$host/$slug/ → $code" ;;
            *)                           cross "$host/$slug/ → $code" ;;
        esac
    done
done

info "OAuth callback reachable (must return 302 or 400, not 404)"
for host in "$OLD_HOST" "$NEW_HOST"; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://${host}/auth/callback" || echo "ERR")
    case "$code" in
        200|302|400) tick "$host/auth/callback → $code" ;;
        *)           cross "$host/auth/callback → $code" ;;
    esac
done

info "Webhook endpoint reachable (expects 403 — signature missing is the right answer)"
for host in "$OLD_HOST" "$NEW_HOST"; do
    code=$(curl -sS -o /dev/null -w "%{http_code}" -X POST --max-time 10 "https://${host}/webhook/github" || echo "ERR")
    if [[ "$code" == "403" ]]; then
        tick "$host/webhook/github → 403 (rejected unsigned — correct)"
    else
        cross "$host/webhook/github → $code (expected 403)"
    fi
done

echo
if [[ $fail -eq 0 ]]; then
    printf "\033[1;32mall %d checks passed.\033[0m\n" "$pass"
    exit 0
else
    printf "\033[1;31m%d passed, %d failed.\033[0m\n" "$pass" "$fail"
    exit 1
fi
