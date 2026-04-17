# Incubator Admin Panel

The platform layer for [incubator.egelloc.com](https://incubator.egelloc.com) — authentication, app launcher, permissions, and infrastructure access management.

## What it does

- **Google OAuth SSO** — restricts access to `@egelloc.com` accounts
- **App Launcher** — home screen linking to all Incubator applications
- **Permissions** — per-user, per-app access control with toggle switches
- **Infrastructure Access** — SSH key management, database user creation, network access (trusted IPs)
- **Coaching Briefer API** — AMA streaming, ClickUp integration, student context pre-caching

## Files

| File | Purpose |
|------|---------|
| `server.py` | Flask server — all routes, auth, API endpoints |
| `permissions.py` | Permission system — grant/revoke per app per user |
| `admin.html` | Admin panel UI — permissions table + infrastructure tab |
| `launcher.html` | App launcher home screen |
| `hub-navbar.js` | Shared auth widget injected into all apps |
| `requirements.txt` | Python dependencies |

## Local Development (Docker)

Run the full platform locally with Docker Compose.

### Prerequisites

- Docker and Docker Compose
- A `.env` file (copy from `.env.example`)

### Start everything

```bash
cp .env.example .env
# Fill in at minimum: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, FLASK_SECRET_KEY

docker compose up --build
```

This starts:

| Service | URL | Purpose |
|---------|-----|---------|
| Nginx | http://localhost | Reverse proxy (entry point) |
| Admin panel | http://localhost:5051 | Flask server (also at http://localhost/admin) |
| Deploy service | http://localhost:5001 | Handles app deploys |
| Postgres | localhost:5432 | Shared DB for deployed apps |

### Test a deploy (dry run)

```bash
# Dry run — prints what would happen without building anything
python scripts/deploy.py deploy \
  --app-name testapp \
  --port 3100 \
  --local-path ./starter-template \
  --dry-run
```

### Deploy the starter template locally

```bash
# Full deploy against local Docker
python scripts/deploy.py deploy \
  --app-name testapp \
  --port 3100 \
  --local-path ./starter-template

# Verify it's running
curl http://localhost/testapp/health

# Clean up
python scripts/deploy.py undeploy --app-name testapp
```

### Manage Nginx routes manually

```bash
python scripts/nginx_config.py list
python scripts/nginx_config.py add myapp 3005 --dry-run
python scripts/nginx_config.py remove myapp --dry-run
```

### Provision a DB user manually

```bash
python scripts/db_provision.py create myapp --dry-run
python scripts/db_provision.py drop myapp --dry-run
```

### Stop everything

```bash
docker compose down           # stop containers
docker compose down -v        # stop and delete DB data
```

## Production Deployment

Runs on the DigitalOcean droplet at `165.232.155.132`, managed by PM2.

```bash
# Deploy
scp server.py admin.html launcher.html hub-navbar.js permissions.py root@165.232.155.132:/var/www/coaching-briefer/
ssh root@165.232.155.132 "pm2 restart coaching-briefer"
```

## Environment Variables

See `.env.example` for required variables (Google OAuth, DB credentials, DO API tokens).

## Related Repos

- [coachtonyle/ai-hub](https://github.com/coachtonyle/ai-hub) — Tech Knowledge Base (Next.js)
- [victor-egelloc/coachingnotes](https://github.com/victor-egelloc/coachingnotes) — Coaching Briefer
