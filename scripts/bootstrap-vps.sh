#!/usr/bin/env bash
# Bootstrap an aihub-admin VPS from a fresh Ubuntu 24.04 box.
# Idempotent — safe to re-run. Won't overwrite an existing .env.
#
# Required env vars (caller must export before running):
#   VPS_ROLE              incubator | playground
#   APP_DOMAIN            e.g. playground.egelloc.com (must have DNS pointing here)
#   CERTBOT_EMAIL         e.g. victor@egelloc.com
#   GITHUB_TOKEN          fine-grained PAT, read access to egelloc-Corporation/aihub-admin
#   GOOGLE_CLIENT_ID      shared with incubator's OAuth client
#   GOOGLE_CLIENT_SECRET  shared with incubator's OAuth client
#   DB_HOST, DB_USER, DB_PASSWORD   Nest primary (MySQL) for staff list + DDL
#
# Optional:
#   ALLOWED_EMAIL_DOMAIN  default: egelloc.com
#   PROTECTED_IPS         default: the box's own public IP (auto-detected)
#   SESSION_COOKIE_DOMAIN default: .egelloc.com
#   DB_PORT               default: 3306
#   DB_NAME               default: egelloc
#   FLASK_SECRET_KEY      default: openssl rand -hex 32
#   POSTGRES_USER         default: aihub_admin
#   POSTGRES_PASSWORD     default: openssl rand -hex 24
#   REPO_BRANCH           default: main
#   ACQ_DB_HOST, ACQ_DB_PORT, ACQ_DB_NAME,
#   INCUBATOR_LOG_DB_USER, INCUBATOR_LOG_DB_PASSWORD   for audit logging

set -euo pipefail

REPO_DIR="/var/www/aihub-admin"
REPO_URL_BASE="github.com/egelloC-Corporation/aihub-admin.git"

require() {
  local var="$1"
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is required" >&2
    exit 1
  fi
}

for v in VPS_ROLE APP_DOMAIN CERTBOT_EMAIL GITHUB_TOKEN \
         GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET \
         DB_HOST DB_USER DB_PASSWORD; do
  require "$v"
done

ALLOWED_EMAIL_DOMAIN="${ALLOWED_EMAIL_DOMAIN:-egelloc.com}"
SESSION_COOKIE_DOMAIN="${SESSION_COOKIE_DOMAIN:-.egelloc.com}"
DB_PORT="${DB_PORT:-3306}"
DB_NAME="${DB_NAME:-egelloc}"
POSTGRES_USER="${POSTGRES_USER:-aihub_admin}"

if [ -z "${PROTECTED_IPS:-}" ]; then
  PROTECTED_IPS="$(curl -4s --max-time 5 ifconfig.me || true)"
  if [ -z "$PROTECTED_IPS" ]; then
    echo "ERROR: could not auto-detect public IP; set PROTECTED_IPS explicitly" >&2
    exit 1
  fi
fi

echo "==> VPS_ROLE=$VPS_ROLE  APP_DOMAIN=$APP_DOMAIN  PROTECTED_IPS=$PROTECTED_IPS"

# ─── 1. System packages ────────────────────────────────────────────────
echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git ufw nginx \
                      certbot python3-certbot-nginx openssl

# Docker (official repo)
if ! command -v docker >/dev/null; then
  echo "==> Installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# ─── 2. Firewall ───────────────────────────────────────────────────────
echo "==> Configuring ufw"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ─── 3. Clone / update the repo ────────────────────────────────────────
REPO_URL="https://x-access-token:${GITHUB_TOKEN}@${REPO_URL_BASE}"
REPO_BRANCH="${REPO_BRANCH:-main}"
mkdir -p /var/www
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "==> Cloning repo (branch: $REPO_BRANCH)"
  git clone --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
else
  echo "==> Updating repo (branch: $REPO_BRANCH)"
  git -C "$REPO_DIR" remote set-url origin "$REPO_URL"
  git -C "$REPO_DIR" fetch origin "$REPO_BRANCH"
  git -C "$REPO_DIR" checkout "$REPO_BRANCH"
  git -C "$REPO_DIR" reset --hard "origin/$REPO_BRANCH"
fi

# ─── 4. State files + secrets dir ──────────────────────────────────────
echo "==> Preparing state files"
cd "$REPO_DIR"
mkdir -p secrets apps nginx/apps
chmod 700 secrets
for f in permissions.db readonly_db_users.json ip_labels.json ssh_aliases.json; do
  [ -e "$f" ] || touch "$f"
done
[ -s readonly_db_users.json ] || echo "[]" > readonly_db_users.json
[ -s ip_labels.json ]         || echo "{}" > ip_labels.json
[ -s ssh_aliases.json ]       || echo "{}" > ssh_aliases.json

# ─── 5. .env (preserve existing) ───────────────────────────────────────
#
# Rather than hardcoding a subset of keys (previous approach — dropped
# DO_API_TOKEN_NEST, DB_ADMIN_*, etc. without noticing), iterate over a
# named list. If a key is in the caller's environment, it lands in .env.
# Adding a new env var = add one line to ALL_ENV_KEYS below.

