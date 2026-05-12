CREATE TABLE IF NOT EXISTS tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_agent TEXT,
    ip_address VARCHAR(45),
    user_uuid UUID NOT NULL REFERENCES users(uuid) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX idx_tokens_token_active
ON tokens(token)
WHERE revoked_at IS NULL;

CREATE INDEX idx_tokens_user_uuid ON tokens(user_uuid);
