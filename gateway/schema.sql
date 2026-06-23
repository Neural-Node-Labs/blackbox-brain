-- schema.sql — Blackbox Brain / Osiris gateway DDL
-- Applied automatically on first Postgres container boot.
-- SQLAlchemy metadata.create_all handles subsequent starts idempotently.

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (username);

CREATE TABLE IF NOT EXISTS active_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_tokens_hash    ON active_tokens (token_hash);
CREATE INDEX        IF NOT EXISTS idx_active_tokens_user_id ON active_tokens (user_id);
CREATE INDEX        IF NOT EXISTS idx_active_tokens_expires ON active_tokens (expires_at);

CREATE TABLE IF NOT EXISTS ssh_jobs (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(36) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_ids TEXT NOT NULL,
    procedure TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    results TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ssh_jobs_job_id  ON ssh_jobs (job_id);
CREATE INDEX IF NOT EXISTS idx_ssh_jobs_status  ON ssh_jobs (status);
CREATE INDEX IF NOT EXISTS idx_ssh_jobs_user_id ON ssh_jobs (user_id);

CREATE TABLE IF NOT EXISTS procedures (
    id SERIAL PRIMARY KEY,
    procedure_id VARCHAR(36) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(200) NOT NULL,
    description VARCHAR(1000),
    body TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_procedures_proc_id  ON procedures (procedure_id);
CREATE INDEX        IF NOT EXISTS idx_procedures_user_id  ON procedures (user_id);

CREATE TABLE IF NOT EXISTS procedure_runs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(36) UNIQUE NOT NULL,
    procedure_id VARCHAR(36),
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_ids TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    procedure_snapshot TEXT NOT NULL,
    results TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    triggered_by VARCHAR(50) NOT NULL DEFAULT 'manual'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_run_id      ON procedure_runs (run_id);
CREATE INDEX        IF NOT EXISTS idx_runs_proc_id     ON procedure_runs (procedure_id);
CREATE INDEX        IF NOT EXISTS idx_runs_user_id     ON procedure_runs (user_id);
CREATE INDEX        IF NOT EXISTS idx_runs_status      ON procedure_runs (status);
CREATE INDEX        IF NOT EXISTS idx_runs_created_at  ON procedure_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS run_step_logs (
    id SERIAL PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL,
    target_id VARCHAR(200) NOT NULL,
    step_id VARCHAR(200) NOT NULL,
    step_name VARCHAR(500),
    action VARCHAR(100),
    command TEXT,
    reasoning TEXT,
    stdout TEXT,
    stderr TEXT,
    exit_code INTEGER,
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    attempt INTEGER NOT NULL DEFAULT 1,
    fix_applied TEXT,
    duration_ms INTEGER,
    logged_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step_logs_run_id    ON run_step_logs (run_id);
CREATE INDEX IF NOT EXISTS idx_step_logs_target_id ON run_step_logs (target_id);
CREATE INDEX IF NOT EXISTS idx_step_logs_logged_at ON run_step_logs (logged_at);

CREATE TABLE IF NOT EXISTS schedules (
    id SERIAL PRIMARY KEY,
    schedule_id VARCHAR(36) UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    procedure_id VARCHAR(36) NOT NULL,
    target_ids TEXT NOT NULL,
    cron_expr VARCHAR(100) NOT NULL,
    label VARCHAR(200),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_sched_id  ON schedules (schedule_id);
CREATE INDEX        IF NOT EXISTS idx_schedules_user_id   ON schedules (user_id);
CREATE INDEX        IF NOT EXISTS idx_schedules_proc_id   ON schedules (procedure_id);
CREATE INDEX        IF NOT EXISTS idx_schedules_enabled   ON schedules (enabled);
