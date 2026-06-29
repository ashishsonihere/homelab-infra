CREATE TABLE IF NOT EXISTS agent_tasks (
    id         bigserial PRIMARY KEY,
    title      text NOT NULL,
    spec       text NOT NULL,
    status     text NOT NULL DEFAULT 'queued',
    branch     text,
    pr_url     text,
    attempts   int NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    done_at    timestamptz
);
CREATE INDEX IF NOT EXISTS agent_tasks_status_idx ON agent_tasks (status, created_at);
