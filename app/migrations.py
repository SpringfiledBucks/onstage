"""A backed-up, transactional upgrade. Never use executescript inside its transaction."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from .security import iso_time, new_id


def execute_schema(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("数据库结构文件包含未完成语句。")


def backup_before_migration(
    connection: sqlite3.Connection, data_dir: Path, from_version: str, to_version: str,
) -> Path:
    if connection.in_transaction:
        raise RuntimeError("迁移备份须在写事务开始前完成。")
    if not ((from_version in {"1", "2", "3"} and to_version == "4")
            or (from_version == "4" and to_version == "4-community")):
        raise RuntimeError("未知的数据库迁移备份版本。")
    directory = data_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = iso_time().replace(":", "").replace("-", "").replace(".", "")
    destination = directory / f"onstage-schema{from_version}-before-schema{to_version}-{stamp}-{new_id()[:8]}.sqlite3"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    backup = None
    try:
        backup = sqlite3.connect(destination, timeout=5, isolation_level=None)
        connection.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("迁移前备份完整性检查失败，原库未迁移。")
        version = backup.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if version != (from_version,):
            raise RuntimeError("迁移前备份版本不正确，原库未迁移。")
    except BaseException:
        if backup is not None:
            backup.close()
            backup = None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if backup is not None:
            backup.close()
    destination.chmod(0o600)
    return destination


def migrate_one_to_two(connection: sqlite3.Connection, scenarios: dict[str, dict]) -> None:
    if not connection.in_transaction:
        raise RuntimeError("迁移必须在同一个显式事务内运行。")
    connection.execute(
        "ALTER TABLE rooms ADD COLUMN background TEXT NOT NULL DEFAULT '' CHECK(length(background)<=2000)"
    )
    connection.execute(
        "ALTER TABLE rooms ADD COLUMN announcement TEXT NOT NULL DEFAULT '' CHECK(length(announcement)<=500)"
    )
    connection.execute("ALTER TABLE room_members ADD COLUMN display_alias TEXT NOT NULL DEFAULT ''")
    for room in connection.execute("SELECT id,scenario_id FROM rooms").fetchall():
        background = str(scenarios.get(room["scenario_id"], {}).get("opening", ""))[:2000]
        connection.execute("UPDATE rooms SET background=? WHERE id=?", (background, room["id"]))
        members = connection.execute(
            "SELECT id FROM room_members WHERE room_id=? ORDER BY joined_at,id", (room["id"],)
        ).fetchall()
        for number, member in enumerate(members, 1):
            connection.execute(
                "UPDATE room_members SET display_alias=? WHERE id=?",
                (f"观众 {number}", member["id"]),
            )
    # Historical aliases, session tokens, messages and application rows remain intact.
    # The caller adds the latest new tables only after all column migrations finish.


def migrate_two_to_three(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("迁移必须在同一个显式事务内运行。")
    # Additive columns keep message IDs, seqs and report foreign keys intact.
    connection.execute("ALTER TABLE messages ADD COLUMN sticker_id TEXT")
    connection.execute("ALTER TABLE messages ADD COLUMN sticker_json TEXT")
    connection.execute(
        "ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text' "
        "CHECK(content_type IN ('text','sticker')) "
        "CHECK((content_type='text' AND sticker_id IS NULL AND sticker_json IS NULL) OR "
        "(content_type='sticker' AND kind='speech' AND sticker_id IS NOT NULL "
        "AND length(sticker_id) BETWEEN 1 AND 80 AND sticker_json IS NOT NULL))"
    )


def migrate_three_to_four(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("迁移必须在同一个显式事务内运行。")
    connection.execute(
        "ALTER TABLE sessions ADD COLUMN nickname TEXT NOT NULL DEFAULT '' CHECK(length(nickname)<=24)"
    )
    # Existing identities remain valid and unnamed. Never invent past room events.
