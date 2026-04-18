const express = require("express");
const path = require("path");
const { loginRequired, requirePermission } = require("./aihub-auth");

const app = express();
const PORT = process.env.PORT || 3100;
const APP_SLUG = process.env.APP_SLUG || "my-app";

app.use(express.json());

// Health check (no auth)
app.get("/health", (req, res) => {
  res.json({ status: "ok", app: APP_SLUG });
});

// HTML page — any authenticated Incubator user
//
// Notice: we don't render an app-name heading or set favicon <link> tags.
// The platform's hub-navbar.js injects a sticky header banner with
// the egg logo, "Incubator" wordmark, a divider, and your app name —
// plus the favicon tags and the app-switcher drawer. See docs/platform-banner.md.
app.get("/", loginRequired, (req, res) => {
  res.send(`<!DOCTYPE html>
<html><head><title>${APP_SLUG}</title>
<style>body{background:#0f1117;color:#e4e6eb;font-family:-apple-system,sans-serif;margin:0;padding:40px;}</style>
</head><body>
<p>Hello ${req.user.name || req.user.email}! Welcome to ${APP_SLUG}.</p>
<!-- Put your app content + its own sub-header (filters, selectors, tabs) here. -->
<script src="/hub-navbar.js" defer></script>
</body></html>`);
});

// API route — only users with permission for this specific app
app.get("/data", requirePermission(APP_SLUG), (req, res) => {
  res.json({
    message: "You have access to this app's data",
    user: req.user.email,
  });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`${APP_SLUG} running at http://localhost:${PORT}`);
});
