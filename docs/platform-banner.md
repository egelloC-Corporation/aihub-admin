# Platform banner & header chrome

Every app on the Incubator platform gets consistent top-level branding
injected by a single script: `hub-navbar.js`. This doc is the canonical
contract between the platform and individual apps.

## TL;DR for app builders

```html
<script src="/hub-navbar.js" defer></script>
```

Include that one tag before `</body>` on every authenticated HTML page.
Then:

- **Don't** render your own "Incubator" brand, app name as a page title,
  or `<link rel="icon">` tags — the script owns all of that.
- **Do** put app-specific controls (filters, selectors, tabs, search) in
  your own sub-header *below* the banner.
- **CSS:** if you use `position: sticky; top: 0`, change it to
  `top: 48px` (desktop) / `top: 44px` (mobile) so you stack below the
  banner.

## What the script renders

Three rendering modes, chosen automatically based on the page:

### Mode 1 — Banner (non-Streamlit app pages)
Full-width sticky header at the top of `<body>`:

```
┌──────────────────────────────────────────────────────────────┐
│ 🥚 Incubator │ [App Name]                             │ ☰  │
└──────────────────────────────────────────────────────────────┘
```

- **Egg + "Incubator" (left cluster)** → clicking navigates to `/launcher`
- **Divider** — non-interactive visual separator
- **[App Name]** → clicking navigates to the app's own root (e.g. `/briefer/`)
- **Waffle button (right cluster)** → opens the app-switcher drawer with
  the user's info, apps they have permission for, and a log-out link

Banner height: **48px** (44px on `max-width: 600px`).
Position: `sticky; top: 0; z-index: 1000`.

### Mode 2 — Pill (Streamlit apps)
Bottom-right floating container alongside the existing waffle drawer:

```
                              ┌──────────────────────┐
                              │ 🥚 Incubator · Name  │ ☰
                              └──────────────────────┘
```

Why different: Streamlit's React mount creates a stacking context that
occludes our top-of-page DOM nodes. The floating + re-attach pattern
(with `isolation: isolate`) is the only reliable way to keep our UI on
top. The clickable pill carries the same "go to /launcher" affordance
as the banner's left cluster.

### Mode 3 — Floating (platform pages: `/launcher`, `/login`)
Just the top-right waffle drawer, no banner. The user is already at the
platform root; another "Incubator" badge would be redundant.

## Mode detection

| Page signal | Mode |
|---|---|
| `document.querySelector('div#root')` AND `<noscript>` present | pill |
| First URL segment is `launcher`, `login`, `logged-out`, `auth`, `webhook`, or empty | floating |
| Otherwise | banner |

The app name for the banner/pill is derived from:

1. The first URL path segment (e.g., `/briefer/foo` → `briefer`)
2. Looked up against `/launcher/api/apps` for its display name
3. Falls back to Title-Cased slug (e.g., `coaching-responder` → "Coaching Responder")

Aliases exist for paths that differ from submission slugs (the knowledge
base is submitted as slug `hub` but served at `/knowledge/`; the navbar
maps `knowledge` → `hub`).

## Contract for apps

### What you own
- The content below the banner
- Any sub-header with app-specific controls (filters, selectors, nav
  tabs, search) — anything that doesn't make sense as global chrome
- Your app's internal routing, state, data fetching

### What the platform owns
- The banner/pill itself, its content, and its layout
- The favicon and all icon `<link>` variants
- The user info display in the waffle drawer
- The set of apps shown in the drawer (filtered by the viewing user's
  permissions)

### Do / Don't

| Don't | Do |
|---|---|
| Render "Incubator" anywhere in your header | Let the banner show it |
| Show your app's name as an `<h1>` at the top | The banner shows it |
| Add `<link rel="icon">` tags | The script injects them |
| Use `position: sticky; top: 0` on your own elements | Use `top: 48px` / `top: 44px` mobile |
| Add user-info / logout UI | The waffle drawer already provides these |
| Override or restyle `.hub-bar`, `.hub-banner`, `.hub-pill`, `.hub-waffle`, `.hub-dropdown` | Leave these CSS classes alone |
| Try to redraw platform chrome on route changes | The script handles its own persistence |

## Position + layout math

### Banner mode — sticky sizing

The banner sits in normal flow at `top: 0`, so no special body padding
is needed. Your first content element renders directly below it; when
the user scrolls, the banner sticks to the viewport top.

If you have your own sticky element inside the app:

```css
/* WRONG — collides with banner */
.my-sub-header { position: sticky; top: 0; }

/* RIGHT — stacks below banner */
.my-sub-header { position: sticky; top: 48px; }
@media (max-width: 600px) {
  .my-sub-header { top: 44px; }
}
```

### z-index budget

| Element | z-index |
|---|---|
| Your app modals / overlays | < 1000 |
| Platform banner | 1000 |
| Waffle dropdown | 1001 |
| *(leave room)* | 1002–99998 |
| Streamlit-mode bar | 2147483647 |

Don't set `z-index: 9999` or higher on app elements unless you *need* to
override platform chrome (very rare).

## Streamlit specifics

Two things happen on Streamlit pages:

1. **Injection via nginx** — Streamlit apps don't have raw HTML control,
   so their nginx configs use `sub_filter` to inject the script tag
   before `</body>`:
   ```nginx
   sub_filter '</body>' '<script src="/hub-navbar.js" defer></script></body>';
   sub_filter_once on;
   sub_filter_types text/html;
   proxy_set_header Accept-Encoding "";
   ```

2. **Stacking-context fix** — `hub-navbar.js` runs a 3-second-delayed
   re-attach that clones the bar, removes the original, and re-inserts
   with `isolation: isolate`. This creates a new stacking context that
   sits above Streamlit's React tree. The clone is repeated at 6s as a
   fallback for slow cold starts.

App builders don't need to do anything specific — just register the app
as Streamlit in the submission form.

## Dev-mode / local behavior

The script fetches `/auth/me` and `/launcher/api/apps` at load time to
populate the waffle drawer. When developing an app locally (outside the
platform's nginx), those endpoints don't exist — the drawer stays
empty but the banner/pill still renders with a slug-derived app name.

For full local fidelity, the starter template's dev mode bypasses auth
entirely. See `starter-template/README.md`.

## Rollback / opt-out

There's no per-app opt-out mechanism, by design — consistency is the
point. If the banner is genuinely incompatible with an app, the fix is
platform-side (add a new rendering mode or edge case to `hub-navbar.js`).

If the script itself breaks in a way that hurts production, the rollback
is one revert of the last `hub-navbar.js` commit in the `aihub-admin`
repo. Apps keep working without the banner; the change is atomic.
