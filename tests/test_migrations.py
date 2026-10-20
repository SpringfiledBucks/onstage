import json
from pathlib import Path
import sqlite3

import pytest

from app.security import Settings, token_hash
from app.store import CHARACTER_IDS, Store


def legacy_database(directory, schema_version=1):
    """Build frozen v1/v2/v3 fixtures without invoking current migration code."""
    directory.mkdir()
    path = directory / "onstage.sqlite3"
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA foreign_keys=ON")
        database.executescript(Path(__file__).with_name("fixtures").joinpath(f"schema_v{schema_version}.sql").read_text())
        database.execute("INSERT INTO schema_meta VALUES('schema_version',?)", (str(schema_version),))
        database.executemany("INSERT INTO characters VALUES(?)", [(value,) for value in CHARACTER_IDS])
        database.executemany(
            "INSERT INTO sessions VALUES(?,?,?,?,?,NULL)",
            [("old-host", token_hash("legacy-host-token"), "legacy-host-csrf", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
             ("old-actor", token_hash("legacy-actor-token"), "legacy-actor-csrf", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z")],
        )
        database.execute(
            "INSERT INTO rooms(id,title,scenario_id,status,allow_join,host_session_id,stage_seq,backstage_seq,created_at,updated_at) "
            "VALUES('legacy-room','旧房间','mygo_rehearsal','live',1,'old-host',1,1,'2026-01-02T00:00:00Z','2026-01-02T00:00:00Z')"
        )
        columns = "id,room_id,session_id,alias,character_id,first_character_id,joined_at"
        members = [
            ("host-member", "legacy-room", "old-host", "旅人·ABCDEF", None, None, "2026-01-02T00:00:00Z"),
            ("actor-member", "legacy-room", "old-actor", "旅人·123456", "mygo.anon", "mygo.anon", "2026-01-02T00:00:01Z"),
        ]
        if schema_version >= 2:
            columns += ",display_alias"
            members = [(*member, f"观众 {index}") for index, member in enumerate(members, 1)]
        placeholders = ",".join("?" for _ in members[0])
        database.executemany(f"INSERT INTO room_members({columns}) VALUES({placeholders})", members)
        database.executemany(
            "INSERT INTO messages(id,room_id,seq,channel,member_id,character_id,author,text,created_at,kind,client_message_id) "
            "VALUES(?,'legacy-room',1,?,?,?,?,?,'2026-01-02T00:00:02Z','speech',?)",
            [("old-stage", "stage", "host-member", None, "旅人·ABCDEF", "原始舞台记录", "old-stage-key"),
             ("old-backstage", "backstage", "actor-member", "mygo.anon", "千早爱音", "原始后台记录", "old-backstage-key")],
        )
        database.execute(
            "INSERT INTO role_requests VALUES('old-request','legacy-room','host-member','mygo.tomori','pending','2026-01-02T00:00:03Z',NULL)"
        )
        if schema_version == 3:
            original_sticker = {"id": "legacy-sticker", "label": "旧贴纸", "character_id": "mygo.anon",
                                "image": "/static/assets/stickers/legacy.png"}
            database.execute(
                "INSERT INTO messages(id,room_id,seq,channel,member_id,character_id,author,text,created_at,kind,"
                "client_message_id,content_type,sticker_id,sticker_json) VALUES('old-sticker','legacy-room',2,'stage',"
                "'actor-member','mygo.anon','千早爱音','[贴纸] 旧贴纸','2026-01-02T00:00:03Z','speech',"
                "'old-sticker-key','sticker','legacy-sticker',?)", (json.dumps(original_sticker, ensure_ascii=False),),
            )
            database.execute("UPDATE rooms SET stage_seq=2 WHERE id='legacy-room'")
        if schema_version >= 2:
            database.execute("UPDATE rooms SET background='保留已编辑的背景',announcement='保留公告' WHERE id='legacy-room'")
            database.execute(
                "INSERT INTO sessions VALUES('old-removed',?,'removed-csrf','2026-01-01T00:00:00Z','2099-01-01T00:00:00Z',NULL)",
                (token_hash("removed-token"),),
            )
            database.execute(
                "INSERT INTO room_members(id,room_id,session_id,alias,display_alias,status,joined_at,left_at) "
                "VALUES('removed-member','legacy-room','old-removed','观众 3','观众 3','left','2026-01-02T00:00:00Z','2026-01-02T00:00:03Z')"
            )
            database.execute("INSERT INTO room_bans VALUES('legacy-room','old-removed','host-member','2026-01-02T00:00:03Z')")
            database.execute("INSERT INTO rate_events(action,subject_key,created_at) VALUES('report','old-actor','2026-01-02T00:00:04Z')")
            for report_id, message_id, reporter, reporter_member, target_member, against_host in (
                ("old-open-report", "old-stage", "old-actor", "actor-member", "host-member", 1),
                ("old-resolved-report", "old-backstage", "old-host", "host-member", "actor-member", 0),
            ):
                message = dict(database.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
                database.execute(
                    "INSERT INTO reports(id,room_id,reporter_session_id,reporter_member_id,target_type,target_key,message_id,"
                    "target_member_id,against_host,reason,details,snapshot_json,created_at) "
                    "VALUES(?,'legacy-room',?,?,'message',?,?,?,?, 'other','旧举报说明',?,'2026-01-02T00:00:04Z')",
                    (report_id, reporter, reporter_member, "message:" + message_id, message_id, target_member,
                     against_host, json.dumps({"message": message}, ensure_ascii=False)),
                )
            database.execute(
                "UPDATE reports SET status='resolved',resolved_at='2026-01-02T00:00:05Z',resolution_note='旧处理说明',"
                "resolved_by='host',resolver_member_id='host-member' WHERE id='old-resolved-report'"
            )
    return path


def fingerprint_legacy(path):
    with sqlite3.connect(path) as database:
        tables = [row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {
            "schema": database.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
            "columns": {table: [row[1] for row in database.execute(f"PRAGMA table_info({table})")] for table in tables},
            "tables": {table: database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables},
        }


def make_store(directory):
    return Store(Settings.from_values(data_dir=directory, public_origin="http://127.0.0.1:8765", secure_cookie=False))


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_old_schema_upgrade_preserves_rows_and_creates_verified_backup(tmp_path, schema_version):
    directory = tmp_path / "legacy"
    path = legacy_database(directory, schema_version)
    before = fingerprint_legacy(path)
    store = make_store(directory)
    store.initialize()
    assert store.ready() == {"status": "ready", "schema_version": 4, "community_revision": "20261020_02"}
    assert store.get_session("legacy-host-token")["csrf_token"] == "legacy-host-csrf"
    assert store.get_session("legacy-actor-token")["authenticated"] is True
    assert store.get_session("legacy-host-token")["requires_nickname"] is True
    assert store.get_session("legacy-host-token")["nickname"] is None
    room = store.get_room("legacy-host-token", "legacy-room", set())["room"]
    assert room["title"] == "旧房间" and room["status"] == "live"
    expected_background = store.scenarios["mygo_rehearsal"]["opening"] if schema_version == 1 else "保留已编辑的背景"
    assert room["background"] == expected_background
    assert room["announcement"] == ("" if schema_version == 1 else "保留公告")
    assert {member["alias"] for member in room["members"]} == {"观众 1", "观众 2"}
    actor = store.get_room("legacy-actor-token", "legacy-room", set())["room"]["me"]
    assert actor["character_id"] == actor["first_character_id"] == "mygo.anon"
    messages = store.get_messages("legacy-host-token", "legacy-room", "stage", 0, 100)["messages"]
    assert messages[0]["author"] == "观众 1"
    assert messages[0]["text"] == "原始舞台记录"
    assert messages[0]["kind"] == "speech" and messages[0]["content_type"] == "text"
    assert messages[0]["sticker_id"] is None and messages[0]["sticker"] is None
    assert store.get_messages("legacy-actor-token", "legacy-room", "backstage", 0, 100)["messages"][0]["text"] == "原始后台记录"
    assert not any(message["kind"] == "system" for message in messages)  # No invented past entries.
    if schema_version == 3:
        assert messages[1]["id"] == "old-sticker"
        assert messages[1]["sticker"]["label"] == "旧贴纸"
    with sqlite3.connect(path) as database:
        for table, columns in before["columns"].items():
            if table == "schema_meta":
                continue
            # Compare every old column, including old report snapshot bytes and decisions.
            selected = ",".join(columns)
            assert database.execute(f"SELECT {selected} FROM {table} ORDER BY 1").fetchall() == before["tables"][table]
        assert database.execute("SELECT alias FROM room_members WHERE id='host-member'").fetchone()[0] == "旅人·ABCDEF"
        assert database.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            database.execute("UPDATE messages SET content_type='sticker' WHERE id='old-stage'")
    assert store.last_migration_backup is not None
    assert store.last_migration_backup.name.startswith(f"onstage-schema{schema_version}-before-schema4-")
    assert fingerprint_legacy(store.last_migration_backup) == before
    with sqlite3.connect(store.last_migration_backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Existing data supports the new payload without replacing the member or room.
    sticker_id = next(iter(store.stickers))
    created = store.send_message("legacy-actor-token", "legacy-actor-csrf", "legacy-room", "stage", "",
                                 "migrated-sticker-key", "sticker", sticker_id)["message"]
    assert created["seq"] == (3 if schema_version == 3 else 2) and created["content_type"] == "sticker"
    if schema_version >= 2:
        reports = store.list_reports("legacy-actor-token", None, 0, 50)["reports"]
        assert reports[0]["id"] == "old-open-report" and reports[0]["status"] == "open"
        if schema_version == 2:
            assert "content_type" not in reports[0]["snapshot"]["message"]
        assert store.operator_list_reports("resolved", 0, 50)["reports"][0]["resolution_note"] == "旧处理说明"
    # Opening an already upgraded database must not create another migration backup.
    make_store(directory).initialize()
    assert len(list((directory / "backups").glob("*.sqlite3"))) == 1


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_migration_failure_after_all_ddl_rolls_back_schema_and_every_old_row(tmp_path, monkeypatch, schema_version):
    directory = tmp_path / "legacy"
    path = legacy_database(directory, schema_version)
    before = fingerprint_legacy(path)
    store = make_store(directory)
    from app.migrations import execute_schema
    def fail_after_changes(connection, script):
        execute_schema(connection, script)
        assert connection.execute("SELECT display_alias FROM room_members LIMIT 1").fetchone()[0]
        assert connection.execute("SELECT content_type FROM messages LIMIT 1").fetchone()[0] == "text"
        assert connection.execute("SELECT nickname FROM sessions LIMIT 1").fetchone()[0] == ""
        connection.execute("SELECT id FROM reports LIMIT 1").fetchone()
        raise RuntimeError("injected migration failure after all DDL and backfill")
    monkeypatch.setattr("app.store.execute_schema", fail_after_changes)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        store.initialize()
    assert fingerprint_legacy(path) == before
    assert store.last_migration_backup is not None
    assert fingerprint_legacy(store.last_migration_backup) == before


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_backup_failure_prevents_any_migration_write(tmp_path, monkeypatch, schema_version):
    directory = tmp_path / "legacy"
    path = legacy_database(directory, schema_version)
    before = fingerprint_legacy(path)
    def fail_backup(*_args):
        raise OSError("injected unavailable backup destination")
    monkeypatch.setattr("app.store.backup_before_migration", fail_backup)
    with pytest.raises(OSError, match="injected unavailable"):
        make_store(directory).initialize()
    assert fingerprint_legacy(path) == before
