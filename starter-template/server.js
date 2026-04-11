const express = require("express");
const { loginRequired, requirePermission } = require("./aihub-auth");

const app = express();
const PORT = process.env.PORT || 3100;
const APP_SLUG = process.env.APP_SLUG || "my-app";

app.use(express.json());

// Health check (no auth)
app.get("/health", (req, res) => {
  res.json({ status: "ok", app: APP_SLUG });
});

// Protected route — any authenticated AI Hub user
app.get("/", loginRequired, (req, res) => {
  res.json({
    message: `Hello ${req.user.name || req.user.email}!`,
    app: APP_SLUG,
    user: req.user,
  });
});

// Protected route — only users with permission for this specific app
app.get("/data", requirePermission(APP_SLUG), (req, res) => {
  res.json({
    message: "You have access to this app's data",
    user: req.user.email,
  });
});

app.listen(PORT, () => {
  console.log(`${APP_SLUG} running at http://localhost:${PORT}`);
});
