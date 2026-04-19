# App instrumentation contract

How apps on the Incubator platform emit custom success-metric events
(e.g. `roadmap_generated`, `brief_cached`) and how those events flow
into the Incubator Logs Success Metrics dashboard.

**TL;DR:**
1. Your app POSTs to `/incubator-logs/api/log` with `event_type="feature_use"`, a verb-noun `action`, and optional `metadata` JSON.
2. A platform admin defines the key event name in the Incubator Logs **Success Metrics** panel (click Define on your app's card).
3. Emissions + definitions combine to populate the count + sparkline for your app.

Emitting and defining are independent — you can emit before anyone
defines, or define before anyone emits. Events always persist; the
definition only tells the dashboard which action name to count.

---

## Event shape

Every success-metric event uses the following envelope:

```json
{
  "event_type": "feature_use",
  "action": "roadmap_generated",
  "metadata": {
    "duration_ms": 3421,
    "status": "ok",
    "user_tier": "coach"
  }
}
```

| Field          | Required | Purpose                                                   |
| -------------- | -------- | --------------------------------------------------------- |
| `event_type`   | yes      | Always `feature_use` for instrumentation events.          |
| `action`       | yes      | verb-noun describing what happened. Enforced by regex — see below. |
| `metadata`     | no       | Free-form JSON. Keep field names stable; values should have bounded cardinality (durations, status strings, categorical tiers — not raw IDs unless hashed). |
| `user_email`   | auto     | Populated by the platform from the authenticated session. Don't set manually. |
| `user_name`    | auto     | Same.                                                     |
| `app_slug`     | no       | Usually inferred from the request path. Safe to omit.     |
| `detail`       | no       | Optional short human-readable note (~200 chars).          |

### `action` naming rules

Enforced both client-side in the Define modal and server-side in
the KPI creation endpoint:

- Regex: `^[a-z][a-z0-9_-]{2,49}$`
- Lowercase only. Starts with a letter. 3–50 characters.
- Only a–z, 0–9, `_`, `-` allowed.

### Standard `metadata` fields

Use these key names when applicable so dashboards and alerts
don't need per-app lookups:

| Field         | Type    | When to use                                           |
| ------------- | ------- | ----------------------------------------------------- |
| `duration_ms` | integer | Time the user waited for the action to complete.      |
| `status`      | string  | `"ok"` for success. `"failed"`, `"partial"` for bad outcomes. Aggregators can filter by `status="ok"` for clean success counts. |
| `user_tier`   | string  | User category if relevant (e.g., `coach`, `admin`, `student`). |

You can add your own fields. Keep them low-cardinality so they
fit in JSONB and stay queryable.

### What NOT to put in `action` or unbounded metadata

- Raw user IDs, emails, student IDs — cardinality kills the column.
- Session IDs, UUIDs, file paths — store a hash if you need them, else skip.
- Free-text descriptions — use `detail` or omit.

Good:
```
action: "roadmap_generated"
metadata: { "roadmap_type": "freshman", "duration_ms": 3421, "status": "ok" }
```

Bad:
```
action: "roadmap_generated_for_student_1234"   ← explodes cardinality
metadata: { "student_id": 1234 }               ← PII + high cardinality
```

---

## How to emit

The endpoint is `/incubator-logs/api/log`. It's same-origin for
any app served under `incubator.egelloc.com/<slug>/`, so no CORS
and no explicit auth — just forward the session cookie.

### Browser (fetch)

```js
// Fire-and-forget after a user action succeeds
fetch("/incubator-logs/api/log", {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    event_type: "feature_use",
    action: "roadmap_generated",
    metadata: {
      roadmap_type: "freshman",
      duration_ms: Math.round(performance.now() - startedAt),
      status: "ok",
    },
  }),
}).catch((err) => console.warn("instrument failed", err));
```

### Node / Next.js (server-side, same container)

```js
await fetch(`${process.env.INTERNAL_LOGS_URL || "http://aihub-incubator-logs:3011"}/api/log`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    // Required for server-to-server emits (background jobs, workers, cron):
    // /api/log expects either a valid session cookie OR this shared secret.
    // Background tasks have no user session, so without a secret they 401.
    "X-Log-Secret": process.env.LOG_SECRET,
  },
  body: JSON.stringify({
    event_type: "feature_use",
    action: "roadmap_generated",
    user_email: req.user?.email,       // include when you know who triggered it
    metadata: { duration_ms, status: "ok" },
  }),
});
```

**One-time deployment setup:** pick a random string (e.g. `openssl rand -hex 32`)
and set `LOG_SECRET=<same-value>` in BOTH the `incubator-logs` app's `.env`
AND every emitting app's `.env`. The secret is only needed for
server-to-server emits; browser calls from a logged-in user authenticate
via the session cookie automatically.

### Python

```python
import os, requests

def log_event(action: str, **metadata) -> None:
    try:
        requests.post(
            "http://aihub-incubator-logs:3011/api/log",
            json={"event_type": "feature_use", "action": action, "metadata": metadata},
            headers={"X-Log-Secret": os.environ.get("LOG_SECRET", "")},
            timeout=2,
        )
    except requests.RequestException:
        pass  # Never block the user flow on instrumentation
```

### curl (for testing)

```bash
curl -X POST https://incubator.egelloc.com/incubator-logs/api/log \
  -H "Content-Type: application/json" \
  -H "Cookie: session=<your-session>" \
  -d '{"event_type":"feature_use","action":"roadmap_generated","metadata":{"status":"ok"}}'
```

### Rules of thumb

- **Fire-and-forget.** Never block the user on the log POST. Catch errors and log them, but continue.
- **Emit on success.** Count the outcome you actually want to celebrate (`roadmap_generated` after the PDF is delivered, not at request start).
- **Don't log PII.** If an ID is useful for joining, hash it (SHA-256 hex is fine). If you're not going to join, just omit.
- **Low cardinality.** Action names should map to a few dozen distinct values across the platform — not thousands.

---

## How it connects to the Success Metrics dashboard

Defining a KPI in the Incubator Logs UI creates a row in the
`app_kpi_definitions` table tying `(app_slug, action)` to a human
display label. When the dashboard loads, it:

1. Fetches all definitions.
2. For each defined KPI, counts matching `feature_use` events in
   the current range.
3. Shows that count (plus a sparkline) on the app's card.

Apps whose cards still say "Key events not defined yet" just need
someone to click Define. The events are already being recorded if
the app is emitting; the definition flips the card to a metric view.

---

## Naming guide

Pattern: **verb_noun** (or **verb-noun**; pick one style per app
and stay consistent).

| Good                    | Why                                              |
| ----------------------- | ------------------------------------------------ |
| `roadmap_generated`     | Clear verb (generated) + clear object (roadmap). |
| `brief_cached`          | Past tense = the event happened.                 |
| `check_in_responded`    | Compound noun, kebab-or-snake throughout.        |
| `report_exported`       | Simple + searchable.                             |

| Avoid                       | Why                                                  |
| --------------------------- | ---------------------------------------------------- |
| `roadmap`                   | Not an event — a noun. Needs a verb.                 |
| `process_roadmap_for_12345` | High cardinality; IDs belong in metadata, not action.|
| `gen_rm`                    | Too terse to discover later.                         |
| `ROADMAP_DONE`              | Case rejected by validation regex.                   |
| `app.roadmap.generated`     | No namespaces needed — app_slug is already on the event. |

Rule of thumb: if the dashboard card label would make sense as
"*{Display label} over the last 7 days*", the action name is good.

---

## Checklist for adding instrumentation to your app

- [ ] Pick an event: what's the "win" users care about?
- [ ] Pick a verb-noun `action` name. Check it against the regex.
- [ ] Find the code path where that win happens (after success, not at start).
- [ ] Add a `fetch` / `requests` POST, fire-and-forget.
- [ ] Deploy.
- [ ] In Incubator Logs → App Usage → Success Metrics, click Define on your app's card. Enter the `action`, a display label, and an optional description.
- [ ] Trigger the event. Wait ~10s. Refresh the dashboard. Confirm the count ticks up.
