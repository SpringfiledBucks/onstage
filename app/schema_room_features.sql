CREATE TABLE IF NOT EXISTS room_options (
 room_id TEXT PRIMARY KEY REFERENCES rooms(id),
 access TEXT NOT NULL DEFAULT 'public' CHECK(access IN ('public','password','invite')),
 password_hash TEXT,
 hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0,1)),
 share_enabled INTEGER NOT NULL DEFAULT 1 CHECK(share_enabled IN (0,1)),
 cast_scenario_id TEXT NOT NULL,
 recruitment_json TEXT NOT NULL DEFAULT '[]',
 offline_since TEXT
);
CREATE TABLE IF NOT EXISTS room_access_grants (
 room_id TEXT NOT NULL REFERENCES rooms(id),
 session_id TEXT NOT NULL REFERENCES sessions(id),
 created_at TEXT NOT NULL,
 PRIMARY KEY(room_id,session_id)
);
CREATE TABLE IF NOT EXISTS room_invites (
 id TEXT PRIMARY KEY,
 room_id TEXT NOT NULL REFERENCES rooms(id),
 token_hash TEXT NOT NULL UNIQUE,
 expires_at TEXT NOT NULL,
 max_uses INTEGER NOT NULL CHECK(max_uses BETWEEN 1 AND 100),
 uses INTEGER NOT NULL DEFAULT 0,
 revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS room_invites_room ON room_invites(room_id);
CREATE TABLE IF NOT EXISTS room_scene_actions (
 room_id TEXT NOT NULL REFERENCES rooms(id),
 action_id TEXT NOT NULL,
 scenario_id TEXT NOT NULL,
 message_id TEXT NOT NULL REFERENCES messages(id),
 created_at TEXT NOT NULL,
 PRIMARY KEY(room_id,action_id)
);
CREATE TABLE IF NOT EXISTS room_presence_leases (
 room_id TEXT NOT NULL REFERENCES rooms(id),
 session_id TEXT NOT NULL REFERENCES sessions(id),
 connection_id TEXT NOT NULL,
 auth_session_id TEXT REFERENCES auth_sessions(id),
 expires_at TEXT NOT NULL,
 PRIMARY KEY(room_id,session_id,connection_id)
);
CREATE INDEX IF NOT EXISTS presence_expiry ON room_presence_leases(expires_at);
CREATE TABLE IF NOT EXISTS room_list_snapshots (
 id TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL,
 criteria TEXT NOT NULL,
 room_ids TEXT NOT NULL,
 expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS room_snapshot_expiry ON room_list_snapshots(expires_at);
CREATE INDEX IF NOT EXISTS room_members_session_room ON room_members(session_id,room_id);
