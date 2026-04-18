# Incubator App Starter Template

Barebones Express app pre-wired with Incubator SSO authentication.

## Prerequisites

- Node.js 20+

That's it. You do **not** need to run the admin panel, set up Google OAuth, or install anything auth-related to develop locally.

## Quick start

```bash
# 1. Copy this template
cp -r starter-template/ ../my-app
cd ../my-app

# 2. Install dependencies
npm install

# 3. Set up environment
cp .env.example .env
# Edit .env:
#   APP_SLUG=my-app           (your app's slug)
#   AIHUB_DEV_EMAIL=you@egelloc.com  (your email — enables dev mode)

# 4. Run your app
npm run dev
# → runs on http://localhost:3100
```

## Test it

1. Visit `http://localhost:3100/` — you'll see your mock user info
2. Visit `http://localhost:3100/data` — permission-gated route (works because dev mode grants your APP_SLUG)
3. Visit `http://localhost:3100/health` — no auth needed (health check)

## How auth works

**Locally** — set `AIHUB_DEV_EMAIL` in `.env` and auth is bypassed. Every request authenticates as that email with permission for your `APP_SLUG`. No Google OAuth, no running the admin panel, no cookie wrangling across ports.

**In production** — remove `AIHUB_DEV_EMAIL` (or leave it unset). The SDK automatically switches to real auth: it forwards the user's session cookie to Incubator's `/auth/me` endpoint. Since all apps run behind `incubator.egelloc.com`, the cookie is shared and everything works.

The user object on `req.user`:

```js
{
  email: "you@egelloc.com",
  name: "you",
  picture: "",                              // populated in production
  permissions: ["my-app"]                   // all granted apps in production
}
```

Two middleware options:

- `loginRequired` — any authenticated @egelloc.com user
- `requirePermission("my-app")` — only users with permission for your specific app

## Platform chrome (header banner, favicon, app-switcher)

Every authenticated page that includes `<script src="/hub-navbar.js" defer>`
automatically gets, rendered by the platform, not by your app:

- **Sticky top banner** (48px): egg logo → "Incubator" wordmark → divider → your app's name
- **Favicon** set across all sizes (Incubator egg)
- **Waffle app-switcher** drawer on the right of the banner (user info + apps they have access to)

**What your app should NOT do:**
- Don't render your own "Incubator" brand or your own app name in your header — the banner already shows them
- Don't add `<link rel="icon">` tags — the platform manages favicons centrally
- Don't use `position: sticky; top: 0` on your own elements; use `top: 48px` (or `44px` on mobile) so you stack below the banner

**What your app SHOULD do:**
- Put app-specific controls (filters, date pickers, selectors, sub-navigation) in a sub-header below the banner, or integrated into your main view

Full spec: [`docs/platform-banner.md`](../docs/platform-banner.md)

## Run with Docker

```bash
cp .env.example .env
docker compose up --build
```

## Submit to Incubator

Once your app works locally:

1. Go to the admin panel → **App Registry** tab
2. Fill out the "Register an App" form with your slug, name, port, and env keys
3. Click **Submit for Review**
4. An admin will approve it and trigger deployment

## File structure

```
my-app/
├── server.js          # Your app — add routes here
├── aihub-auth.js      # Auth SDK (don't modify)
├── package.json
├── .env.example
├── .env               # Your local config (git-ignored)
├── Dockerfile
├── docker-compose.yml
└── .gitignore
```
