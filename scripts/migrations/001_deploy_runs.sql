-- One row per deploy-service action (deploy, restart, undeploy, …).
-- Read by the incubator-logs app's Deploys viewer.
--
-- Run once against the Acquisition DB (same DB as incubator_logs):
--   psql "host=... port=... dbname=... user=... password=... sslmode=require" \
--     -f scripts/migrations/001_deploy_runs.sql

CREATE TABLE IF NOT EXISTS deploy_runs (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    app_slug     VARCHAR(100) NOT NULL,
    action       VARCHAR(50)  NOT NULL,           -- deploy | restart | redeploy | undeploy | self-deploy | kb-deploy
    actor_email  VARCHAR(255),                    -- admin who triggered; NULL for webhook-origin
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at     TIMESTAMPTZ,                     -- NULL while running
    status       VARCHAR(20)  NOT NULL DEFAULT 'running',  -- running | success | failed
    summary      TEXT,                            -- one-line outcome
    log          TEXT,                            -- captured output, last ~200KB
    metadata     JSONB       NOT NULL DEFAULT '{}'::jsonb  -- port, repo_url, commit_sha, etc.
);

CREATE INDEX IF NOT EXISTS deploy_runs_started_at_idx
    ON deploy_runs (started_at DESC);

CREATE INDEX IF NOT EXISTS deploy_runs_app_slug_started_idx
    ON deploy_runs (app_slug, started_at DESC);

CREATE INDEX IF NOT EXISTS deploy_runs_running_idx
    ON deploy_runs (started_at DESC)
    WHERE status = 'running';