# Keys whose values come from the caller's environment (typical pattern:
# `set -a; . /tmp/incubator.env; set +a; bash bootstrap.sh` to pick up
# every secret in one go).
PASSTHROUGH_KEYS=(
  GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET
  DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME
  DB_ADMIN_USER DB_ADMIN_PASSWORD
  NEST_REPLICA_HOST NEST_REPLICA_PORT
  ACQ_DB_HOST ACQ_DB_PORT ACQ_DB_NAME
  ACQ_DB_ADMIN_USER ACQ_DB_ADMIN_PASSWORD
  INCUBATOR_LOG_DB_USER INCUBATOR_LOG_DB_PASSWORD
  DO_API_TOKEN_NEST DO_DB_CLUSTER_NEST DO_DB_REPLICA_NEST
  DO_API_TOKEN_ACQ DO_DB_CLUSTER_ACQ
  GITHUB_TOKEN GITHUB_WEBHOOK_SECRET
  POSTGRES_USER POSTGRES_PASSWORD
  ANTHROPIC_API_KEY OPENAI_API_KEY
  FATHOM_API_KEY FATHOM_WEBHOOK_SECRET
  SLACK_WEBHOOK_URL
)

# Keys for instance-level config — bootstrap always writes these, using
# the CLI-passed values (with defaults supplied earlier in this script).
INSTANCE_KEYS=(
  VPS_ROLE APP_DOMAIN ALLOWED_EMAIL_DOMAIN PROTECTED_IPS
  SESSION_COOKIE_DOMAIN FLASK_SECRET_KEY
  INSTANCE_NAME INSTANCE_TAGLINE INSTANCE_LOGO_URL
  FEATURES_INFRA_ACCESS
)

if [ ! -f "$REPO_DIR/.env" ]; then
  echo "==> Writing .env"
  FLASK_SECRET_KEY="${FLASK_SECRET_KEY:-$(openssl rand -hex 32)}"
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 24)}"
  ACQ_DB_PORT="${ACQ_DB_PORT:-25060}"
  ACQ_DB_NAME="${ACQ_DB_NAME:-defaultdb}"
  umask 077
  {
    echo "# Generated by scripts/bootstrap-vps.sh on $(date -Iseconds)"
    echo "# Edit by hand after this — the script never overwrites an existing .env."
    echo ""
    echo "# ── Instance-level ──"
    for k in "${INSTANCE_KEYS[@]}"; do
      v="${!k:-}"
      [ -n "$v" ] && printf "%s=%s\n" "$k" "$v"
    done
    echo ""
    echo "# ── Shared / passthrough ──"
    for k in "${PASSTHROUGH_KEYS[@]}"; do
      v="${!k:-}"
      [ -n "$v" ] && printf "%s=%s\n" "$k" "$v"
    done
  } > "$REPO_DIR/.env"
  umask 022
  # Warn about any required passthrough that ended up missing so the
  # operator notices before admin-panel starts failing at runtime.
  for k in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET DB_HOST DB_USER DB_PASSWORD GITHUB_TOKEN; do
    if ! grep -q "^${k}=" "$REPO_DIR/.env"; then
      echo "WARN: $k is empty — admin-panel will start but features depending on it will fail." >&2
    fi
  done
else
  echo "==> .env already present — leaving as-is"
fi

# ─── 6. nginx site for the admin panel ─────────────────────────────────
echo "==> Writing nginx site for $APP_DOMAIN"
mkdir -p /etc/nginx/aihub-apps
SITE_FILE="/etc/nginx/sites-available/${APP_DOMAIN}"
cat > "$SITE_FILE" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name ${APP_DOMAIN};

    client_max_body_size 50M;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    # Per-app configs written by deploy-service.
    # Must come before the admin-panel catch-all so /app-slug/... wins.
    include /etc/nginx/aihub-apps/*.conf;

    location / {
        proxy_pass http://127.0.0.1:5051;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
}
NGINX
ln -sf "$SITE_FILE" "/etc/nginx/sites-enabled/${APP_DOMAIN}"
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ─── 7. TLS via Let's Encrypt ──────────────────────────────────────────
CERT_DIR="/etc/letsencrypt/live/${APP_DOMAIN}"
if [ ! -d "$CERT_DIR" ]; then
  echo "==> Issuing TLS cert for $APP_DOMAIN"
  certbot --nginx -d "$APP_DOMAIN" \
    --non-interactive --agree-tos --email "$CERTBOT_EMAIL" --redirect
else
  echo "==> TLS cert already exists for $APP_DOMAIN"
fi

# ─── 8. Start the compose stack ────────────────────────────────────────
echo "==> Starting Docker Compose stack"
cd "$REPO_DIR"
docker compose -f docker-compose.production.yml up -d --build

echo
echo "✓ Bootstrap complete. Admin panel: https://${APP_DOMAIN}"
echo "  Next: log in with an @${ALLOWED_EMAIL_DOMAIN} Google account to verify."
