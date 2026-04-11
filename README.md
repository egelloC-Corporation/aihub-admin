# AI Hub Admin Panel

The platform layer for [aihub.egelloc.com](https://aihub.egelloc.com) — authentication, app launcher, permissions, and infrastructure access management.

## What it does

- **Google OAuth SSO** — restricts access to `@egelloc.com` accounts
- **App Launcher** — home screen linking to all AI Hub applications
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

## Deployment

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
