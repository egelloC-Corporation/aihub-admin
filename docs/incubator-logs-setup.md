# Incubator Logs — Setup Instructions

Audit log system that tracks every authenticated request across AI Hub.

---

## 1. Create the `incubator_logs` table

Run this on the **Acquisition (PostgreSQL)** database (`defaultdb`):

```sql
CREATE TABLE IF NOT EXISTS incubator_logs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    user_email VARCHAR(255),
    user_name VARCHAR(255),
    app_slug VARCHAR(100),
    action VARCHAR(255) NOT NULL,
    detail TEXT,
    metadata JSONB DEFAULT '{}',
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

CREATE INDEX idx_incubator_logs_timestamp ON incubator_logs (timestamp DESC);
CREATE INDEX idx_incubator_logs_user_email ON incubator_logs (user_email);
CREATE INDEX idx_incubator_logs_event_type ON incubator_logs (event_type);
CREATE INDEX idx_incubator_logs_app_slug ON incubator_logs (app_slug);
CREATE INDEX idx_incubator_logs_action ON incubator_logs (action);
```

## 2. Create a scoped DB user for logging

Do NOT use `doadmin` — create a dedicated user with only INSERT/SELECT on this table:

```sql
CREATE USER incubator_logger WITH PASSWORD '<generate-a-password>';
GRANT INSERT, SELECT ON incubator_logs TO incubator_logger;
```

Also grant SELECT to Shane's user for the dashboard:

```sql
GRANT SELECT ON incubator_logs TO "shane-kelly";
```

## 3. Add environment variables

Add these to the platform `.env`:

```
INCUBATOR_LOG_DB_USER=incubator_logger
INCUBATOR_LOG_DB_PASSWORD=<the-password-from-step-2>
```

## 4. Add audit logging to `server.py`

### 4a. Add the import and logger (near the top, after existing imports):

```python
import threading
import queue
```

### 4b. Add the background log writer (after the Flask app is created):

```python
# ── Audit log — background writer ──
# Writes to incubator_logs in a background thread so requests aren't blocked
# by a DB round-trip. Uses a single persistent connection with auto-reconnect.

_audit_queue = queue.Queue(maxsize=1000)

def _audit_worker():
    """Background thread that drains the audit queue and writes to PostgreSQL."""
    import psycopg2
    conn = None
    while True:
        entry = _audit_queue.get()
        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(
                    host=os.environ.get("ACQ_DB_HOST", ""),
                    port=int(os.environ.get("ACQ_DB_PORT", "25060")),
                    dbname=os.environ.get("ACQ_DB_NAME", "defaultdb"),
                    user=os.environ.get("INCUBATOR_LOG_DB_USER", ""),
                    password=os.environ.get("INCUBATOR_LOG_DB_PASSWORD", ""),
                    sslmode="require",
                )
                conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO incubator_logs
                   (event_type, user_email, user_name, app_slug, action,
                    detail, metadata, ip_address, user_agent)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (entry["event_type"], entry.get("user_email"), entry.get("user_name"),
                 entry.get("app_slug"), entry["action"], entry.get("detail"),
                 json.dumps(entry.get("metadata") or {}),
                 entry.get("ip_address"), entry.get("user_agent")),
            )
            cur.close()
        except Exception as e:
            log.warning("Audit log write failed: %s", e)
            # Force reconnect on next iteration
            try:
                conn.close()
            except Exception:
                pass
            conn = None

_audit_thread = threading.Thread(target=_audit_worker, daemon=True)
_audit_thread.start()


def log_event(event_type, action, **kwargs):
    """Queue an audit log entry (non-blocking)."""
    try:
        _audit_queue.put_nowait({"event_type": event_type, "action": action, **kwargs})
    except queue.Full:
        pass  # Drop rather than block the request
```

### 4c. Add the after_request hook (after the Flask app is created):

```python
@app.after_request
def audit_log_request(response):
    """Log every authenticated request to the incubator_logs table."""
    # Skip noisy/internal paths
    skip = ("/static", "/health", "/hub-navbar.js", "/favicon", "/auth/me")
    if any(request.path.startswith(p) for p in skip):
        return response

    user = session.get("user")
    if not user:
        return response

    # Extract app slug from /{slug}/ routes
    parts = [p for p in request.path.strip("/").split("/") if p]
    known_slugs = {"briefer", "knowledge", "admin", "sales-kpi",
                   "marketing-dashboard", "acquisition-logs", "launcher"}
    app_slug = parts[0] if parts and parts[0] in known_slugs else None

    log_event(
        "app_access",
        f"{request.method} {request.path}",
        user_email=user.get("email"),
        user_name=user.get("name"),
        app_slug=app_slug,
        ip_address=request.headers.get("X-Forwarded-For", request.remote_addr),
        user_agent=request.headers.get("User-Agent"),
    )
    return response
```

## 5. Optional: granular event logging

Add specific `log_event()` calls to key routes for richer audit trails:

```python
# In auth_callback(), after session["user"] is set:
log_event("auth", "login", user_email=user_info["email"],
          user_name=user_info.get("name"),
          ip_address=request.headers.get("X-Forwarded-For", request.remote_addr))

# In grant_permission route:
log_event("admin", "permission_granted", user_email=session["user"]["email"],
          detail=f"Granted {target_user} access to {app_slug}",
          metadata={"target_user": target_user, "app_slug": app_slug})

# In deploy route:
log_event("admin", "deploy", user_email=session["user"]["email"],
          app_slug=app_name, detail=f"Deployed {app_name}")
```

Event types: `auth`, `app_access`, `admin`, `data`, `security`, `system`

## 6. Dashboard app registration

The `acquisition-logs` repo queries the `activities` table, not `incubator_logs`.
Shane needs to either fork/modify the repo or create a new one that queries
`incubator_logs` with the correct column names (`timestamp`, `event_type`,
`user_email`, `action` vs `date_created`, `activity_type_name`, `user_name`).

When submitting via the admin panel:
- **Slug:** `incubator-logs`
- **Port:** `3011` (3000/3001 are taken, 3010 is acquisition-logs)
- **Repo:** new repo or fork with incubator_logs queries

App-specific `.env` (create at `apps/incubator-logs/.env` after deploy):
```
DB_HOST=egelloc-ai-db-do-user-33607902-0.g.db.ondigitalocean.com
DB_PORT=25060
DB_NAME=defaultdb
DB_USER=shane-kelly
DB_PASSWORD=<shanes-db-password>
DB_SSLMODE=require
```

---

## Changes from original proposal

| Issue | Original | Fixed |
|-------|----------|-------|
| DB connection per request | New TCP+SSL connection on every hit (~150ms) | Background thread with persistent connection, non-blocking queue |
| DB credentials | Uses `doadmin` (full admin) | Scoped `incubator_logger` user with INSERT/SELECT only |
| app_slug extraction | Checks `parts[0] == "apps"` (never matches real routes) | Matches against known app slugs from `/{slug}/` paths |
| Port | 3000 (conflicts with other containers) | 3011 |
| Dashboard repo | Same as acquisition-logs (queries wrong table) | Needs new/forked repo for incubator_logs columns |
