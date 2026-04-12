(function() {
  // ── Config ──
  var APPS = [
    { slug: "hub", name: "Tech Knowledge Base", icon: "\ud83d\udcda", url: "/knowledge" },
    { slug: "briefer", name: "Coaching Briefer", icon: "\ud83d\udccb", url: "/briefer/" },
    { slug: "admin", name: "Admin Panel", icon: "\u2699\ufe0f", url: "/admin" },
  ];

  // ── Styles ──
  // Detect Streamlit — check for root+noscript pattern
  var isStreamlit = !!document.querySelector('div#root') && !!document.querySelector('noscript');

  var style = document.createElement("style");
  style.textContent = [
    isStreamlit
      ? ".hub-bar{position:fixed;bottom:20px;right:20px;top:auto;z-index:2147483647;display:flex;align-items:center;gap:8px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;pointer-events:auto}"
      : ".hub-bar{position:fixed;top:10px;right:16px;z-index:99999;display:flex;align-items:center;gap:8px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}",
    ".hub-bar *{box-sizing:border-box}",

    // Waffle button
    isStreamlit
      ? ".hub-waffle{width:48px;height:48px;border-radius:50%;border:1px solid #2a2e3b;background:#1a1d27;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all 0.15s;box-shadow:0 4px 16px rgba(0,0,0,0.4);pointer-events:auto}"
      : ".hub-waffle{width:36px;height:36px;border-radius:50%;border:none;background:none;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background 0.15s}",
    ".hub-waffle:hover{background:rgba(255,255,255,0.1)}",
    ".hub-waffle svg{width:20px;height:20px;fill:#8b8fa3}",
    ".hub-waffle:hover svg{fill:#e4e6eb}",


    // Dropdown panel
    isStreamlit
      ? ".hub-dropdown{display:none;position:absolute;bottom:56px;right:0;top:auto;background:#1a1d27;border:1px solid #2a2e3b;border-radius:12px;width:320px;box-shadow:0 8px 32px rgba(0,0,0,0.4);overflow:hidden;pointer-events:auto}"
      : ".hub-dropdown{display:none;position:absolute;top:44px;right:0;background:#1a1d27;border:1px solid #2a2e3b;border-radius:12px;width:320px;box-shadow:0 8px 32px rgba(0,0,0,0.4);overflow:hidden}",
    ".hub-dropdown.open{display:block}",

    // User header in dropdown
    ".hub-dropdown-header{padding:16px 20px;border-bottom:1px solid #2a2e3b;display:flex;align-items:center;gap:12px}",
    ".hub-dropdown-header img{width:40px;height:40px;border-radius:50%}",
    ".hub-dropdown-header-info{flex:1}",
    ".hub-dropdown-name{font-size:14px;font-weight:600;color:#e4e6eb}",
    ".hub-dropdown-email{font-size:12px;color:#8b8fa3}",

    // App grid
    ".hub-app-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:12px}",
    ".hub-app-item{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 8px;border-radius:8px;text-decoration:none;color:#8b8fa3;transition:background 0.12s;cursor:pointer}",
    ".hub-app-item:hover{background:#222635;color:#e4e6eb}",
    ".hub-app-item.active{background:rgba(79,143,247,0.1);color:#4f8ff7}",
    ".hub-app-icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;background:#222635}",
    ".hub-app-item.active .hub-app-icon{background:rgba(79,143,247,0.15)}",
    ".hub-app-item:hover .hub-app-icon{background:#2a2e3b}",
    ".hub-app-label{font-size:11px;text-align:center;line-height:1.3;font-weight:500}",

    // Footer
    ".hub-dropdown-footer{padding:12px 20px;border-top:1px solid #2a2e3b;display:flex;justify-content:center}",
    ".hub-logout{background:none;border:1px solid #2a2e3b;color:#8b8fa3;padding:6px 20px;border-radius:6px;font-size:12px;cursor:pointer;font-family:inherit;transition:all 0.15s;text-decoration:none}",
    ".hub-logout:hover{border-color:#ef4444;color:#ef4444}",

    // Mobile
    "@media(max-width:600px){.hub-bar{top:8px;right:8px}.hub-dropdown{width:280px;right:-8px}.hub-app-grid{grid-template-columns:repeat(3,1fr);gap:2px;padding:8px}.hub-app-label{font-size:10px}}",
  ].join("\n");
  document.head.appendChild(style);

  // ── Build DOM ──
  var bar = document.createElement("div");
  bar.className = "hub-bar";
  bar.innerHTML = [
    // Waffle button
    '<button class="hub-waffle" id="hubWaffle" title="Apps">',
      '<svg viewBox="0 0 24 24"><circle cx="5" cy="5" r="2"/><circle cx="12" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="12" cy="19" r="2"/><circle cx="19" cy="19" r="2"/></svg>',
    '</button>',
    // Dropdown
    '<div class="hub-dropdown" id="hubDropdown">',
      '<div class="hub-dropdown-header">',
        '<img id="hubDropdownAvatar" src="" alt="">',
        '<div class="hub-dropdown-header-info">',
          '<div class="hub-dropdown-name" id="hubDropdownName"></div>',
          '<div class="hub-dropdown-email" id="hubDropdownEmail"></div>',
        '</div>',
      '</div>',
      '<div class="hub-app-grid" id="hubAppGrid"></div>',
      '<div class="hub-dropdown-footer">',
        '<a class="hub-logout" id="hubLogout" href="/logout">Log out</a>',
      '</div>',
    '</div>',
  ].join("");

  document.body.appendChild(bar);

  // ── Detect current app from URL ──
  var currentSlug = "";
  var path = window.location.pathname;
  if (path.indexOf("/briefer") === 0) currentSlug = "briefer";
  else if (path.indexOf("/admin") === 0) currentSlug = "admin";
  else if (path.indexOf("/knowledge") === 0) currentSlug = "hub";

  // ── Load user + permissions ──
  function fetchAuth() {
    return fetch("/auth/me")
      .then(function(r) {
        if (r.ok) return r;
        return fetch("/briefer/auth/me");
      })
      .then(function(r) {
        if (r.ok) return r.json();
        return { authenticated: false };
      });
  }

  function fetchDeployedApps() {
    return fetch("/launcher/api/apps")
      .then(function(r) { return r.ok ? r.json() : { apps: [] }; })
      .then(function(data) {
        return (data.apps || []).map(function(a) {
          return { slug: a.slug, name: a.name, icon: a.icon || "\ud83d\udce6", url: "/" + a.slug + "/" };
        });
      })
      .catch(function() { return []; });
  }

  Promise.all([fetchAuth(), fetchDeployedApps()])
    .then(function(results) {
      var user = results[0];
      var deployed = results[1];
      if (!user.authenticated) return;

      var perms = user.permissions || [];

      // Avatar in dropdown
      if (user.picture) {
        document.getElementById("hubDropdownAvatar").src = user.picture;
      }

      // Name + email
      document.getElementById("hubDropdownName").textContent = user.name || "";
      document.getElementById("hubDropdownEmail").textContent = user.email || "";

      // Merge hardcoded + deployed apps (no duplicates)
      var knownSlugs = {};
      for (var i = 0; i < APPS.length; i++) knownSlugs[APPS[i].slug] = true;
      var allApps = APPS.slice();
      for (var j = 0; j < deployed.length; j++) {
        if (!knownSlugs[deployed[j].slug]) allApps.push(deployed[j]);
      }

      // Detect current app from URL
      if (!currentSlug) {
        for (var k = 0; k < deployed.length; k++) {
          if (path.indexOf("/" + deployed[k].slug) === 0) {
            currentSlug = deployed[k].slug;
            break;
          }
        }
      }

      // Build app grid — only show apps user has permission for
      var grid = document.getElementById("hubAppGrid");
      var html = "";
      function esc(s) { return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

      // Always show launcher
      html += '<a class="hub-app-item" href="/launcher">' +
        '<div class="hub-app-icon">\ud83c\udfe0</div>' +
        '<div class="hub-app-label">Home</div></a>';

      for (var i = 0; i < allApps.length; i++) {
        var app = allApps[i];
        if (perms.indexOf(app.slug) === -1) continue;
        var active = app.slug === currentSlug ? " active" : "";
        html += '<a class="hub-app-item' + active + '" href="' + esc(app.url) + '">' +
          '<div class="hub-app-icon">' + app.icon + '</div>' +
          '<div class="hub-app-label">' + esc(app.name) + '</div></a>';
      }

      grid.innerHTML = html;
    })
    .catch(function() {});

  // ── Toggle dropdown ──
  var waffle = document.getElementById("hubWaffle");
  var dropdown = document.getElementById("hubDropdown");

  function toggle(e) {
    e.stopPropagation();
    dropdown.classList.toggle("open");
  }

  waffle.addEventListener("click", toggle);

  // Close on click outside
  document.addEventListener("click", function(e) {
    if (!bar.contains(e.target)) {
      dropdown.classList.remove("open");
    }
  });

  // Close on Escape
  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") dropdown.classList.remove("open");
  });
})();
