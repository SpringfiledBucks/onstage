"""PostgreSQL persistence with shared domain rules and explicit SQL dialect.

Write ownership: session advisory lock (cross-room limits), room row lock
(room state/sequence/role/permission atomically), report row lock. Only session
and room admission take their own global capacity lock. Independent rooms with
different senders never share a write lock. Reads use one repeatable snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .security import Settings
from .store import Store


class Record(dict):
    """Match sqlite.Row's existing indexed and named field access."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class Result:
    def __init__(self, result):
        self.result = result

    def fetchone(self):
        row = self.result.fetchone()
        return Record(row._mapping) if row is not None else None

    def fetchall(self):
        return [Record(row._mapping) for row in self.result.fetchall()]


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=None):
        return Result(self.connection.execute(text(statement), parameters or {}))


def postgres_url(value: str) -> str:
    if value.startswith('postgres://'):
        return 'postgresql+psycopg://' + value[len('postgres://'):]
    if value.startswith('postgresql://'):
        return 'postgresql+psycopg://' + value[len('postgresql://'):]
    if not value.startswith('postgresql+psycopg://'):
        raise RuntimeError('DATABASE_URL must use PostgreSQL (postgresql:// or postgresql+psycopg://).')
    return value


class PostgresStore(Store):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.engine = create_engine(
            postgres_url(settings.database_url), pool_size=5, max_overflow=5,
            pool_timeout=5, pool_pre_ping=True, hide_parameters=True,
            connect_args={'connect_timeout': 5},
        )

    def initialize(self):
        self._load_catalog()
        # Deliberately no schema mutation during application/Pod startup.
        self.ready()

    def close(self):
        self.engine.dispose()

    @staticmethod
    def _lock(connection, scope):
        key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], 'big', signed=True)
        connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})

    @contextmanager
    def _transaction(self, *, write=False, room_id=None, session_token=None,
                     admission=None, report_id=None):
        with self.engine.connect() as raw:
            with raw.begin():
                raw.execute(text("SET LOCAL lock_timeout = '5s'"))
                raw.execute(text("SET LOCAL statement_timeout = '15s'"))
                if not write:
                    raw.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'))
                else:
                    # All callers use this order. Session scope is a digest, never a credential.
                    if session_token:
                        principal = raw.execute(text('SELECT u.principal_session_id FROM auth_sessions a JOIN users u ON u.id=a.user_id WHERE a.token_hash=:hash'),
                                                {'hash': hashlib.sha256(session_token.encode()).hexdigest()}).scalar()
                        self._lock(raw, 'session:' + (principal or hashlib.sha256(session_token.encode()).hexdigest()))
                    if admission:
                        self._lock(raw, 'admission:' + admission)
                    # Operator report resolution starts with only a report ID.
                    # Locate its immutable room first, then acquire the same
                    # room -> report lock order as browser/host resolution.
                    lock_room = room_id
                    if report_id and not lock_room:
                        lock_room = raw.execute(text('SELECT room_id FROM reports WHERE id=:id'), {'id': report_id}).scalar()
                    if lock_room:
                        raw.execute(text('SELECT id FROM rooms WHERE id=:id FOR UPDATE'), {'id': lock_room})
                    if report_id:
                        raw.execute(text('SELECT id FROM reports WHERE id=:id FOR UPDATE'), {'id': report_id})
                yield Connection(raw)

    def migrate(self):
        """Explicit operator step; a PostgreSQL transaction guards the whole DDL."""
        self._load_catalog()
        with self.engine.begin() as raw:
            self._lock(raw, 'schema-migration')
            existing = raw.execute(text("SELECT to_regclass('schema_meta')")).scalar()
            if existing:
                version = raw.execute(text("SELECT value FROM schema_meta WHERE key='schema_version'")).scalar()
                if version != '4':
                    raise RuntimeError('Unsupported PostgreSQL schema version; source preserved.')
            elif raw.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' LIMIT 1")).first():
                raise RuntimeError('Target schema is not empty; refusing to overwrite unrelated tables.')
            schema = Path(__file__).with_name('schema_postgres.sql').read_text(encoding='utf8')
            for statement in schema.split(';'):
                if statement.strip():
                    raw.execute(text(statement))
            raw.execute(text("INSERT INTO schema_meta(key,value) VALUES('schema_version','4') ON CONFLICT(key) DO NOTHING"))
            raw.execute(text('INSERT INTO characters(id) VALUES(:id) ON CONFLICT(id) DO NOTHING'),
                        [{'id': item} for item in sorted(self.characters)])
            from .extension_schema import migrate_extensions
            migrate_extensions(raw)
