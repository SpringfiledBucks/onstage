#!/usr/bin/env python3
"""Controlled PostgreSQL migration and private snapshot recovery.

DATABASE_URL is read from the environment, never a command argument. Snapshots
contain credentials and private chat/report text: keep them outside the checkout.
Restore requires a migrated but empty PostgreSQL destination and never deletes rows.
SQLite files are retired and are never imported by this command.
Freeze source writes before final export/cutover; keep exactly one writable site.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import inspect, text
from app.postgres import PostgresStore
from app.security import Settings, iso_time

LEGACY_TABLES = ('schema_meta', 'sessions', 'characters', 'rooms', 'room_members',
          'role_requests', 'messages', 'room_bans', 'rate_events', 'reports', 'moderation_actions')
EXTENSION_TABLES = ('users', 'auth_sessions', 'account_audit', 'room_options', 'room_access_grants',
                    'room_invites', 'room_scene_actions', 'room_presence_leases', 'room_list_snapshots',
                    'room_ratings', 'member_feedback', 'room_share_permissions', 'conversation_shares',
                    'share_messages', 'member_reports', 'developer_feedback', 'alembic_version')
V2_EXTENSION_TABLES = EXTENSION_TABLES
EXTENSION_TABLES = EXTENSION_TABLES[:-1] + ('member_feedback_ledger', 'push_subscriptions', 'room_notification_follows', 'push_outbox', 'alembic_version')
TABLES = LEGACY_TABLES + EXTENSION_TABLES
BUSINESS = tuple(name for name in TABLES if name not in ('schema_meta', 'characters', 'alembic_version'))
FORMAT = 'onstage-postgres-private-v3'
MAX_BYTES = 64 * 1024 * 1024


def canonical(rows):
    return sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':')) for row in rows)


def snapshot(store):
    with store.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
        with connection.begin():
            connection.execute(text('SET TRANSACTION READ ONLY'))
            existing = set(inspect(connection).get_table_names())
            extension = set(EXTENSION_TABLES) & existing
            if extension and extension not in (set(EXTENSION_TABLES), set(V2_EXTENSION_TABLES)):
                raise ValueError('Incomplete community schema; use a verified native pg_dump backup before repair.')
            if not set(LEGACY_TABLES).issubset(existing):
                raise ValueError('Source is not a complete Onstage schema-4 database.')
            version = connection.execute(text("SELECT value FROM schema_meta WHERE key='schema_version'")).scalar()
            if version != '4':
                raise ValueError('Unsupported source schema; source preserved.')
            names = (TABLES if extension == set(EXTENSION_TABLES) else LEGACY_TABLES + V2_EXTENSION_TABLES) if extension else LEGACY_TABLES
            tables = {}
            for name in names:
                rows = connection.execute(text(f'SELECT * FROM {name} LIMIT 100001')).mappings().all()
                if len(rows) > 100000:
                    raise ValueError('Snapshot row limit exceeded; use pg_dump for larger databases.')
                tables[name] = [dict(row) for row in rows]
    return {'format': (FORMAT if extension == set(EXTENSION_TABLES) else 'onstage-postgres-private-v2') if extension else 'onstage-postgres-private-v1', 'schema_version': 4, 'exported_at': iso_time(),
            'contains_private_session_credentials': True, 'tables': tables,
            'row_counts': {name: len(rows) for name, rows in tables.items()}}


def read_snapshot(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Snapshot is too large; use native PostgreSQL backup tooling.')
    payload = json.loads(path.read_text(encoding='utf8'))
    tables = payload.get('tables', {})
    legacy = payload.get('format') in ('onstage-postgres-private-v1', 'onstage-confidential-state-v1')
    valid_sets = ((set(LEGACY_TABLES), set(LEGACY_TABLES) - {'rate_events'}) if legacy else
                  (set(LEGACY_TABLES + V2_EXTENSION_TABLES),) if payload.get('format') == 'onstage-postgres-private-v2' else (set(TABLES),))
    if (payload.get('format') not in (FORMAT, 'onstage-postgres-private-v2', 'onstage-postgres-private-v1', 'onstage-confidential-state-v1')
            or payload.get('schema_version') != 4
            or payload.get('contains_private_session_credentials') is not True
            or not isinstance(tables, dict)
            or set(tables) not in valid_sets):
        raise ValueError('Unsupported or incomplete private snapshot.')
    for name, rows in tables.items():
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or payload.get('row_counts', {}).get(name) != len(rows)):
            raise ValueError('Snapshot row count/shape mismatch.')
    if not any(row == {'key': 'schema_version', 'value': '4'} for row in tables['schema_meta']):
        raise ValueError('Snapshot does not contain the expected business schema version.')
    return payload


def sqlite_snapshot(path):
    source = path.resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('SQLite source failed integrity/foreign-key validation.')
        version = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if not version or version[0] != '4':
            raise ValueError('SQLite source must already be schema 4; no implicit migration performed.')
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        extension = set(EXTENSION_TABLES) & existing
        if extension and extension not in (set(EXTENSION_TABLES), set(V2_EXTENSION_TABLES)):
            raise ValueError('SQLite community schema is incomplete; source preserved.')
        names = (TABLES if extension == set(EXTENSION_TABLES) else LEGACY_TABLES + V2_EXTENSION_TABLES) if extension else LEGACY_TABLES
        tables = {name: [dict(row) for row in connection.execute(f'SELECT * FROM {name}')] for name in names}
    return {'format': (FORMAT if extension == set(EXTENSION_TABLES) else 'onstage-postgres-private-v2') if extension else 'onstage-postgres-private-v1', 'schema_version': 4, 'contains_private_session_credentials': True,
            'tables': tables, 'row_counts': {name: len(rows) for name, rows in tables.items()}}


def compare(connection, payload):
    counts = {}
    for name, expected in payload['tables'].items():
        if name == 'alembic_version' and payload['format'] == 'onstage-postgres-private-v2':
            from app.extension_schema import REVISION
            expected = [{'version_num': REVISION}]
        actual = [dict(row) for row in connection.execute(text(f'SELECT * FROM {name}')).mappings()]
        if canonical(expected) != canonical(actual):
            raise ValueError(f'Recovery verification failed for {name}.')
        counts[name] = len(actual)
    # Foreign keys are immediate and validated by PostgreSQL during import.
    # Check room/channel cursors against all stored messages as a separate invariant.
    invalid = connection.execute(text("SELECT r.id FROM rooms r WHERE r.stage_seq != COALESCE((SELECT MAX(seq) FROM messages m WHERE m.room_id=r.id AND m.channel='stage'),0) OR r.backstage_seq != COALESCE((SELECT MAX(seq) FROM messages m WHERE m.room_id=r.id AND m.channel='backstage'),0) LIMIT 1")).first()
    if invalid:
        raise ValueError('Room cursor invariant failed.')
    return counts


def restore(store, payload):
    with store.engine.begin() as connection:
        # Destination must be offline. These locks also stop an accidental live
        # writer from racing the empty-target check and import transaction.
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text('LOCK TABLE ' + ','.join(TABLES) + ' IN ACCESS EXCLUSIVE MODE'))
        if any(connection.execute(text(f'SELECT 1 FROM {name} LIMIT 1')).first() for name in BUSINESS):
            raise ValueError('Destination has business data; restore refuses to overwrite it.')
        inspector = inspect(connection)
        for name in TABLES:
            rows = payload['tables'].get(name, [])
            if name == 'alembic_version' and payload['format'] == 'onstage-postgres-private-v2':
                continue  # The controlled destination is already migrated to head.
            columns = {column['name'] for column in inspector.get_columns(name)}
            for row in rows:
                if set(row) != columns:
                    raise ValueError(f'Snapshot columns differ from schema: {name}.')
                if name in ('schema_meta', 'characters', 'alembic_version'):
                    key = 'key' if name == 'schema_meta' else 'version_num' if name == 'alembic_version' else 'id'
                    existing = connection.execute(text(f'SELECT * FROM {name} WHERE {key}=:key'), {'key': row[key]}).mappings().first()
                    if existing:
                        if dict(existing) != row:
                            raise ValueError(f'Destination seed differs: {name}.')
                        continue
                names = list(row)
                connection.execute(text(f'INSERT INTO {name} (' + ','.join('"'+key+'"' for key in names) + ') VALUES (' + ','.join(':'+key for key in names) + ')'), row)
        if payload['format'] == 'onstage-postgres-private-v2':
            connection.execute(text('INSERT INTO member_feedback_ledger(room_id,voter_user_id,target_principal_session_id,created_at) SELECT f.room_id,f.voter_user_id,m.session_id,f.created_at FROM member_feedback f JOIN room_members m ON m.id=f.target_member_id WHERE NOT EXISTS(SELECT 1 FROM member_feedback_ledger l WHERE l.room_id=f.room_id AND l.voter_user_id=f.voter_user_id AND l.target_principal_session_id=m.session_id)'))
        for name, column in (('reports', 'seq'), ('rate_events', 'id')):
            connection.execute(text(f"SELECT setval(pg_get_serial_sequence('{name}','{column}'), COALESCE(MAX({column}),1), MAX({column}) IS NOT NULL) FROM {name}"))
        counts = compare(connection, payload)
    return counts


def backup(store, path):
    path = path.resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Private snapshots must be saved outside the source checkout.")
    payload = snapshot(store)
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Snapshot exceeds size limit; no backup published.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish only a fully written snapshot. A same-directory hard link is
    # atomic and fails if a previous recovery point already owns the name.
    descriptor, temporary_name = tempfile.mkstemp(prefix='.onstage-private-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw), 'row_counts': payload['row_counts']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('migrate')
    for command in ('backup', 'restore', 'verify'):
        commands.add_parser(command).add_argument('path', type=Path)
    args = parser.parse_args()
    settings = Settings.from_values(public_origin=os.environ.get('ONSTAGE_PUBLIC_ORIGIN', 'http://localhost'))
    if not settings.database_url:
        parser.error('DATABASE_URL or ONSTAGE_DATABASE_URL is required; no SQLite fallback in this tool.')
    store = PostgresStore(settings)
    try:
        if args.command == 'migrate':
            store.migrate()
            result = store.ready()
        elif args.command == 'backup':
            # Also accepts the intact pre-community schema, before any migration.
            result = backup(store, args.path)
        else:
            store.ready()
            payload = read_snapshot(args.path)
            if args.command == 'verify':
                with store.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
                    with connection.begin():
                        result = {'verified_row_counts': compare(connection, payload)}
            else:
                result = {'restored_row_counts': restore(store, payload)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        # DB exception strings may contain DSNs or private bound row values.
        print(json.dumps({'error': type(error).__name__, 'detail': str(error) if isinstance(error, ValueError) else 'Database operation failed; source preserved.'}), file=sys.stderr)
        return 1
    finally:
        store.close()

if __name__ == '__main__':
    raise SystemExit(main())
