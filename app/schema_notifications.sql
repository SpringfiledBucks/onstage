CREATE TABLE IF NOT EXISTS push_subscriptions (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 auth_session_id TEXT NOT NULL REFERENCES auth_sessions(id),
 endpoint_hash TEXT NOT NULL UNIQUE, endpoint TEXT NOT NULL,
 p256dh TEXT NOT NULL, auth_secret TEXT NOT NULL, frontend_origin TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS push_subscription_user ON push_subscriptions(user_id,auth_session_id);
CREATE TABLE IF NOT EXISTS room_notification_follows (
 user_id TEXT NOT NULL REFERENCES users(id), room_id TEXT NOT NULL REFERENCES rooms(id),
 created_at TEXT NOT NULL, PRIMARY KEY(user_id,room_id)
);
CREATE TABLE IF NOT EXISTS push_outbox (
 subscription_id TEXT PRIMARY KEY REFERENCES push_subscriptions(id) ON DELETE CASCADE,
 room_id TEXT NOT NULL REFERENCES rooms(id), message_id TEXT NOT NULL REFERENCES messages(id),
 channel TEXT NOT NULL CHECK(channel IN ('stage','backstage')),
 queued_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_until TEXT, claim_token TEXT
);
CREATE INDEX IF NOT EXISTS push_outbox_due ON push_outbox(next_attempt_at,lease_until);
