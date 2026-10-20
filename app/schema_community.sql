CREATE TABLE IF NOT EXISTS room_ratings (
 room_id TEXT NOT NULL REFERENCES rooms(id), user_id TEXT NOT NULL REFERENCES users(id),
 rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(room_id,user_id)
);
CREATE TABLE IF NOT EXISTS member_feedback (
 room_id TEXT NOT NULL REFERENCES rooms(id), voter_user_id TEXT NOT NULL REFERENCES users(id),
 target_member_id TEXT NOT NULL REFERENCES room_members(id), value INTEGER NOT NULL CHECK(value IN(-1,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(room_id,voter_user_id,target_member_id)
);
-- Permanent anti-abuse occupancy, independent of active/withdrawn feedback.
CREATE TABLE IF NOT EXISTS member_feedback_ledger (
 room_id TEXT NOT NULL REFERENCES rooms(id), voter_user_id TEXT NOT NULL REFERENCES users(id),
 target_principal_session_id TEXT NOT NULL REFERENCES sessions(id), created_at TEXT NOT NULL,
 PRIMARY KEY(room_id,voter_user_id,target_principal_session_id)
);
CREATE INDEX IF NOT EXISTS member_feedback_ledger_pair ON member_feedback_ledger(voter_user_id,target_principal_session_id);
INSERT INTO member_feedback_ledger(room_id,voter_user_id,target_principal_session_id,created_at)
 SELECT f.room_id,f.voter_user_id,m.session_id,f.created_at FROM member_feedback f
 JOIN room_members m ON m.id=f.target_member_id
 WHERE NOT EXISTS (SELECT 1 FROM member_feedback_ledger l WHERE l.room_id=f.room_id AND l.voter_user_id=f.voter_user_id AND l.target_principal_session_id=m.session_id);
CREATE TABLE IF NOT EXISTS room_share_permissions (
 room_id TEXT NOT NULL REFERENCES rooms(id), member_id TEXT NOT NULL REFERENCES room_members(id),
 allowed INTEGER NOT NULL CHECK(allowed IN(0,1)), updated_at TEXT NOT NULL, PRIMARY KEY(room_id,member_id)
);
CREATE TABLE IF NOT EXISTS conversation_shares (
 id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, room_id TEXT NOT NULL REFERENCES rooms(id),
 creator_user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS conversation_shares_page ON conversation_shares(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS share_messages (
 share_id TEXT NOT NULL REFERENCES conversation_shares(id), message_id TEXT NOT NULL REFERENCES messages(id),
 member_id TEXT NOT NULL REFERENCES room_members(id), seq INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
 PRIMARY KEY(share_id,message_id), UNIQUE(share_id,seq)
);
CREATE TABLE IF NOT EXISTS member_reports (
 id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id), reporter_user_id TEXT NOT NULL REFERENCES users(id),
 target_member_id TEXT NOT NULL REFERENCES room_members(id), reason TEXT NOT NULL, details TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','resolved','dismissed')),
 created_at TEXT NOT NULL, resolved_at TEXT, resolution_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS member_reports_open ON member_reports(room_id,reporter_user_id,target_member_id) WHERE status='open';
CREATE TABLE IF NOT EXISTS developer_feedback (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), text TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','resolved','dismissed')),
 created_at TEXT NOT NULL, resolved_at TEXT, resolution_note TEXT
);
CREATE INDEX IF NOT EXISTS member_feedback_page ON member_feedback(updated_at DESC,room_id);
CREATE INDEX IF NOT EXISTS member_reports_page ON member_reports(status,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS developer_feedback_page ON developer_feedback(status,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS share_messages_author ON share_messages(member_id,share_id);
