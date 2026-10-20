CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    principal_session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0,1)),
    banned INTEGER NOT NULL DEFAULT 0 CHECK (banned IN (0,1)),
    preferences_json TEXT NOT NULL DEFAULT '[]',
    onboarding_done INTEGER NOT NULL DEFAULT 0 CHECK (onboarding_done IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_seen_at TEXT NOT NULL,
    remember INTEGER NOT NULL CHECK (remember IN (0,1))
);
CREATE INDEX IF NOT EXISTS auth_user_active ON auth_sessions(user_id, revoked_at, expires_at);
CREATE INDEX IF NOT EXISTS users_created ON users(created_at, id);
CREATE TABLE IF NOT EXISTS account_audit (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT REFERENCES users(id),
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
