/**
 * AI Hub Auth SDK — Node.js
 *
 * Validates requests against AI Hub's SSO.
 * Works with Express, Fastify, or plain http.
 *
 * In production, forwards the session cookie to the central auth server.
 * For local dev, set AIHUB_DEV_EMAIL in your .env to bypass SSO entirely.
 *
 * Usage (Express):
 *   const { verifyUser, loginRequired, requirePermission } = require("./aihub-auth");
 *
 *   app.get("/protected", loginRequired, (req, res) => {
 *     res.json({ hello: req.user.email });
 *   });
 *
 * Usage (manual):
 *   const user = await verifyUser(req);
 *   // Returns { email, name, picture, permissions } or null
 */

const AIHUB_AUTH_URL = process.env.AIHUB_AUTH_URL || "http://localhost:5051/auth/me";
const AIHUB_DEV_EMAIL = process.env.AIHUB_DEV_EMAIL || "";

if (AIHUB_DEV_EMAIL) {
  console.log(`[aihub-auth] Dev mode — all requests authenticate as ${AIHUB_DEV_EMAIL}`);
}

/**
 * Verify the current request against AI Hub SSO.
 *
 * In dev mode (AIHUB_DEV_EMAIL set), returns a mock user immediately.
 * In production, forwards the session cookie to AI Hub's /auth/me endpoint.
 *
 * @param {import("http").IncomingMessage} req - HTTP request (needs headers.cookie)
 * @returns {Promise<{email: string, name: string, picture: string, permissions: string[]}|null>}
 */
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

  // Extract the session cookie
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

/**
 * Express middleware — rejects unauthenticated requests with 401.
 * Sets req.user on success.
 */
function loginRequired(req, res, next) {
  verifyUser(req).then(function (user) {
    if (!user) return res.status(401).json({ error: "Unauthorized" });
    req.user = user;
    next();
  });
}

/**
 * Express middleware factory — rejects if user lacks permission for the given app slug.
 * Sets req.user on success.
 *
 * @param {string} appSlug - The app slug to check permission for
 */
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
