/**
 * Incubator Auth SDK — Node.js
 *
 * Validates requests against Incubator's SSO.
 *
 * In production, forwards the session cookie to the central auth server.
 * For local dev, set AIHUB_DEV_EMAIL in your .env to bypass SSO entirely.
 *
 * Usage:
 *   const { loginRequired, requirePermission } = require("./aihub-auth");
 *   app.get("/protected", loginRequired, (req, res) => { ... });
 */

const AIHUB_AUTH_URL = process.env.AIHUB_AUTH_URL || "http://localhost:5051/auth/me";
const AIHUB_DEV_EMAIL = process.env.AIHUB_DEV_EMAIL || "";

if (AIHUB_DEV_EMAIL) {
  console.log(`[aihub-auth] Dev mode — all requests authenticate as ${AIHUB_DEV_EMAIL}`);
}

async function verifyUser(req) {
  // Dev mode — skip SSO, return mock user
  if (AIHUB_DEV_EMAIL) {
    return {
      email: AIHUB_DEV_EMAIL,
      name: AIHUB_DEV_EMAIL.split("@")[0],
      picture: "",
      permissions: [process.env.APP_SLUG || "dev"],
    };
  }

  const cookieHeader = req.headers.cookie;
  if (!cookieHeader) return null;

  const match = cookieHeader.match(/(?:^|;\s*)session=([^;]+)/);
  if (!match) return null;

  try {
    const resp = await fetch(AIHUB_AUTH_URL, {
      headers: { cookie: `session=${match[1]}` },
    });
    if (!resp.ok) return null;

    const data = await resp.json();
    if (!data.authenticated) return null;

    return {
      email: data.email,
      name: data.name || "",
      picture: data.picture || "",
      permissions: data.permissions || [],
    };
  } catch {
    return null;
  }
}

function loginRequired(req, res, next) {
  verifyUser(req).then(function (user) {
    if (!user) return res.status(401).json({ error: "Unauthorized" });
    req.user = user;
    next();
  });
}

function requirePermission(appSlug) {
  return function (req, res, next) {
    verifyUser(req).then(function (user) {
      if (!user) return res.status(401).json({ error: "Unauthorized" });
      if (!user.permissions.includes(appSlug)) {
        return res.status(403).json({ error: "Forbidden — no access to this app" });
      }
      req.user = user;
      next();
    });
  };
}

module.exports = { verifyUser, loginRequired, requirePermission };
