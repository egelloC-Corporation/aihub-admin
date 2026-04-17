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
app.get("/", loginRequired, (req, res) => {
  res.send(`<!DOCTYPE html>
<html><head><title>${APP_SLUG}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon/32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/favicon/180.png">
<style>body{background:#0f1117;color:#e4e6eb;font-family:-apple-system,sans-serif;margin:0;padding:40px;}</style>
</head><body>
<h1>Hello ${req.user.name || req.user.email}!</h1>
<p>This is ${APP_SLUG}.</p>
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
