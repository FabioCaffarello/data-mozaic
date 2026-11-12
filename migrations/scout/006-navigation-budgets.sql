CREATE TABLE scout_navigation_budgets (
    session_id text PRIMARY KEY REFERENCES scout_sessions(session_id),
    principal text NOT NULL,
    source_id text NOT NULL,
    source_revision text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    approval_expires_at timestamptz NOT NULL,
    scope_snapshot jsonb NOT NULL,
    max_requests integer NOT NULL CHECK (max_requests > 0),
    max_duration_seconds integer NOT NULL CHECK (max_duration_seconds > 0),
    max_response_bytes integer NOT NULL CHECK (max_response_bytes > 0),
    requests_used integer NOT NULL DEFAULT 0 CHECK (requests_used >= 0)
);

CREATE TABLE scout_navigation_budget_requests (
    session_id text NOT NULL REFERENCES scout_navigation_budgets(session_id),
    call_id text NOT NULL,
    hop integer NOT NULL CHECK (hop >= 0),
    consumed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, call_id, hop)
);
