from __future__ import annotations

import json
import re
import secrets
import sqlite3
import unicodedata
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Iterator

from .migrations import (
    backup_before_migration, execute_schema, migrate_one_to_two, migrate_two_to_three,
    migrate_three_to_four,
)
from .security import (
    DomainError, Settings, iso_time, new_id, parse_time, token_hash,
    token_matches, utc_now,
)


CHARACTER_IDS = {
    "mygo.tomori", "mygo.anon", "mygo.rana", "mygo.soyo", "mygo.taki",
    "mujica.uika", "mujica.mutsumi", "mujica.umiri", "mujica.nyamu", "mujica.sakiko",
}


class LegacyStore:
    """Every public operation owns one connection and one short transaction.

    These methods are synchronous: callers run a whole method in one worker
    thread. No SQLite connection, cursor or transaction crosses an await.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.data_dir / "onstage.sqlite3"
        self.catalog: dict = {}
        self.characters: dict[str, dict] = {}
        self.scenarios: dict[str, dict] = {}
        self.stickers: dict[str, dict] = {}
        self.last_migration_backup: Path | None = None

    def _load_catalog(self) -> None:
        self.settings.validate()
        path = Path(__file__).with_name("catalog.json")
        self.catalog = json.loads(path.read_text(encoding="utf-8"))
        self.characters = {item["id"]: item for item in self.catalog["characters"]}
        self.scenarios = {item["id"]: item for item in self.catalog["scenarios"]}
        if set(self.characters) != CHARACTER_IDS or len(self.catalog["characters"]) != 10:
            raise RuntimeError("catalog.json 必须包含约定的十个固定角色。")
        if not self.scenarios or len(self.scenarios) != len(self.catalog["scenarios"]):
            raise RuntimeError("catalog.json 场景不能为空，ID 必须唯一。")
        for scenario in self.scenarios.values():
            ids = scenario["character_ids"]
            if not ids or not set(ids).issubset(CHARACTER_IDS) or len(set(ids)) != len(ids):
                raise RuntimeError("场景角色列表不合法。")
        sticker_rows = self.catalog.setdefault("stickers", [])
        if not isinstance(sticker_rows, list):
            raise RuntimeError("贴纸目录必须是数组。")
        self.stickers = {}
        for sticker in sticker_rows:
            if not isinstance(sticker, dict):
                raise RuntimeError("贴纸目录项不合法。")
            sticker_id, label = sticker.get("id"), sticker.get("label")
            image, character_id = sticker.get("image"), sticker.get("character_id")
            source_category = sticker.get("source_category")
            if (
                not isinstance(sticker_id, str) or not 1 <= len(sticker_id) <= 80
                or sticker_id.strip() != sticker_id or sticker_id in self.stickers
                or not isinstance(label, str) or not label.strip() or len(label) > 80
                or "character_id" not in sticker
                or (character_id is not None and (
                    not isinstance(character_id, str) or character_id not in CHARACTER_IDS
                ))
                or (character_id is None and (
                    not isinstance(source_category, str) or not source_category.strip()
                    or len(source_category) > 40
                ))
                or any(
                    key in sticker and (
                        not isinstance(sticker[key], str) or not sticker[key].strip()
                        or len(sticker[key]) > 80
                    )
                    for key in ("source_pack", "source_category")
                )
                or not isinstance(image, str) or not image.startswith("/static/")
                or any(part in {"", ".", ".."} for part in image[1:].split("/"))
                or any(char in image for char in "?%#\\")
                or any(ord(char) < 32 or ord(char) == 127 for char in image)
                or not isinstance(sticker.get("keywords"), list)
                or any(not isinstance(word, str) for word in sticker["keywords"])
            ):
                raise RuntimeError("贴纸须有唯一 ID、有效人物或独立素材分类与仅本域 /static/ 图片路径。")
            self.stickers[sticker_id] = sticker
    def initialize(self) -> None:
        self._load_catalog()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = self._connect()
        try:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            schema_version = None
            if existing:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                schema_version = row["value"] if row else None
                if schema_version not in {"1", "2", "3", "4"}:
                    raise RuntimeError("数据库结构版本不兼容，未修改原数据库。")
            elif connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise RuntimeError("已有数据库缺少结构版本，拒绝自动覆盖。")
            if schema_version in {"1", "2", "3"}:
                self.last_migration_backup = backup_before_migration(
                    connection, self.settings.data_dir, schema_version, "4",
                )
            elif schema_version == '4' and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'").fetchone():
                self.last_migration_backup = backup_before_migration(
                    connection, self.settings.data_dir, '4', '4-community',
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            if schema_version in {"1", "2", "3"}:
                current = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if current is None or current["value"] != schema_version:
                    raise RuntimeError("备份后数据库版本已变化，停止迁移。")
                if schema_version == "1":
                    self._migrate_one_to_two(connection)
                if schema_version in {"1", "2"}:
                    self._migrate_two_to_three(connection)
                self._migrate_three_to_four(connection)
            execute_schema(connection, Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version','4') "
                "ON CONFLICT(key) DO UPDATE SET value='4'"
            )
            connection.executemany(
                "INSERT OR IGNORE INTO characters(id) VALUES(?)",
                [(character_id,) for character_id in sorted(CHARACTER_IDS)],
            )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("数据库外键检查失败，迁移已回滚。")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.db_path.chmod(0o600)
        from .extension_schema import migrate_sqlite_extensions
        migrate_sqlite_extensions(self.db_path)

    def _migrate_one_to_two(self, connection: sqlite3.Connection) -> None:
        migrate_one_to_two(connection, self.scenarios)

    def _migrate_two_to_three(self, connection: sqlite3.Connection) -> None:
        migrate_two_to_three(connection)

    def _migrate_three_to_four(self, connection: sqlite3.Connection) -> None:
        migrate_three_to_four(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self, *, write: bool = False, room_id: str | None = None,
                     session_token: str | None = None, admission: str | None = None,
                     report_id: str | None = None) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        """SQLite owns only operation-local connections."""

    def ready(self) -> dict:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or row["value"] != "4":
                raise DomainError(503, "数据库结构尚未就绪。")
            from .extension_schema import REVISION
            revision = connection.execute('SELECT version_num FROM alembic_version').fetchone()
            if revision is None or revision[0] != REVISION:
                raise DomainError(503, '账户与房间扩展尚未迁移。')
            connection.execute("SELECT id FROM rooms LIMIT 1").fetchone()
            return {"status": "ready", "schema_version": 4, "community_revision": REVISION}

    def _session(
        self,
        connection: sqlite3.Connection,
        token: str | None,
        csrf: str | None = None,
        *,
        require_csrf: bool = False,
    ) -> sqlite3.Row:
        if not token or len(token) > 256:
            raise DomainError(401, "匿名会话尚未建立，请重新进入。")
        session = connection.execute(
            'SELECT * FROM sessions WHERE token_hash=:p0', {'p0': token_hash(token)}
        ).fetchone()
        if session is None or session["revoked_at"] is not None:
            raise DomainError(401, "会话已失效，请重新进入。")
        try:
            expired = parse_time(session["expires_at"]) <= utc_now()
        except (ValueError, TypeError):
            expired = True
        if expired:
            raise DomainError(401, "会话已到期，请重新进入。")
        if require_csrf and (not csrf or not token_matches(csrf, session["csrf_token"])):
            raise DomainError(403, "CSRF 校验失败，请刷新页面后重试。")
        return session

    @staticmethod
    def _check_rate(
        connection: sqlite3.Connection, action: str, key: str, limit: int, seconds: int,
    ) -> None:
        cutoff = iso_time(utc_now() - timedelta(seconds=seconds))
        count = connection.execute(
            'SELECT COUNT(*) FROM rate_events WHERE action=:p0 AND subject_key=:p1 AND created_at>=:p2',
            {'p0': action, 'p1': key, 'p2': cutoff},
        ).fetchone()[0]
        if count >= limit:
            raise DomainError(429, "操作过于频繁，请稍后再试。")

    @staticmethod
    def _record_rate(connection: sqlite3.Connection, action: str, key: str) -> None:
        connection.execute(
            'DELETE FROM rate_events WHERE created_at<:p0', {'p0': iso_time(utc_now() - timedelta(days=1))}
        )
        connection.execute(
            'INSERT INTO rate_events(action,subject_key,created_at) VALUES(:p0,:p1,:p2)', {'p0': action, 'p1': key, 'p2': iso_time()}
        )

    @staticmethod
    def _normalize_nickname(nickname: str | None) -> str:
        if nickname is None:
            return ""
        if not isinstance(nickname, str):
            raise DomainError(422, "昵称须为文字。")
        if any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in nickname
        ):
            raise DomainError(422, "昵称最多 24 字，不能包含换行或控制字符。")
        nickname = nickname.strip()
        if len(nickname) > 24:
            raise DomainError(422, "昵称最多 24 字，不能包含换行或控制字符。")
        return nickname

    def create_session(
        self, token: str | None = None, source: str = "unknown",
        nickname: str | None = None, csrf: str | None = None,
    ) -> dict:
        nickname = self._normalize_nickname(nickname)
        with self._transaction(write=True, session_token=token, admission='sessions') as connection:
            if token:
                try:
                    existing = self._session(connection, token)
                except DomainError as error:
                    if error.status_code != 401:
                        raise
                else:
                    changed_rooms = []
                    if nickname and nickname != existing["nickname"]:
                        self._session(connection, token, csrf, require_csrf=True)
                        connection.execute('UPDATE sessions SET nickname=:p0 WHERE id=:p1', {'p0': nickname, 'p1': existing["id"]})
                        changed_rooms = [row["room_id"] for row in connection.execute(
                            "SELECT room_id FROM room_members WHERE session_id=:p0 AND status='active'",
                            {'p0': existing["id"]},
                        ).fetchall()]
                        for room_id in changed_rooms:
                            self._bump(connection, room_id)
                    else:
                        nickname = existing["nickname"]
                    return {
                        "token": None, "csrf_token": existing["csrf_token"], "resumed": True,
                        "nickname": nickname or None, "requires_nickname": not bool(nickname),
                        "changed_rooms": changed_rooms,
                    }
            source_key = token_hash(source)
            self._check_rate(connection, "session", source_key, self.settings.session_limit_per_minute, 60)
            self._check_rate(connection, "session", source_key, self.settings.session_limit_per_hour, 3600)
            now = utc_now()
            active = connection.execute(
                'SELECT COUNT(*) FROM sessions s WHERE revoked_at IS NULL AND expires_at>:p0 '
                'AND NOT EXISTS(SELECT 1 FROM users u WHERE u.principal_session_id=s.id)', {'p0': iso_time(now)}
            ).fetchone()[0]
            if active >= self.settings.max_active_sessions:
                raise DomainError(429, "当前体验人数已满，请稍后再来。")
            token = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            connection.execute(
                'INSERT INTO sessions(id,token_hash,csrf_token,created_at,expires_at,nickname) VALUES(:p0,:p1,:p2,:p3,:p4,:p5)',
                {'p0': new_id(), 'p1': token_hash(token), 'p2': csrf, 'p3': iso_time(now), 'p4': iso_time(now + timedelta(seconds=self.settings.session_ttl_seconds)), 'p5': nickname},
            )
            self._record_rate(connection, "session", source_key)
        return {
            "token": token, "csrf_token": csrf, "resumed": False,
            "nickname": nickname or None, "requires_nickname": not bool(nickname), "changed_rooms": [],
        }

    def get_session(self, token: str | None) -> dict:
        with self._transaction() as connection:
            session = self._session(connection, token)
            return {
                "authenticated": True,
                "csrf_token": session["csrf_token"],
                "version": self.settings.version,
                "nickname": session["nickname"] or None,
                "requires_nickname": not bool(session["nickname"]),
            }

    def logout(self, token: str | None, csrf: str | None) -> str:
        with self._transaction(write=True, session_token=token) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            connection.execute(
                'UPDATE sessions SET revoked_at=:p0 WHERE id=:p1',
                {'p0': iso_time(), 'p1': session["id"]},
            )
            return session["id"]

    def get_catalog(self, token: str | None) -> dict:
        with self._transaction() as connection:
            self._session(connection, token)
            return self.catalog

    @staticmethod
    def _room(connection: sqlite3.Connection, room_id: str) -> sqlite3.Row:
        room = connection.execute('SELECT * FROM rooms WHERE id=:p0', {'p0': room_id}).fetchone()
        if room is None:
            raise DomainError(404, "房间不存在。")
        return room

    @staticmethod
    def _member(
        connection: sqlite3.Connection, room_id: str, session_id: str,
        *, required: bool = True,
    ) -> sqlite3.Row | None:
        member = connection.execute(
            "SELECT * FROM room_members WHERE room_id=:p0 AND session_id=:p1 AND status='active' AND NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=room_members.room_id AND b.session_id=room_members.session_id)",
            {'p0': room_id, 'p1': session_id},
        ).fetchone()
        if member is None and required:
            raise DomainError(403, "请先加入本房间。")
        return member

    @staticmethod
    def _not_banned(connection: sqlite3.Connection, room_id: str, session_id: str) -> None:
        if connection.execute(
            'SELECT 1 FROM room_bans WHERE room_id=:p0 AND session_id=:p1', {'p0': room_id, 'p1': session_id}
        ).fetchone():
            raise DomainError(403, "你已被房主移出，本次匿名身份不能再次进入该房间。")

    def _host(
        self, connection: sqlite3.Connection, room: sqlite3.Row, session_id: str,
    ) -> sqlite3.Row:
        if room["host_session_id"] != session_id:
            raise DomainError(403, "只有本房房主可以执行此操作。")
        return self._member(connection, room["id"], session_id)

    @staticmethod
    def _backstage_allowed(room: sqlite3.Row, member: sqlite3.Row) -> bool:
        return bool(member["character_id"] or room["host_session_id"] == member["session_id"])

    @staticmethod
    def _claim_allowed(room: sqlite3.Row, member: sqlite3.Row) -> bool:
        return (
            member["character_id"] is None
            and room["status"] != "ended"
            and (room["status"] == "preparing" or bool(room["allow_join"]))
        )

    def _validate_character(
        self, connection: sqlite3.Connection, room: sqlite3.Row,
        member: sqlite3.Row, character_id: str,
    ) -> None:
        if not self._claim_allowed(room, member):
            raise DomainError(409, "当前状态不能选择空角色，请重新同步房间。")
        if character_id not in self.scenarios[room["scenario_id"]]["character_ids"]:
            raise DomainError(409, "该角色不在本场角色范围内。")
        if member["first_character_id"] not in {None, character_id}:
            raise DomainError(409, "同一场只能重新选择最初扮演的角色。")
        occupied = connection.execute(
            'SELECT id FROM room_members WHERE room_id=:p0 AND character_id=:p1',
            {'p0': room["id"], 'p1': character_id},
        ).fetchone()
        if occupied is not None:
            raise DomainError(409, "该角色已被其他成员占用。")

    @staticmethod
    def _message_json(message: sqlite3.Row, connection: sqlite3.Connection) -> dict:
        result = {key: message[key] for key in (
            "id", "seq", "channel", "member_id", "character_id", "author",
            "text", "created_at", "kind", "content_type", "sticker_id", "client_message_id",
        )}
        result["sticker"] = json.loads(message["sticker_json"]) if message["sticker_json"] is not None else None
        if message["member_id"] and message["character_id"] is None:
            member = connection.execute(
                'SELECT alias,display_alias FROM room_members WHERE id=:p0', {'p0': message["member_id"]}
            ).fetchone()
            if member and member["alias"].startswith("旅人·") and message["author"] == member["alias"]:
                result["author"] = member["display_alias"]
        return result

    def _snapshot(
        self, connection: sqlite3.Connection, room: sqlite3.Row,
        session_id: str, online: set[str], *, member_rows=None, host_row=None,
    ) -> dict:
        rows = member_rows if member_rows is not None else connection.execute(
            "SELECT m.*, s.expires_at, s.revoked_at, s.nickname FROM room_members m JOIN sessions s ON s.id=m.session_id WHERE m.room_id=:p0 AND m.status='active' ORDER BY m.joined_at,m.id",
            {'p0': room["id"]},
        ).fetchall()
        members = []
        own_member = None
        now = utc_now()
        for row in rows:
            valid_online = False
            if row["id"] in online and row["revoked_at"] is None:
                try:
                    valid_online = parse_time(row["expires_at"]) > now
                except (ValueError, TypeError):
                    pass
            members.append({
                "id": row["id"], "alias": row["display_alias"],
                "nickname": row["nickname"] or None,
                "display_name": self.characters[row["character_id"]]["name"] if row["character_id"] else row["nickname"] or row["display_alias"],
                "character_id": row["character_id"],
                "is_host": row["session_id"] == room["host_session_id"],
                "online": valid_online,
            })
            if row["session_id"] == session_id:
                own_member = row
        host = host_row if member_rows is not None else connection.execute(
            'SELECT id FROM room_members WHERE room_id=:p0 AND session_id=:p1',
            {'p0': room["id"], 'p1': room["host_session_id"]},
        ).fetchone()
        me = None
        if own_member is not None:
            is_host = session_id == room["host_session_id"]
            me = {
                "member_id": own_member["id"], "is_host": is_host,
                "nickname": own_member["nickname"] or None,
                "character_id": own_member["character_id"],
                "first_character_id": own_member["first_character_id"],
                "can_stage": room["status"] == "live" and (
                    is_host or own_member["character_id"] is not None
                ),
                "can_backstage": self._backstage_allowed(room, own_member),
                "can_claim": self._claim_allowed(room, own_member),
                "can_manage_reports": is_host,
            }
        return {
            "id": room["id"], "title": room["title"],
            "background": room["background"], "announcement": room["announcement"],
            "scenario_id": room["scenario_id"], "status": room["status"],
            "allow_join": bool(room["allow_join"]), "revision": room["revision"],
            "created_at": room["created_at"],
            "host_member_id": host["id"] if host else None,
            "online_count": sum(member["online"] for member in members),
            "members": members, "me": me,
        }

    def get_room(self, token: str | None, room_id: str, online: set[str]) -> dict:
        with self._transaction() as connection:
            session = self._session(connection, token)
            room = self._room(connection, room_id)
            self._not_banned(connection, room_id, session["id"])
            return {"room": self._snapshot(connection, room, session["id"], online)}

    def list_rooms(self, token: str | None, online: dict[str, set[str]],
                   limit: int = 50, cursor: str | None = None, filter: str = "all") -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or filter not in {"all", "active", "ended", "mine"}:
            raise DomainError(422, "房间分页参数不合法。")
        with self._transaction() as connection:
            session = self._session(connection, token)
            scope = token_hash(session["id"])[:16]
            conditions = ["NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=r.id AND b.session_id=:sid)"]
            conditions.append("(COALESCE((SELECT access FROM room_options WHERE room_id=r.id),'public')='public' OR r.host_session_id=:sid OR EXISTS(SELECT 1 FROM room_members own WHERE own.room_id=r.id AND own.session_id=:sid) OR EXISTS(SELECT 1 FROM room_access_grants g WHERE g.room_id=r.id AND g.session_id=:sid))")
            if filter != 'mine':
                conditions.append("COALESCE((SELECT hidden FROM room_options WHERE room_id=r.id),0)=0")
            params = {"sid": session["id"], "limit": limit + 1}
            if filter == "active":
                conditions.append("r.status<>'ended'")
            elif filter == "ended":
                conditions.append("r.status='ended'")
            elif filter == "mine":
                conditions.append("EXISTS(SELECT 1 FROM room_members own WHERE own.room_id=r.id AND own.session_id=:sid AND own.status='active')")
            if cursor is not None:
                parts = cursor.split("|") if isinstance(cursor, str) and len(cursor) <= 200 else []
                if len(parts) != 4 or parts[:2] != [filter, scope] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", parts[2]) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", parts[3]):
                    raise DomainError(422, "房间游标不合法或不属于当前筛选和身份。")
                try:
                    parse_time(parts[2])
                except ValueError:
                    raise DomainError(422, "房间游标时间不合法。") from None
                params.update(at=parts[2], rid=parts[3])
                conditions.append("(r.created_at<:at OR (r.created_at=:at AND r.id>:rid))")
            rooms = connection.execute(
                "SELECT r.*,o.access AS list_access,o.hidden AS list_hidden,o.share_enabled AS list_share_enabled,o.cast_scenario_id AS list_cast_scenario_id,o.recruitment_json AS list_recruitment_json,(SELECT id FROM room_members h WHERE h.room_id=r.id AND h.session_id=r.host_session_id) AS host_member_id FROM rooms r LEFT JOIN room_options o ON o.room_id=r.id WHERE " + " AND ".join(conditions) + " ORDER BY r.created_at DESC,r.id ASC LIMIT :limit", params,
            ).fetchall()
            has_more = len(rooms) > limit
            rooms = rooms[:limit]
            by_room = {room["id"]: [] for room in rooms}
            if rooms:
                ids = {f"r{i}": room["id"] for i, room in enumerate(rooms)}
                members = connection.execute(
                    "SELECT m.*,s.expires_at,s.revoked_at,s.nickname FROM room_members m JOIN sessions s ON s.id=m.session_id WHERE m.status='active' AND m.room_id IN (" + ",".join(":" + key for key in ids) + ") AND NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=m.room_id AND b.session_id=m.session_id) ORDER BY m.joined_at,m.id", ids,
                ).fetchall()
                for member in members:
                    by_room[member["room_id"]].append(member)
            return {"rooms": [
                self._snapshot(connection, room, session["id"], online.get(room["id"], set()), member_rows=by_room[room["id"]], host_row={"id": room["host_member_id"]} if room["host_member_id"] else None,
                               options_override={'access': room['list_access'] or 'public', 'hidden': room['list_hidden'] or 0,
                                                 'share_enabled': room['list_share_enabled'] if room['list_share_enabled'] is not None else 1,
                                                 'cast_scenario_id': room['list_cast_scenario_id'] or room['scenario_id'],
                                                 'recruitment_json': room['list_recruitment_json'] or '[]'})
                for room in rooms
            ], "has_more": has_more, "next_cursor": "|".join((filter, scope, rooms[-1]["created_at"], rooms[-1]["id"])) if has_more else None}

    @staticmethod
    def _new_member(connection: sqlite3.Connection, room_id: str, session_id: str) -> str:
        member_id = new_id()
        number = connection.execute('SELECT COUNT(*) FROM room_members WHERE room_id=:p0', {'p0': room_id}).fetchone()[0] + 1
        alias = f"观众 {number}"
        connection.execute(
            'INSERT INTO room_members(id,room_id,session_id,alias,display_alias,joined_at) VALUES(:p0,:p1,:p2,:p3,:p4,:p5)',
            {'p0': member_id, 'p1': room_id, 'p2': session_id, 'p3': alias, 'p4': alias, 'p5': iso_time()},
        )
        return member_id

    @staticmethod
    def _bump(connection: sqlite3.Connection, room_id: str) -> None:
        connection.execute(
            'UPDATE rooms SET revision=revision+1,updated_at=:p0 WHERE id=:p1',
            {'p0': iso_time(), 'p1': room_id},
        )

    def _member_event(
        self, connection: sqlite3.Connection, room: sqlite3.Row, member: sqlite3.Row,
        action: str, character_id: str | None = None,
    ) -> None:
        # Reading or leaving an archive must not keep extending its performance.
        if room["status"] == "ended":
            return
        session = connection.execute('SELECT nickname FROM sessions WHERE id=:p0', {'p0': member["session_id"]}).fetchone()
        nickname = session["nickname"] or member["display_alias"]
        role = character_id or member["character_id"]
        label = f"{nickname}（{self.characters[role]['name']}）" if role else nickname
        if action == "join":
            text = f"{nickname} 以观众身份进入了房间。"
        elif action == "claim":
            text = f"{nickname} 作为 {self.characters[character_id]['name']} 入场。"
        elif action == "leave":
            text = f"{label}主动离开了房间。"
        elif action == "remove":
            text = f"{label}已被房主移出房间。"
        else:
            raise ValueError("Unknown member event")
        # Labels are frozen in stored text. Later nickname/role changes never
        # rebuild this history, and only server-side transitions call this.
        self._insert_message(
            connection, room["id"], "stage", text=text,
            member_id=None, character_id=None, author="系统", kind="system",
        )

    def create_room(
        self, token: str | None, csrf: str | None, scenario_id: str, title: str | None,
        background: str | None = None, announcement: str = "", source: str = "unknown",
        *, access: str = 'public', password: str | None = None,
    ) -> str:
        if access not in {'public', 'password', 'invite'}:
            raise DomainError(422, '访问方式不合法。')
        encoded = None
        if access == 'password':
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise DomainError(422, '房间密码须为8–128字符。')
            from .accounts import PASSWORDS
            encoded = PASSWORDS.hash(password)
        elif password is not None:
            raise DomainError(422, '仅密码房可设置密码。')
        with self._transaction(write=True, session_token=token, admission='rooms') as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            if access != 'public':
                self._account(connection, session)
            scenario = self.scenarios.get(scenario_id)
            if scenario is None:
                raise DomainError(400, "场景不存在。")
            title = (title or "").strip() or scenario["title"]
            if len(title) > 80:
                raise DomainError(422, "房间名称最多 80 字。")
            background = str(scenario.get("opening", "")) if background is None else background
            if len(background) > 2000 or len(announcement) > 500:
                raise DomainError(422, "故事背景最多 2000 字，公告最多 500 字。")
            self._check_rate(connection, "room_session", session["id"], self.settings.room_limit_per_hour, 3600)
            source_key = token_hash(source)
            self._check_rate(connection, "room_source", source_key, self.settings.room_ip_limit_per_hour, 3600)
            count = connection.execute(
                "SELECT COUNT(*) FROM rooms WHERE status!='ended'"
            ).fetchone()[0]
            if count >= self.settings.max_active_rooms:
                raise DomainError(429, "当前开放房间已满，请先结束旧场次。")
            owned = connection.execute(
                "SELECT COUNT(*) FROM rooms WHERE host_session_id=:p0 AND status!='ended'", {'p0': session["id"]}
            ).fetchone()[0]
            if owned >= self.settings.max_active_rooms_per_session:
                raise DomainError(429, "你已有未结束房间，请先结束旧场次。")
            room_id = new_id()
            now = iso_time()
            connection.execute(
                "INSERT INTO rooms(id,title,scenario_id,background,announcement,status,host_session_id,created_at,updated_at) VALUES(:p0,:p1,:p2,:p3,:p4,'preparing',:p5,:p6,:p7)",
                {'p0': room_id, 'p1': title, 'p2': scenario_id, 'p3': background, 'p4': announcement, 'p5': session["id"], 'p6': now, 'p7': now},
            )
            self._new_member(connection, room_id, session["id"])
            connection.execute('INSERT INTO room_options(room_id,access,password_hash,share_enabled,cast_scenario_id) '
                               'VALUES(:r,:access,:password,:share,:scene)',
                               {'r': room_id, 'access': access, 'password': encoded, 'share': int(access == 'public'), 'scene': scenario_id})
            self._member_event(
                connection, self._room(connection, room_id),
                self._member(connection, room_id, session["id"]), "join",
            )
            self._record_rate(connection, "room_session", session["id"])
            self._record_rate(connection, "room_source", source_key)
            return room_id

    def join(self, token: str | None, csrf: str | None, room_id: str) -> None:
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            self._not_banned(connection, room_id, session["id"])
            member = connection.execute(
                'SELECT * FROM room_members WHERE room_id=:p0 AND session_id=:p1',
                {'p0': room_id, 'p1': session["id"]},
            ).fetchone()
            if member is None or member["status"] != "active":
                active = connection.execute(
                    "SELECT COUNT(*) FROM room_members WHERE room_id=:p0 AND status='active'", {'p0': room_id}
                ).fetchone()[0]
                if active >= self.settings.max_room_members:
                    raise DomainError(429, "本房人数已满，请稍后再来。")
            if member is None:
                total = connection.execute('SELECT COUNT(*) FROM room_members WHERE room_id=:p0', {'p0': room_id}).fetchone()[0]
                if total >= self.settings.max_room_identities:
                    raise DomainError(429, "本房已达到本次体验的身份容量，请进入其他房间。")
                self._new_member(connection, room_id, session["id"])
            elif member["status"] != "active":
                connection.execute(
                    "UPDATE room_members SET status='active',character_id=NULL,left_at=NULL,joined_at=:p0 WHERE id=:p1", {'p0': iso_time(), 'p1': member["id"]},
                )
            else:
                return
            self._bump(connection, room_id)
            self._member_event(
                connection, room, self._member(connection, room_id, session["id"]), "join",
            )

    def leave(self, token: str | None, csrf: str | None, room_id: str) -> str | None:
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            if room["host_session_id"] == session["id"] and room["status"] != "ended":
                raise DomainError(409, "房主须先结束本场，再离开房间。")
            member = self._member(connection, room_id, session["id"], required=False)
            if member is None:
                return None
            now = iso_time()
            connection.execute(
                "UPDATE room_members SET status='left',character_id=NULL,left_at=:p0 WHERE id=:p1",
                {'p0': now, 'p1': member["id"]},
            )
            connection.execute(
                "UPDATE role_requests SET status='cancelled',decided_at=:p0 WHERE room_id=:p1 AND member_id=:p2 AND status='pending'",
                {'p0': now, 'p1': room_id, 'p2': member["id"]},
            )
            self._bump(connection, room_id)
            self._member_event(connection, room, member, "leave")
            return member["id"]

    def claim_character(
        self, token: str | None, csrf: str | None, room_id: str, character_id: str,
    ) -> None:
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            member = self._member(connection, room_id, session["id"])
            if room["status"] == "ended":
                raise DomainError(409, "本场已经结束，不能再选择角色。")
            if member["character_id"] == character_id:
                return
            self._validate_character(connection, room, member, character_id)
            try:
                connection.execute(
                    'UPDATE room_members SET character_id=:p0,first_character_id=COALESCE(first_character_id,:p1) WHERE id=:p2',
                    {'p0': character_id, 'p1': character_id, 'p2': member["id"]},
                )
            except (sqlite3.IntegrityError, SQLAlchemyIntegrityError) as error:
                raise DomainError(409, "该角色已被其他成员占用。") from error
            self._bump(connection, room_id)
            self._member_event(connection, room, member, "claim", character_id)

    def update_settings(
        self, token: str | None, csrf: str | None, room_id: str,
        background: str, announcement: str,
    ) -> None:
        if len(background) > 2000 or len(announcement) > 500:
            raise DomainError(422, "故事背景最多 2000 字，公告最多 500 字。")
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            self._host(connection, room, session["id"])
            if room["status"] == "ended":
                raise DomainError(409, "本场已经结束，背景与公告保留为只读。")
            if room["background"] == background and room["announcement"] == announcement:
                return
            connection.execute(
                'UPDATE rooms SET background=:p0,announcement=:p1 WHERE id=:p2', {'p0': background, 'p1': announcement, 'p2': room_id}
            )
            self._bump(connection, room_id)

    def remove_member(
        self, token: str | None, csrf: str | None, room_id: str, member_id: str,
    ) -> str:
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            host = self._host(connection, room, session["id"])
            if room["status"] == "ended":
                raise DomainError(409, "本场已经结束，成员记录保留为只读。")
            target = connection.execute(
                'SELECT * FROM room_members WHERE room_id=:p0 AND id=:p1', {'p0': room_id, 'p1': member_id}
            ).fetchone()
            if target is None:
                raise DomainError(404, "本房没有该成员。")
            if target["session_id"] == session["id"]:
                raise DomainError(409, "房主不能移出自己，请先结束并离房。")
            if connection.execute(
                'SELECT 1 FROM room_bans WHERE room_id=:p0 AND session_id=:p1', {'p0': room_id, 'p1': target["session_id"]}
            ).fetchone():
                return member_id
            now = iso_time()
            connection.execute(
                'INSERT INTO room_bans(room_id,session_id,removed_by_member_id,created_at) VALUES(:p0,:p1,:p2,:p3)',
                {'p0': room_id, 'p1': target["session_id"], 'p2': host["id"], 'p3': now},
            )
            connection.execute(
                "UPDATE room_members SET status='left',character_id=NULL,left_at=:p0 WHERE id=:p1", {'p0': now, 'p1': member_id}
            )
            connection.execute(
                "UPDATE role_requests SET status='cancelled',decided_at=:p0 WHERE room_id=:p1 AND member_id=:p2 AND status='pending'",
                {'p0': now, 'p1': room_id, 'p2': member_id},
            )
            self._bump(connection, room_id)
            self._member_event(connection, room, target, "remove")
            return member_id

    def _insert_message(
        self, connection: sqlite3.Connection, room_id: str, channel: str,
        *, text: str, member_id: str | None, character_id: str | None,
        author: str, kind: str, client_message_id: str | None = None,
        content_type: str = "text", sticker_id: str | None = None, sticker: dict | None = None,
    ) -> dict:
        column = {"stage": "stage_seq", "backstage": "backstage_seq"}[channel]
        connection.execute(f'UPDATE rooms SET {column}={column}+1 WHERE id=:p0', {'p0': room_id})
        seq = connection.execute(f'SELECT {column} FROM rooms WHERE id=:p0', {'p0': room_id}).fetchone()[0]
        message_id = new_id()
        connection.execute(
            'INSERT INTO messages(id,room_id,seq,channel,member_id,character_id,author,text,created_at,kind,client_message_id,content_type,sticker_id,sticker_json) VALUES(:p0,:p1,:p2,:p3,:p4,:p5,:p6,:p7,:p8,:p9,:p10,:p11,:p12,:p13)',
            {'p0': message_id, 'p1': room_id, 'p2': seq, 'p3': channel, 'p4': member_id, 'p5': character_id, 'p6': author, 'p7': text, 'p8': iso_time(), 'p9': kind, 'p10': client_message_id, 'p11': content_type, 'p12': sticker_id, 'p13': json.dumps(sticker, ensure_ascii=False, separators=(",", ":")) if sticker is not None else None},
        )
        return self._message_json(connection.execute(
            'SELECT * FROM messages WHERE id=:p0', {'p0': message_id}
        ).fetchone(), connection)

    def control(
        self, token: str | None, csrf: str | None, room_id: str, action: str,
    ) -> None:
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            self._host(connection, room, session["id"])
            status = room["status"]
            next_status = status
            next_join = bool(room["allow_join"])
            system_text = ""
            if action == "start":
                if status == "live":
                    return
                if status != "preparing":
                    raise DomainError(409, "只有准备中的场次可以开场；暂停后请使用继续。")
                actors = connection.execute(
                    "SELECT COUNT(*) FROM room_members WHERE room_id=:p0 AND status='active' AND character_id IS NOT NULL", {'p0': room_id},
                ).fetchone()[0]
                if actors < 2:
                    raise DomainError(409, "至少需要两名已选角色的演员才能开场。")
                next_status = "live"
                system_text = "本场演出开始。\n" + room["background"]
            elif action == "pause":
                if status == "paused":
                    return
                if status != "live":
                    raise DomainError(409, "只有演出中的场次可以暂停。")
                next_status, system_text = "paused", "主持人暂停了舞台发言，可在后台继续讨论。"
            elif action == "resume":
                if status == "live":
                    return
                if status != "paused":
                    raise DomainError(409, "只有暂停中的场次可以继续。")
                next_status, system_text = "live", "主持人继续了本场演出。"
            elif action == "end":
                if status == "ended":
                    return
                next_status, next_join = "ended", False
                system_text = "本场演出已结束，记录保留供本房成员阅读。"
                connection.execute(
                    "UPDATE role_requests SET status='cancelled',decided_at=:p0 WHERE room_id=:p1 AND status='pending'", {'p0': iso_time(), 'p1': room_id},
                )
            elif action in {"open_join", "close_join"}:
                if status == "ended":
                    raise DomainError(409, "已结束的场次不能修改入场规则。")
                next_join = action == "open_join"
                if next_join == bool(room["allow_join"]):
                    return
                system_text = "主持人开放了中途选角。" if next_join else "主持人关闭了中途选角。"
            else:
                raise DomainError(422, "未知主持操作。")
            connection.execute(
                'UPDATE rooms SET status=:p0,allow_join=:p1,revision=revision+1,updated_at=:p2 WHERE id=:p3',
                {'p0': next_status, 'p1': int(next_join), 'p2': iso_time(), 'p3': room_id},
            )
            self._insert_message(
                connection, room_id, "stage", text=system_text,
                member_id=None, character_id=None, author="系统", kind="system",
            )

    def send_message(
        self, token: str | None, csrf: str | None, room_id: str,
        channel: str, text: str, client_message_id: str,
        content_type: str = "text", sticker_id: str | None = None,
    ) -> dict:
        if channel not in {"stage", "backstage"}:
            raise DomainError(422, "未知消息频道。")
        sticker = None
        if content_type == "text":
            # Preserve exact RP formatting and text idempotency.
            if sticker_id is not None:
                raise DomainError(422, "文字消息不能同时包含贴纸 ID。")
            if not text.strip() or len(text) > 2000:
                raise DomainError(422, "正文须为 1—2000 字的非空文字。")
        elif content_type == "sticker":
            if text != "":
                raise DomainError(422, "贴纸消息不能同时包含文字。")
            if not isinstance(sticker_id, str) or not 1 <= len(sticker_id) <= 80:
                raise DomainError(422, "贴纸 ID 长度须为 1—80 字符。")
        else:
            raise DomainError(422, "未知消息内容类型。")
        if not 8 <= len(client_message_id) <= 80:
            raise DomainError(422, "客户端消息 ID 长度须为 8—80 字符。")
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            member = self._member(connection, room_id, session["id"])
            backstage_allowed = self._backstage_allowed(room, member)
            if channel == "backstage" and not backstage_allowed:
                raise DomainError(403, "后台仅向房主和现任演员开放。")
            existing = connection.execute(
                'SELECT * FROM messages WHERE room_id=:p0 AND member_id=:p1 AND client_message_id=:p2',
                {'p0': room_id, 'p1': member["id"], 'p2': client_message_id},
            ).fetchone()
            if existing is not None:
                if (
                    existing["channel"] != channel or existing["content_type"] != content_type
                    or existing["sticker_id"] != sticker_id
                    or (content_type == "text" and existing["text"] != text)
                ):
                    raise DomainError(409, "同一客户端消息 ID 已用于不同的内容类型、内容或频道。")
                return {"message": self._message_json(existing, connection)}
            if room["status"] == "ended":
                raise DomainError(409, "本场已经结束，不能继续发言。")
            if channel == "stage":
                if not backstage_allowed:
                    raise DomainError(403, "观众不能在舞台发言，请先选择空角色。")
                if room["status"] != "live":
                    raise DomainError(409, "舞台尚未开场或已暂停，可在后台讨论。")
            if content_type == "sticker":
                # Previously committed retries use their saved snapshot even if
                # the sticker was retired. Only new messages require a current ID.
                if sticker_id not in self.stickers:
                    raise DomainError(422, "贴纸 ID 不在当前目录中。")
                sticker = {key: self.stickers[sticker_id][key] for key in ("id", "label", "character_id", "image")}
            cutoff = iso_time(utc_now() - timedelta(seconds=60))
            count = connection.execute(
                "SELECT COUNT(*) FROM messages msg JOIN room_members member ON member.id=msg.member_id WHERE member.session_id=:p0 AND msg.kind='speech' AND msg.created_at>=:p1",
                {'p0': session["id"], 'p1': cutoff},
            ).fetchone()[0]
            if count >= self.settings.message_limit_per_minute:
                raise DomainError(429, "发言过于频繁，请稍后再试。")
            character_id = member["character_id"]
            author = self.characters[character_id]["name"] if character_id else session["nickname"] or "主持人"
            message = self._insert_message(
                connection, room_id, channel, text="[贴纸] " + sticker["label"] if sticker else text,
                member_id=member["id"],
                character_id=character_id, author=author, kind="speech",
                client_message_id=client_message_id, content_type=content_type,
                sticker_id=sticker_id, sticker=sticker,
            )
            return {"message": message}

    def _page(
        self, connection: sqlite3.Connection, room_id: str,
        channel: str, after: int, limit: int,
    ) -> dict:
        rows = connection.execute(
            'SELECT * FROM messages WHERE room_id=:p0 AND channel=:p1 AND seq>:p2 ORDER BY seq LIMIT :p3',
            {'p0': room_id, 'p1': channel, 'p2': after, 'p3': limit + 1},
        ).fetchall()
        selected = rows[:limit]
        return {
            "messages": [self._message_json(row, connection) for row in selected],
            "cursor": selected[-1]["seq"] if selected else after,
            "has_more": len(rows) > limit,
        }

    def get_messages(
        self, token: str | None, room_id: str, channel: str, after: int, limit: int,
    ) -> dict:
        with self._transaction() as connection:
            session = self._session(connection, token)
            room = self._room(connection, room_id)
            member = self._member(connection, room_id, session["id"])
            if channel == "backstage" and not self._backstage_allowed(room, member):
                raise DomainError(403, "后台仅向房主和现任演员开放。")
            return self._page(connection, room_id, channel, after, limit)

    def sync(
        self, token: str | None, room_id: str, after_stage: int,
        after_backstage: int, online: set[str],
    ) -> dict:
        with self._transaction() as connection:
            session = self._session(connection, token)
            room = self._room(connection, room_id)
            member = self._member(connection, room_id, session["id"])
            stage = self._page(connection, room_id, "stage", after_stage, 100)
            backstage = {"messages": [], "cursor": after_backstage, "has_more": False}
            if self._backstage_allowed(room, member):
                backstage = self._page(connection, room_id, "backstage", after_backstage, 100)
            return {
                "room": self._snapshot(connection, room, session["id"], online),
                "messages": {"stage": stage["messages"], "backstage": backstage["messages"]},
                "cursors": {"stage": stage["cursor"], "backstage": backstage["cursor"]},
                "has_more": {"stage": stage["has_more"], "backstage": backstage["has_more"]},
            }

    def _report_json(
        self, connection: sqlite3.Connection, report: sqlite3.Row,
        session_id: str | None, *, operator: bool = False,
    ) -> dict:
        room = self._room(connection, report["room_id"])
        member = self._member(connection, room["id"], session_id, required=False) if session_id else None
        is_host = bool(member and room["host_session_id"] == session_id)
        own = report["reporter_session_id"] == session_id
        snapshot = json.loads(report["snapshot_json"])
        redacted = bool(
            not operator and snapshot.get("message", {}).get("channel") == "backstage"
            and (member is None or not self._backstage_allowed(room, member))
        )
        result = {key: report[key] for key in (
            "id", "seq", "room_id", "target_type", "message_id", "reason", "details", "status",
            "created_at", "resolved_at", "resolution_note", "resolved_by",
        )}
        result.update({
            "against_host": bool(report["against_host"]), "is_own": own,
            "can_resolve": report["status"] == "open" and (operator or (is_host and not report["against_host"])),
            "snapshot": None if redacted else snapshot, "snapshot_redacted": redacted,
        })
        if operator:
            result.update({
                "reporter_member_id": report["reporter_member_id"],
                "target_member_id": report["target_member_id"], "operator_label": report["operator_label"],
            })
        return result

    def create_report(
        self, token: str | None, csrf: str | None, room_id: str,
        message_id: str | None, reason: str, details: str,
    ) -> dict:
        if reason not in {"harassment", "spam", "unsafe", "other"} or len(details) > 1000:
            raise DomainError(422, "举报原因或说明不合法。")
        with self._transaction(write=True, session_token=token, room_id=room_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            reporter = self._member(connection, room_id, session["id"])
            host = connection.execute(
                'SELECT id FROM room_members WHERE room_id=:p0 AND session_id=:p1', {'p0': room_id, 'p1': room["host_session_id"]}
            ).fetchone()
            target_member_id = host["id"] if host else None
            against_host = True
            target_type, target_key = "room", "room"
            if message_id is not None:
                message = connection.execute(
                    'SELECT * FROM messages WHERE id=:p0 AND room_id=:p1', {'p0': message_id, 'p1': room_id}
                ).fetchone()
                if message is None:
                    raise DomainError(404, "本房没有这条消息。")
                if message["channel"] == "backstage" and not self._backstage_allowed(room, reporter):
                    raise DomainError(403, "你无权读取或举报这条后台消息。")
                target_member_id = message["member_id"]
                against_host = target_member_id is None or bool(host and target_member_id == host["id"])
                target_type, target_key = "message", "message:" + message_id
                snapshot = {"message": self._message_json(message, connection)}
            else:
                snapshot = {"room": {key: room[key] for key in ("id", "title", "background", "announcement")}}
            existing = connection.execute(
                "SELECT * FROM reports WHERE reporter_session_id=:p0 AND room_id=:p1 AND target_key=:p2 AND status='open'",
                {'p0': session["id"], 'p1': room_id, 'p2': target_key},
            ).fetchone()
            if existing is not None:
                if existing["reason"] == reason and existing["details"] == details:
                    return {"report": self._report_json(connection, existing, session["id"])}
                raise DomainError(409, "这个目标已有待处理反馈，请先查看原回执。")
            self._check_rate(connection, "report", session["id"], self.settings.report_limit_per_hour, 3600)
            report_id = new_id()
            connection.execute(
                'INSERT INTO reports(id,room_id,reporter_session_id,reporter_member_id,target_type,target_key,message_id,target_member_id,against_host,reason,details,snapshot_json,created_at) VALUES(:p0,:p1,:p2,:p3,:p4,:p5,:p6,:p7,:p8,:p9,:p10,:p11,:p12)',
                {'p0': report_id, 'p1': room_id, 'p2': session["id"], 'p3': reporter["id"], 'p4': target_type, 'p5': target_key, 'p6': message_id, 'p7': target_member_id, 'p8': int(against_host), 'p9': reason, 'p10': details, 'p11': json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), 'p12': iso_time()},
            )
            self._record_rate(connection, "report", session["id"])
            self._bump(connection, room_id)
            report = connection.execute('SELECT * FROM reports WHERE id=:p0', {'p0': report_id}).fetchone()
            return {"report": self._report_json(connection, report, session["id"])}

    def list_reports(
        self, token: str | None, room_id: str | None, after: int, limit: int,
    ) -> dict:
        with self._transaction() as connection:
            session = self._session(connection, token)
            parameters = {"after": after, "limit": limit + 1}
            clauses = ["seq>:after"]
            if room_id is not None:
                room = self._room(connection, room_id)
                history_member = connection.execute(
                    'SELECT id FROM room_members WHERE room_id=:p0 AND session_id=:p1', {'p0': room_id, 'p1': session["id"]}
                ).fetchone()
                if history_member is None:
                    raise DomainError(403, "你没有本房反馈记录的查看权限。")
                member = self._member(connection, room_id, session["id"], required=False)
                is_host = bool(member and room["host_session_id"] == session["id"])
                clauses.append("room_id=:room_id")
                parameters["room_id"] = room_id
                if is_host:
                    clauses.append("(reporter_session_id=:session_id OR against_host=0)")
                else:
                    clauses.append("reporter_session_id=:session_id")
                parameters["session_id"] = session["id"]
            else:
                clauses.append("reporter_session_id=:session_id")
                parameters["session_id"] = session["id"]
            rows = connection.execute(
                "SELECT * FROM reports WHERE " + " AND ".join(clauses) + " ORDER BY seq LIMIT :limit", parameters
            ).fetchall()
            selected = rows[:limit]
            return {
                "reports": [self._report_json(connection, row, session["id"]) for row in selected],
                "cursor": selected[-1]["seq"] if selected else after, "has_more": len(rows) > limit,
            }

    @staticmethod
    def _resolve_report_record(
        connection: sqlite3.Connection, report: sqlite3.Row, status: str, note: str,
        *, resolver: str, member_id: str | None = None, operator_label: str | None = None,
    ) -> None:
        if status not in {"resolved", "dismissed"} or not note.strip() or len(note) > 500:
            raise DomainError(422, "处理结果须为已处理或不成立，并附 1—500 字说明。")
        if report["status"] != "open":
            if report["status"] == status and report["resolution_note"] == note:
                return
            raise DomainError(409, "反馈已结案，不能覆盖原处理记录。")
        connection.execute(
            'UPDATE reports SET status=:p0,resolution_note=:p1,resolved_at=:p2,resolved_by=:p3,resolver_member_id=:p4,operator_label=:p5 WHERE id=:p6',
            {'p0': status, 'p1': note, 'p2': iso_time(), 'p3': resolver, 'p4': member_id, 'p5': operator_label, 'p6': report["id"]},
        )

    def resolve_report(
        self, token: str | None, csrf: str | None, room_id: str,
        report_id: str, status: str, note: str,
    ) -> dict:
        with self._transaction(write=True, session_token=token, room_id=room_id, report_id=report_id) as connection:
            session = self._session(connection, token, csrf, require_csrf=True)
            room = self._room(connection, room_id)
            host = self._host(connection, room, session["id"])
            report = connection.execute(
                'SELECT * FROM reports WHERE id=:p0 AND room_id=:p1', {'p0': report_id, 'p1': room_id}
            ).fetchone()
            if report is None:
                raise DomainError(404, "本房没有该反馈。")
            if report["against_host"]:
                raise DomainError(403, "涉及房主或房间本身的反馈须由服务器维护者处理。")
            self._resolve_report_record(connection, report, status, note, resolver="host", member_id=host["id"])
            self._bump(connection, room_id)
            updated = connection.execute('SELECT * FROM reports WHERE id=:p0', {'p0': report_id}).fetchone()
            return {"report": self._report_json(connection, updated, session["id"])}

    def operator_list_reports(self, status: str, after: int, limit: int) -> dict:
        with self._transaction() as connection:
            where, parameters = "seq>:after", {"after": after, "limit": limit + 1}
            if status != "all":
                if status not in {"open", "resolved", "dismissed"}:
                    raise DomainError(422, "未知反馈状态。")
                where += " AND status=:status"
                parameters["status"] = status
            rows = connection.execute("SELECT * FROM reports WHERE " + where + " ORDER BY seq LIMIT :limit", parameters).fetchall()
            selected = rows[:limit]
            return {
                "reports": [self._report_json(connection, row, None, operator=True) for row in selected],
                "cursor": selected[-1]["seq"] if selected else after, "has_more": len(rows) > limit,
            }

    @staticmethod
    def _operator_label(label: str) -> str:
        if not label.strip() or len(label) > 80:
            raise DomainError(422, "维护者审计标签须为 1—80 字。")
        return label.strip()

    def operator_resolve_report(self, report_id: str, status: str, note: str, operator_label: str) -> dict:
        operator_label = self._operator_label(operator_label)
        with self._transaction(write=True, report_id=report_id) as connection:
            report = connection.execute('SELECT * FROM reports WHERE id=:p0', {'p0': report_id}).fetchone()
            if report is None:
                raise DomainError(404, "反馈不存在。")
            self._resolve_report_record(connection, report, status, note, resolver="operator", operator_label=operator_label)
            self._bump(connection, report["room_id"])
            updated = connection.execute('SELECT * FROM reports WHERE id=:p0', {'p0': report_id}).fetchone()
            return {"report": self._report_json(connection, updated, None, operator=True)}

    def operator_end_room(self, room_id: str, note: str, operator_label: str) -> dict:
        operator_label = self._operator_label(operator_label)
        if not note.strip() or len(note) > 500:
            raise DomainError(422, "结束房间须附 1—500 字审计说明。")
        with self._transaction(write=True, room_id=room_id) as connection:
            room = self._room(connection, room_id)
            if room["status"] == "ended":
                return {"room_id": room_id, "status": "ended", "changed": False}
            now, audit_id = iso_time(), new_id()
            connection.execute("UPDATE rooms SET status='ended',allow_join=0 WHERE id=:p0", {'p0': room_id})
            connection.execute(
                "UPDATE role_requests SET status='cancelled',decided_at=:p0 WHERE room_id=:p1 AND status='pending'", {'p0': now, 'p1': room_id}
            )
            connection.execute(
                "INSERT INTO moderation_actions(id,room_id,action,operator_label,note,created_at) VALUES(:p0,:p1,'end_room',:p2,:p3,:p4)", {'p0': audit_id, 'p1': room_id, 'p2': operator_label, 'p3': note, 'p4': now}
            )
            self._bump(connection, room_id)
            self._insert_message(
                connection, room_id, "stage", text="服务器维护者结束了本场，记录保留供本房成员阅读。",
                member_id=None, character_id=None, author="系统", kind="system",
            )
            return {"room_id": room_id, "status": "ended", "changed": True, "audit_id": audit_id}

    def websocket_identity(self, token: str | None, room_id: str) -> dict:
        with self._transaction() as connection:
            session = self._session(connection, token)
            room = self._room(connection, room_id)
            member = self._member(connection, room_id, session["id"])
            return {
                "session_id": session["id"], "member_id": member["id"],
                "expires_at": session["expires_at"],
                "revision": room["revision"],
            }


from .accounts import AccountMixin
from .room_features import RoomFeaturesMixin
from .community import CommunityMixin
from .push import NotificationMixin


class Store(NotificationMixin, AccountMixin, RoomFeaturesMixin, CommunityMixin, LegacyStore):
    """Shared transactional domain with additive account/community modules."""


def create_store(settings: Settings) -> Store:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required; the application supports PostgreSQL only. Existing SQLite files are not opened or migrated.")
    from .postgres import PostgresStore
    return PostgresStore(settings)
