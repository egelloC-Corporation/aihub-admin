(function() {
  // ── Platform favicon ──
  // Inject Incubator favicon links into every app that includes this navbar,
  // so branding is consistent without per-app changes. Absolute URLs because
  // apps may be served under path prefixes (/briefer/, /sales-kpi/, etc.).
  // Remove any pre-existing <link rel="icon"> so the platform icon wins.
  (function injectFavicon() {
    var origin = window.location.origin;
    document.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"], link[rel="apple-touch-icon-precomposed"]').forEach(function(el) {
      el.parentNode.removeChild(el);
    });
    var icons = [
      { rel: "icon",             type: "image/svg+xml", href: "/favicon.svg" },
      { rel: "icon",             type: "image/png",     sizes: "32x32",   href: "/favicon/32.png" },
      { rel: "icon",             type: "image/png",     sizes: "16x16",   href: "/favicon/16.png" },
      { rel: "apple-touch-icon",                        sizes: "180x180", href: "/favicon/180.png" },
      { rel: "icon",             type: "image/png",     sizes: "192x192", href: "/favicon/192.png" },
      { rel: "icon",             type: "image/png",     sizes: "512x512", href: "/favicon/512.png" },
    ];
    icons.forEach(function(cfg) {
      var link = document.createElement("link");
      link.rel = cfg.rel;
      if (cfg.type)  link.type  = cfg.type;
      if (cfg.sizes) link.sizes = cfg.sizes;
      link.href = origin + cfg.href;
      document.head.appendChild(link);
    });
  })();

  // Idempotency: if a previous load already mounted, bail.
  if (document.getElementById("hubWaffle")) return;

  // ── Config ──
  // Hardcoded fallbacks — dynamic list is fetched from /launcher/api/apps.
  var APPS = [
    { slug: "hub", name: "Tech Knowledge Base", icon: "\ud83d\udcda", url: "/knowledge" },
    { slug: "briefer", name: "Coaching Briefer", icon: "\ud83d\udccb", url: "/briefer/" },
    { slug: "admin", name: "Admin Panel", icon: "\u2699\ufe0f", url: "/admin" },
  ];

  // Aliases: URL-path segment → registry slug. Used when an app's public
  // route differs from its submission slug (e.g., knowledge base serves at
  // /knowledge/ but is registered under slug "hub").
  var SLUG_ALIASES = { "knowledge": "hub" };

  // Path segments that are platform-level, NOT apps. Banner is skipped on
  // these so the user isn't told they're "inside Incubator" when they
  // already are at the root.
  var PLATFORM_SEGMENTS = {
    "": true, "launcher": true, "login": true, "logged-out": true,
    "auth": true, "webhook": true,
  };

  function esc(s) {
    return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;")
      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  function titleCase(slug) {
    return slug.split("-").map(function(w) {
      return w ? w[0].toUpperCase() + w.slice(1) : w;
    }).join(" ");
  }

  // ── Detect mode + current app ──
  var isStreamlit = !!document.querySelector('div#root') && !!document.querySelector('noscript');
  var path = window.location.pathname;
  var firstSeg = (path.split("/").filter(Boolean)[0] || "").toLowerCase();
  var isPlatformPage = PLATFORM_SEGMENTS[firstSeg] === true;
  var registrySlug = SLUG_ALIASES[firstSeg] || firstSeg;
  var appHomeUrl = firstSeg ? ("/" + firstSeg + "/") : "/";
  var initialAppName = isPlatformPage ? "" : titleCase(firstSeg);

  // Three rendering modes:
  //   banner   — non-Streamlit app page: full-width sticky top banner
  //              with platform lockup + app name + waffle drawer on right
  //   pill     — Streamlit app page: bottom-right floating container
  //              with a compact branding pill + waffle (existing
  //              Streamlit positioning; extended with the pill)
  //   floating — platform pages (launcher, login): just the top-right
  //              waffle drawer, no banner (the page IS Incubator)
  var mode = isPlatformPage ? "floating" : (isStreamlit ? "pill" : "banner");

  // ── Styles ──
  var style = document.createElement("style");
  style.textContent = [
    // Reset inside our tree
    ".hub-bar *, .hub-banner *{box-sizing:border-box}",

    // ── Banner (non-Streamlit, non-platform pages) ──
    ".hub-banner{position:sticky;top:0;left:0;right:0;z-index:1000;" +
      "display:flex;align-items:center;justify-content:space-between;" +
      "height:48px;padding:0 16px;background:#0f1117;border-bottom:1px solid #1f2230;" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}",
    ".hub-banner-left{display:flex;align-items:center;gap:12px;min-width:0}",
    ".hub-banner-right{display:flex;align-items:center;gap:8px;position:relative}",
    ".hub-banner-platform,.hub-banner-app{display:flex;align-items:center;gap:10px;" +
      "color:#e4e6eb;text-decoration:none;transition:opacity 0.12s;cursor:pointer}",
    ".hub-banner-platform:hover,.hub-banner-app:hover{opacity:0.75}",
    ".hub-banner-platform img{width:24px;height:24px;display:block}",
    ".hub-banner-platform-word{font-style:italic;font-weight:800;font-size:17px;letter-spacing:-0.3px}",
    ".hub-banner-divider{width:1px;height:20px;background:#2a2e3b;flex-shrink:0}",
    ".hub-banner-app-word{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",

    // ── Pill (Streamlit) ──
    ".hub-pill{display:flex;align-items:center;gap:8px;padding:8px 14px;" +
      "background:#1a1d27;border:1px solid #2a2e3b;border-radius:999px;" +
      "color:#e4e6eb;text-decoration:none;font-size:12px;" +
      "box-shadow:0 4px 16px rgba(0,0,0,0.4);transition:opacity 0.15s;pointer-events:auto;" +
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}",
    ".hub-pill:hover{opacity:0.85}",
    ".hub-pill img{width:18px;height:18px;display:block}",
    ".hub-pill-word{font-weight:700;font-style:italic}",
    ".hub-pill-sep{color:#5a5e70}",
    ".hub-pill-app{font-weight:500}",

    // ── Bar (pill + floating modes — shared container) ──
    mode === "pill"
      ? ".hub-bar{position:fixed;bottom:20px;right:20px;top:auto;z-index:2147483647;" +
          "display:flex;align-items:center;gap:8px;pointer-events:auto;" +
          "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}"
      : ".hub-bar{position:fixed;top:10px;right:16px;z-index:99999;" +
          "display:flex;align-items:center;gap:8px;" +
          "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}",

    // Waffle button
    mode === "pill"
      ? ".hub-waffle{width:48px;height:48px;border-radius:50%;border:1px solid #2a2e3b;" +
          "background:#1a1d27;cursor:pointer;display:flex;align-items:center;justify-content:center;" +
          "transition:all 0.15s;box-shadow:0 4px 16px rgba(0,0,0,0.4);pointer-events:auto}"
      : ".hub-waffle{width:36px;height:36px;border-radius:50%;border:none;background:none;" +
          "cursor:pointer;display:flex;align-items:center;justify-content:center;" +
          "transition:background 0.15s}",
    ".hub-waffle:hover{background:rgba(255,255,255,0.1)}",
    ".hub-waffle svg{width:20px;height:20px;fill:#8b8fa3}",
    ".hub-waffle:hover svg{fill:#e4e6eb}",

    // Dropdown panel — positioning depends on the parent container
    mode === "pill"
      ? ".hub-dropdown{display:none;position:absolute;bottom:56px;right:0;top:auto;" +
          "background:#1a1d27;border:1px solid #2a2e3b;border-radius:12px;width:320px;" +
          "box-shadow:0 8px 32px rgba(0,0,0,0.4);overflow:hidden;pointer-events:auto;z-index:2147483647}"
      : mode === "banner"
      ? ".hub-dropdown{display:none;position:absolute;top:46px;right:0;" +
          "background:#1a1d27;border:1px solid #2a2e3b;border-radius:12px;width:320px;" +
          "box-shadow:0 8px 32px rgba(0,0,0,0.4);overflow:hidden;z-index:1001}"
      : ".hub-dropdown{display:none;position:absolute;top:44px;right:0;" +
          "background:#1a1d27;border:1px solid #2a2e3b;border-radius:12px;width:320px;" +
          "box-shadow:0 8px 32px rgba(0,0,0,0.4);overflow:hidden}",
    ".hub-dropdown.open{display:block}",

    // User header in dropdown
    ".hub-dropdown-header{padding:16px 20px;border-bottom:1px solid #2a2e3b;display:flex;align-items:center;gap:12px}",
    ".hub-dropdown-header img{width:40px;height:40px;border-radius:50%}",
    ".hub-dropdown-header-info{flex:1}",
    ".hub-dropdown-name{font-size:14px;font-weight:600;color:#e4e6eb}",
    ".hub-dropdown-email{font-size:12px;color:#8b8fa3}",

    // App grid
    ".hub-app-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:12px}",
    ".hub-app-item{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 8px;" +
      "border-radius:8px;text-decoration:none;color:#8b8fa3;transition:background 0.12s;cursor:pointer}",
    ".hub-app-item:hover{background:#222635;color:#e4e6eb}",
    ".hub-app-item.active{background:rgba(79,143,247,0.1);color:#4f8ff7}",
    ".hub-app-icon{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px;background:#222635}",
    ".hub-app-item.active .hub-app-icon{background:rgba(79,143,247,0.15)}",
    ".hub-app-item:hover .hub-app-icon{background:#2a2e3b}",
    ".hub-app-label{font-size:11px;text-align:center;line-height:1.3;font-weight:500}",
    ".hub-app-item.dragging{opacity:0.3;transform:scale(0.9)}",
    ".hub-app-item.drag-over{background:#222635;box-shadow:inset 0 0 0 2px #4f8ff7;border-radius:8px}",

    // Footer
    ".hub-dropdown-footer{padding:12px 20px;border-top:1px solid #2a2e3b;display:flex;justify-content:center}",
    ".hub-logout{background:none;border:1px solid #2a2e3b;color:#8b8fa3;padding:6px 20px;border-radius:6px;font-size:12px;cursor:pointer;font-family:inherit;transition:all 0.15s;text-decoration:none}",
    ".hub-logout:hover{border-color:#ef4444;color:#ef4444}",

    // Mobile
    "@media(max-width:600px){" +
      ".hub-banner{height:44px;padding:0 10px}" +
      ".hub-banner-platform img{width:20px;height:20px}" +
      ".hub-banner-platform-word{font-size:14px}" +
      ".hub-banner-divider{height:16px}" +
      ".hub-banner-app-word{font-size:13px}" +
      ".hub-bar{top:8px;right:8px}" +
      ".hub-dropdown{width:280px;right:-8px}" +
      ".hub-app-grid{grid-template-columns:repeat(3,1fr);gap:2px;padding:8px}" +
      ".hub-app-label{font-size:10px}" +
    "}",
  ].join("\n");
  document.head.appendChild(style);

  // ── Build markup — one waffle+dropdown, different container per mode ──
  var waffleHtml =
    '<button class="hub-waffle" id="hubWaffle" title="Apps" aria-label="Open app switcher">' +
      '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="5" r="2"/><circle cx="12" cy="5" r="2"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="12" cy="19" r="2"/><circle cx="19" cy="19" r="2"/></svg>' +
    '</button>' +
    '<div class="hub-dropdown" id="hubDropdown">' +
      '<div class="hub-dropdown-header">' +
        '<img id="hubDropdownAvatar" src="" alt="">' +
        '<div class="hub-dropdown-header-info">' +
          '<div class="hub-dropdown-name" id="hubDropdownName"></div>' +
          '<div class="hub-dropdown-email" id="hubDropdownEmail"></div>' +
        '</div>' +
      '</div>' +
      '<div class="hub-app-grid" id="hubAppGrid"></div>' +
      '<div class="hub-dropdown-footer">' +
        '<a class="hub-logout" id="hubLogout" href="/logout">Log out</a>' +
      '</div>' +
    '</div>';

  var root;
  if (mode === "banner") {
    root = document.createElement("header");
    root.className = "hub-banner";
    root.setAttribute("role", "banner");
    root.innerHTML =
      '<div class="hub-banner-left">' +
        '<a class="hub-banner-platform" href="/launcher" title="Incubator home">' +
          '<img src="' + window.location.origin + '/favicon.svg" alt="">' +
          '<span class="hub-banner-platform-word">Incubator</span>' +
        '</a>' +
        '<span class="hub-banner-divider" aria-hidden="true"></span>' +
        '<a class="hub-banner-app" id="hubBannerApp" href="' + esc(appHomeUrl) + '" title="' + esc(initialAppName) + ' home">' +
          '<span class="hub-banner-app-word" id="hubBannerAppName">' + esc(initialAppName) + '</span>' +
        '</a>' +
      '</div>' +
      '<div class="hub-banner-right">' + waffleHtml + '</div>';
  } else if (mode === "pill") {
    root = document.createElement("div");
    root.className = "hub-bar";
    root.innerHTML =
      '<a class="hub-pill" href="/launcher" title="Incubator home">' +
        '<img src="' + window.location.origin + '/favicon.svg" alt="">' +
        '<span class="hub-pill-word">Incubator</span>' +
        '<span class="hub-pill-sep" aria-hidden="true">·</span>' +
        '<span class="hub-pill-app" id="hubPillApp">' + esc(initialAppName) + '</span>' +
      '</a>' +
      waffleHtml;
  } else {
    // floating — platform pages (launcher/login); just the waffle
    root = document.createElement("div");
    root.className = "hub-bar";
    root.innerHTML = waffleHtml;
  }

  if (mode === "banner") {
    document.body.insertBefore(root, document.body.firstChild);
  } else {
    document.body.appendChild(root);
  }

  // ── Streamlit workaround: re-attach after React has mounted. ──
  // Streamlit's React tree creates a stacking context that renders our
  // floating bar behind content despite z-index. Cloning the bar out into
  // a fresh detached node with explicit inline styles (notably
  // `isolation: isolate`) forces our own stacking context. Previously
  // this workaround was an inline script in nginx sub_filter — moved
  // here so every Streamlit app gets it consistently.
  if (mode === "pill") {
    var reattached = false;
    function reattach() {
      if (reattached) return;
      if (!root.isConnected) { reattached = true; return; }
      var clone = root.cloneNode(true);
      root.remove();
      clone.style.cssText =
        "position:fixed;bottom:20px;right:20px;z-index:2147483647;" +
        "pointer-events:auto;isolation:isolate;display:flex;" +
        "align-items:center;gap:8px";
      document.body.appendChild(clone);
      root = clone;
      bindEvents();
      reattached = true;
    }
    // 3s matches the known-working delay from the prior nginx workaround.
    // 6s is a second attempt in case Streamlit took longer to mount
    // (cold starts, slow data fetches) and the first attempt was wasted.
    setTimeout(reattach, 3000);
    setTimeout(reattach, 6000);
  }

  // ── Event binding ──
  // Defined here as a function so Streamlit's clone-and-reinsert can
  // rebind handlers against the new root (cloneNode doesn't copy
  // addEventListener listeners).
  var outsideClickHandler = null;
  var escHandler = null;
  function bindEvents() {
    var waffle = root.querySelector(".hub-waffle");
    var dropdown = root.querySelector(".hub-dropdown");
    if (!waffle || !dropdown) return;

    waffle.onclick = function(e) {
      e.stopPropagation();
      dropdown.classList.toggle("open");
    };

    if (outsideClickHandler) document.removeEventListener("click", outsideClickHandler);
    outsideClickHandler = function(e) {
      if (!root.contains(e.target)) dropdown.classList.remove("open");
    };
    document.addEventListener("click", outsideClickHandler);

    if (escHandler) document.removeEventListener("keydown", escHandler);
    escHandler = function(e) {
      if (e.key === "Escape") dropdown.classList.remove("open");
    };
    document.addEventListener("keydown", escHandler);
  }
  bindEvents();

  // ── Load user + permissions + app registry ──
  function fetchAuth() {
    return fetch("/auth/me")
      .then(function(r) { return r.ok ? r : fetch("/briefer/auth/me"); })
      .then(function(r) { return r.ok ? r.json() : { authenticated: false }; });
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

      document.getElementById("hubDropdownName").textContent = user.name || "";
      document.getElementById("hubDropdownEmail").textContent = user.email || "";
      if (user.picture) document.getElementById("hubDropdownAvatar").src = user.picture;

      // Merge hardcoded + deployed apps (no duplicates)
      var knownSlugs = {};
      for (var i = 0; i < APPS.length; i++) knownSlugs[APPS[i].slug] = true;
      var allApps = APPS.slice();
      for (var j = 0; j < deployed.length; j++) {
        if (!knownSlugs[deployed[j].slug]) allApps.push(deployed[j]);
      }

      // Refine the banner/pill app name from the registry
      var match = null;
      for (var m = 0; m < allApps.length; m++) {
        if (allApps[m].slug === registrySlug) { match = allApps[m]; break; }
      }
      if (match) {
        var bannerEl = document.getElementById("hubBannerAppName");
        if (bannerEl) bannerEl.textContent = match.name;
        var pillEl = document.getElementById("hubPillApp");
        if (pillEl) pillEl.textContent = match.name;
      }

      // Build app grid — only show apps user has permission for
      var grid = document.getElementById("hubAppGrid");
      var permitted = [];
      for (var p = 0; p < allApps.length; p++) {
        if (perms.indexOf(allApps[p].slug) !== -1) permitted.push(allApps[p]);
      }

      // Apply saved order from localStorage
      var savedOrder = [];
      try { savedOrder = JSON.parse(localStorage.getItem("hub-navbar-order") || "[]"); } catch(e) {}
      if (savedOrder.length > 0) {
        permitted.sort(function(a, b) {
          var ai = savedOrder.indexOf(a.slug), bi = savedOrder.indexOf(b.slug);
          if (ai === -1) ai = 999;
          if (bi === -1) bi = 999;
          return ai - bi;
        });
      }

      // Render — the "Incubator" tile (first, fixed) doubles as a quick
      // return to /launcher even though the banner already has one.
      var html = '<a class="hub-app-item" href="/launcher">' +
        '<div class="hub-app-icon"><img src="' + window.location.origin + '/favicon.svg" alt="Incubator" style="width:28px;height:28px;"></div>' +
        '<div class="hub-app-label">Incubator</div></a>';

      for (var q = 0; q < permitted.length; q++) {
        var app = permitted[q];
        var active = app.slug === registrySlug ? " active" : "";
        html += '<a class="hub-app-item' + active + '" draggable="true" data-slug="' + esc(app.slug) + '" href="' + esc(app.url) + '">' +
          '<div class="hub-app-icon">' + app.icon + '</div>' +
          '<div class="hub-app-label">' + esc(app.name) + '</div></a>';
      }

      grid.innerHTML = html;

      // Drag-and-drop reordering
      var dragEl = null;
      grid.addEventListener("dragstart", function(e) {
        var item = e.target.closest(".hub-app-item[data-slug]");
        if (!item) return;
        dragEl = item;
        item.classList.add("dragging");
        e.dataTransfer.effectAllowed = "move";
      });
      grid.addEventListener("dragend", function() {
        if (dragEl) dragEl.classList.remove("dragging");
        grid.querySelectorAll(".drag-over").forEach(function(el) { el.classList.remove("drag-over"); });
        dragEl = null;
      });
      grid.addEventListener("dragover", function(e) {
        e.preventDefault();
        var item = e.target.closest(".hub-app-item[data-slug]");
        if (!item || item === dragEl) return;
        grid.querySelectorAll(".drag-over").forEach(function(el) { el.classList.remove("drag-over"); });
        item.classList.add("drag-over");
      });
      grid.addEventListener("dragleave", function(e) {
        var item = e.target.closest(".hub-app-item");
        if (item) item.classList.remove("drag-over");
      });
      grid.addEventListener("drop", function(e) {
        e.preventDefault();
        var target = e.target.closest(".hub-app-item[data-slug]");
        if (!target || !dragEl || target === dragEl) return;
        target.classList.remove("drag-over");
        var items = Array.from(grid.querySelectorAll(".hub-app-item[data-slug]"));
        var dragIdx = items.indexOf(dragEl);
        var dropIdx = items.indexOf(target);
        if (dragIdx < dropIdx) grid.insertBefore(dragEl, target.nextSibling);
        else grid.insertBefore(dragEl, target);
        var newOrder = Array.from(grid.querySelectorAll(".hub-app-item[data-slug]")).map(function(el) { return el.dataset.slug; });
        try { localStorage.setItem("hub-navbar-order", JSON.stringify(newOrder)); } catch(e) {}
      });
      // Prevent navigation on drag
      grid.addEventListener("click", function(e) {
        if (e.target.closest(".hub-app-item.dragging")) e.preventDefault();
      });
    })
    .catch(function() {});
})();
