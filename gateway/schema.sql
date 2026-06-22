-- schema.sql
-- Production DDL for API Gateway auth layer.
-- Applied automatically on first container boot via
-- docker-entrypoint-initdb.d, and also redundantly created at app
-- startup via SQLAlchemy metadata.create_all (idempotent either way).

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

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_tokens_hash ON active_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_active_tokens_user_id ON active_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_active_tokens_expires_at ON active_tokens (expires_at);
