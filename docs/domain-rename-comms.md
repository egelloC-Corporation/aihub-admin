# Domain rename — team communications

Drafts for announcing the `aihub.egelloc.com` → `incubator.egelloc.com` move.
Delete this file after the migration is complete.

---

## Pre-cutover (send ~24h before)

**Where:** #general or #engineering Slack channel
**Tone:** heads-up, low-stakes

> **Heads up — domain rename tomorrow**
>
> Tomorrow (~[TIME]) I'm renaming our internal app platform from `aihub.egelloc.com` to `incubator.egelloc.com`.
>
> **What changes for you:**
> - The old URL will still work for ~a week — it'll redirect to the new one.
> - You'll need to log in again once after the rename. (Single click, Google OAuth.)
> - Any personal bookmarks should be updated to `incubator.egelloc.com` — the redirect is temporary.
>
> **What doesn't change:**
> - Your permissions, app access, and any work in progress.
>
> Expected hiccup window: ~2 minutes of the admin panel being briefly unavailable. Other apps (briefer, handbook, dashboards, etc.) stay up the whole time.
>
> Ping me if anything looks off afterward.

---

## At cutover time

**Where:** same channel
**Tone:** factual

> Starting the domain rename now. `incubator.egelloc.com` will go live in parallel with `aihub.egelloc.com` in a minute. Old bookmarks will keep working for now.

---

## Post-cutover confirmation

> Rename done. `incubator.egelloc.com` is live and the old domain redirects.
>
> Please update bookmarks when you get a chance. If you hit any issue (can't log in, an app looks broken, a webhook isn't firing), drop a screenshot here and I'll sort it.

---

## For people maintaining hardcoded links outside the platform

**Where:** DMs or targeted messages to anyone who owns docs, Notion pages, saved searches, etc.
**Tone:** specific, action-oriented

> Quick ask — we renamed the internal AI Hub platform from `aihub.egelloc.com` to `incubator.egelloc.com`. If you've got docs, Notion pages, or saved links pointing at the old domain, please update when you have a minute. The old domain redirects for now but we'll retire it eventually.
>
> Known places I've already updated: the app registry, the admin panel's setup guides, the repo READMEs. Known places I haven't: Notion, Slack canvases, email templates, personal bookmarks.

---

## Escalation / rollback note (internal — don't send)

If something breaks and we need to roll back:
1. SSH to egelloc-main, revert `/etc/nginx/sites-available/ai-hub` from the backup created by `migrate_domain.sh` (filename has a timestamp).
2. `nginx -t && systemctl reload nginx`.
3. Old domain resumes serving normally; new domain returns a cert error but the OAuth etc. all still work via old hostname.
4. The cert expansion is non-destructive — no need to undo it. Both hostnames remain on the cert.

No database changes are involved in the cutover, so there's nothing to roll back on the DB side.
