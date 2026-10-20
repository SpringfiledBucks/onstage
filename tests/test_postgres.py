"""Real PostgreSQL tests. Every test creates/drops only a generated schema.

Set ONSTAGE_TEST_DATABASE_URL to an authorized disposable PostgreSQL database.
The normal runtime DATABASE_URL is deliberately never used as test authority.
"""
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.postgres import PostgresStore, postgres_url
from app.security import DomainError, Settings
from app.store import Store
from tools.database import backup, read_snapshot, restore, snapshot, compare, sqlite_snapshot


@pytest.fixture
def pg_factory(tmp_path):
    value = os.environ.get('ONSTAGE_TEST_DATABASE_URL')
    if not value:
        pytest.skip('ONSTAGE_TEST_DATABASE_URL is required for real PostgreSQL integration')
    admin = create_engine(postgres_url(value))
    owned = []
    def factory(**overrides):
        schema = 'onstage_test_' + secrets.token_hex(10)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA {schema}'))
        url = make_url(postgres_url(value)).update_query_dict({'options': '-csearch_path=' + schema})
        settings = Settings.from_values(data_dir=tmp_path / schema, public_origin='http://localhost', database_url=url.render_as_string(hide_password=False))
        settings = replace(settings, **overrides)
        store = PostgresStore(settings)
        owned.append((schema, store))
        store.migrate()
        store.initialize()
        return store
    yield factory
    for schema, store in reversed(owned):
        store.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    admin.dispose()


def person(store):
    result = store.create_session(source=secrets.token_hex(8), nickname='测试参与者')
    return result['token'], result['csrf_token']


def room(store):
    host, actor, observer = [person(store) for _ in range(3)]
    room_id = store.create_room(*host, 'mygo_rehearsal', None)
    store.join(*actor, room_id)
    store.join(*observer, room_id)
    store.claim_character(*host, room_id, 'mygo.tomori')
    store.claim_character(*actor, room_id, 'mygo.anon')
    store.control(*host, room_id, 'start')
    return host, actor, observer, room_id


def error_code(call):
    try:
        return call()
    except DomainError as error:
        return error.status_code


def test_pg_explicit_migration_idempotent_and_startup_no_ddl(pg_factory):
    store = pg_factory()
    store.migrate()
    assert store.ready()['schema_version'] == 4
    with store.engine.begin() as connection:
        connection.execute(text("DELETE FROM schema_meta WHERE key='schema_version'"))
    assert error_code(store.initialize) == 503
    # Initialization did not silently repair or create the missing version.
    with store.engine.connect() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM schema_meta')).scalar() == 0


def test_pg_concurrent_role_claim_and_database_uniqueness(pg_factory):
    store = pg_factory()
    host = person(store)
    room_id = store.create_room(*host, 'mygo_rehearsal', None)
    actors = [person(store) for _ in range(8)]
    for actor in actors:
        store.join(*actor, room_id)
    barrier = threading.Barrier(len(actors))
    def claim(actor):
        barrier.wait()
        return error_code(lambda: store.claim_character(*actor, room_id, 'mygo.tomori'))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, actors))
    assert results.count(None) == 1
    assert results.count(409) == 7
    with store.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM room_members WHERE room_id=:r AND character_id='mygo.tomori'"), {'r': room_id}).scalar() == 1
        assert connection.execute(text("SELECT COUNT(*) FROM pg_constraint WHERE conrelid='room_members'::regclass AND contype='u'")).scalar() >= 4


def test_pg_concurrent_message_idempotency_content_and_cursors(pg_factory):
    store = pg_factory()
    host, actor, observer, room_id = room(store)
    barrier = threading.Barrier(8)
    def send(_):
        barrier.wait()
        return store.send_message(*actor, room_id, 'stage', '相同正文', 'logical-send-001')['message']
    with ThreadPoolExecutor(max_workers=8) as pool:
        messages = list(pool.map(send, range(8)))
    assert len({message['id'] for message in messages}) == 1
    assert all(message['client_message_id'] == 'logical-send-001' for message in messages)
    second = store.send_message(*actor, room_id, 'stage', '相同正文', 'logical-send-002')['message']
    assert second['id'] != messages[0]['id']
    assert error_code(lambda: store.send_message(*actor, room_id, 'stage', '改变正文', 'logical-send-001')) == 409
    sticker_id = next(iter(store.stickers))
    sticker = store.send_message(*actor, room_id, 'backstage', '', 'logical-sticker-001', 'sticker', sticker_id)['message']
    assert sticker['sticker']['id'] == sticker_id
    assert sticker['client_message_id'] == 'logical-sticker-001'
    assert error_code(lambda: store.send_message(*observer, room_id, 'backstage', '偷看', 'observer-001')) == 403
    with store.engine.connect() as connection:
        for channel in ('stage', 'backstage'):
            seqs = connection.execute(text('SELECT seq FROM messages WHERE room_id=:r AND channel=:c ORDER BY seq'), {'r': room_id, 'c': channel}).scalars().all()
            assert seqs == list(range(1, len(seqs) + 1))
    store.close()
    restarted = PostgresStore(store.settings)
    try:
        restarted.initialize()
        retry = restarted.send_message(*actor, room_id, 'stage', '相同正文', 'logical-send-001')['message']
        assert retry['id'] == messages[0]['id']
    finally:
        restarted.close()


def test_pg_kick_end_and_transactional_events(pg_factory):
    store = pg_factory()
    host, actor, observer, room_id = room(store)
    member_id = store.get_room(actor[0], room_id, set())['room']['me']['member_id']
    store.remove_member(*host, room_id, member_id)
    assert error_code(lambda: store.send_message(*actor, room_id, 'stage', '禁止', 'kicked-001')) == 403
    assert error_code(lambda: store.join(*actor, room_id)) == 403
    with store.engine.connect() as connection:
        member = connection.execute(text('SELECT status,character_id FROM room_members WHERE id=:m'), {'m': member_id}).mappings().one()
        assert dict(member) == {'status': 'left', 'character_id': None}
        assert connection.execute(text("SELECT COUNT(*) FROM messages WHERE room_id=:r AND kind='system' AND text LIKE '%移出%'"), {'r': room_id}).scalar() == 1
    store.control(*host, room_id, 'end')
    assert error_code(lambda: store.send_message(*host, room_id, 'backstage', '结束', 'ended-001')) == 409
    assert error_code(lambda: store.claim_character(*observer, room_id, 'mygo.rana')) == 409


def test_pg_reports_private_and_durable(pg_factory):
    store = pg_factory()
    host, actor, observer, room_id = room(store)
    host_message = store.send_message(*host, room_id, 'stage', '主办发言', 'host-msg-001')['message']
    report = store.create_report(*actor, room_id, host_message['id'], 'other', '私有证据')['report']
    assert store.list_reports(host[0], room_id, 0, 50)['reports'] == []
    assert store.list_reports(observer[0], room_id, 0, 50)['reports'] == []
    assert store.list_reports(actor[0], room_id, 0, 50)['reports'][0]['id'] == report['id']
    assert error_code(lambda: store.resolve_report(*host, room_id, report['id'], 'resolved', '越权')) == 403
    result = store.operator_resolve_report(report['id'], 'resolved', '维护者结案', 'test-operator')
    assert result['report']['status'] == 'resolved'
    assert store.operator_list_reports('resolved', 0, 50)['reports'][0]['id'] == report['id']


def test_pg_concurrent_rate_limit_and_room_isolation(pg_factory):
    store = pg_factory(message_limit_per_minute=1)
    host, actor, observer, room_id = room(store)
    barrier = threading.Barrier(6)
    def send(index):
        barrier.wait()
        return error_code(lambda: store.send_message(*actor, room_id, 'stage', '限流', f'rate-send-{index:03}'))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(send, range(6)))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count(429) == 5
    other_host = person(store)
    other_room = store.create_room(*other_host, 'mygo_rehearsal', None)
    # A held lock in another room cannot block a different sender's write here.
    with store._transaction(write=True, room_id=other_room):
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(store.send_message, *host, room_id, 'backstage', '独立房间', 'independent-room-001').result(timeout=3)
        assert result['message']['text'] == '独立房间'
        assert time.monotonic() - started < 3


def test_pg_backup_restore_exact_and_refuse_nonempty(pg_factory, tmp_path):
    source = pg_factory()
    host, actor, observer, room_id = room(source)
    sticker_id = next(iter(source.stickers))
    message = source.send_message(*actor, room_id, 'stage', '', 'backup-sticker-001', 'sticker', sticker_id)['message']
    source.create_report(*observer, room_id, message['id'], 'spam', '保留举报快照')
    source.leave(*observer, room_id)
    path = tmp_path / 'private-state.json'
    with pytest.raises(ValueError, match='outside the source checkout'):
        backup(source, Path(__file__).resolve().parents[1] / 'private-test-forbidden.json')
    backup(source, path)
    with pytest.raises(FileExistsError):
        backup(source, path)
    payload = read_snapshot(path)
    target = pg_factory()
    counts = restore(target, payload)
    assert counts['messages'] > 0 and counts['reports'] == 1
    with target.engine.connect() as connection:
        assert compare(connection, payload) == counts
    restored = target.send_message(*actor, room_id, 'stage', '', 'backup-sticker-001', 'sticker', sticker_id)['message']
    assert restored == message
    with pytest.raises(ValueError, match='business data'):
        restore(target, payload)
    # Imported identity sequences advance correctly for the next report/event.
    target.join(*observer, room_id)
    new_report = target.create_report(*observer, room_id, None, 'other', '新举报')['report']
    assert new_report['id'] != payload['tables']['reports'][0]['id']


def test_pg_sqlite_import_preserves_identity_history_and_permissions(pg_factory, tmp_path):
    source = Store(Settings.from_values(data_dir=tmp_path / 'legacy', public_origin='http://localhost', database_url=''))
    source.initialize()
    host, actor, observer, room_id = room(source)
    message = source.send_message(*actor, room_id, 'stage', '旧库原文', 'legacy-send-001')['message']
    source.create_report(*observer, room_id, None, 'other', '旧库举报')
    payload = sqlite_snapshot(source.db_path)
    target = pg_factory()
    restore(target, payload)
    assert target.send_message(*actor, room_id, 'stage', '旧库原文', 'legacy-send-001')['message'] == message
    assert target.list_reports(host[0], room_id, 0, 50)['reports'] == []
    assert error_code(lambda: target.send_message(*observer, room_id, 'backstage', '越权', 'legacy-denied-001')) == 403

def test_pg_http_websocket_and_process_replacement(pg_factory, tmp_path):
    from conftest import LiveServer, Person, create_room, seat
    store = pg_factory()
    root = tmp_path / 'http-runtime'
    root.mkdir()
    instance = LiveServer(root, database_url=store.settings.database_url)
    instance.start()
    host = actor = None
    try:
        host, actor = Person(instance), Person(instance)
        room_id = create_room(host)
        seat(host, host, room_id, 'mygo.tomori')
        seat(host, actor, room_id, 'mygo.anon')
        host.post(f'/api/rooms/{room_id}/control', {'action': 'start'})
        with actor.websocket(room_id) as ws:
            ws.recv(timeout=3)  # Connection's initial sync hint.
            message = host.post(f'/api/rooms/{room_id}/messages', {'channel': 'stage', 'text': '真实PG跨HTTP消息', 'client_message_id': 'http-pg-send-001'})['message']
            assert message['client_message_id'] == 'http-pg-send-001'
            assert 'sync' in ws.recv(timeout=3)
            received = actor.get(f'/api/rooms/{room_id}/sync')['messages']['stage']
            assert any(item['id'] == message['id'] and item['client_message_id'] == 'http-pg-send-001' for item in received)
        instance.restart()  # New app process, same independent PostgreSQL.
        retry = host.post(f'/api/rooms/{room_id}/messages', {'channel': 'stage', 'text': '真实PG跨HTTP消息', 'client_message_id': 'http-pg-send-001'})['message']
        assert retry == message
        assert actor.get(f'/api/rooms/{room_id}')['room']['me']['member_id']
    finally:
        if host:
            host.close()
        if actor:
            actor.close()
        instance.stop()


def test_pg_cli_private_backup_migration_and_recovery(pg_factory, tmp_path):
    import json
    import subprocess
    import sys
    source = pg_factory()
    host, actor, observer, room_id = room(source)
    source.send_message(*actor, room_id, 'stage', 'CLI恢复正文', 'cli-send-001')
    target = pg_factory()
    path = tmp_path / 'cli-private-backup.json'
    def run(store, *arguments):
        result = subprocess.run([sys.executable, 'tools/database.py', *map(str, arguments)],
                                env={**os.environ, 'DATABASE_URL': store.settings.database_url},
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    run(source, 'migrate')
    run(source, 'migrate')
    assert run(source, 'backup', path)['row_counts']['messages'] > 0
    assert run(target, 'restore', path)['restored_row_counts']['sessions'] == 3
    assert run(target, 'verify', path)['verified_row_counts']['rooms'] == 1
    result = subprocess.run([sys.executable, 'tools/moderate.py', 'list', '--status', 'all'],
                            env={**os.environ, 'DATABASE_URL': target.settings.database_url},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['reports'] == []


def test_pg_room_transition_rollback_includes_system_event(pg_factory, monkeypatch):
    store = pg_factory()
    host, actor, observer, room_id = room(store)
    before = snapshot(store)
    original = store._insert_message
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic failure after system message insertion')
    monkeypatch.setattr(store, '_insert_message', fail_after_insert)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        store.remove_member(*host, room_id, store.get_room(actor[0], room_id, set())['room']['me']['member_id'])
    assert canonical_tables(snapshot(store)['tables']) == canonical_tables(before['tables'])


def canonical_tables(tables):
    import json
    return {name: sorted(json.dumps(row, sort_keys=True) for row in rows) for name, rows in tables.items()}

def test_pg_same_sender_cross_room_rate_limit_is_atomic(pg_factory):
    store = pg_factory(message_limit_per_minute=1)
    host, actor = person(store), person(store)
    rooms = []
    for _ in range(2):
        room_id = store.create_room(*host, 'mygo_rehearsal', None)
        store.join(*actor, room_id)
        store.claim_character(*host, room_id, 'mygo.tomori')
        store.claim_character(*actor, room_id, 'mygo.anon')
        store.control(*host, room_id, 'start')
        rooms.append(room_id)
    barrier = threading.Barrier(2)
    def send(room_id):
        barrier.wait()
        return error_code(lambda: store.send_message(*actor, room_id, 'stage', '跨房限流', 'cross-room-' + room_id))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, rooms))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(429) == 1


def test_pg_concurrent_end_send_and_kick_send_preserve_order(pg_factory):
    store = pg_factory()
    for transition in ('end', 'kick'):
        host, actor, observer, room_id = room(store)
        actor_id = store.get_room(actor[0], room_id, set())['room']['me']['member_id']
        barrier = threading.Barrier(2)
        def send():
            barrier.wait()
            return error_code(lambda: store.send_message(*actor, room_id, 'stage', '与权限撤销并发', 'transition-' + room_id))
        def change():
            barrier.wait()
            if transition == 'end':
                store.control(*host, room_id, 'end')
            else:
                store.remove_member(*host, room_id, actor_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(send)
            pool.submit(change).result(timeout=5)
            result = future.result(timeout=5)
        assert isinstance(result, dict) or result in (403, 409)
        with store.engine.connect() as connection:
            rows = connection.execute(text("SELECT seq,kind,client_message_id FROM messages WHERE room_id=:r AND channel='stage' ORDER BY seq"), {'r': room_id}).mappings().all()
            # Transition event is the final stage record; a concurrently admitted
            # speech may only appear before it, never after permission removal.
            assert rows[-1]['kind'] == 'system'
            if isinstance(result, dict):
                assert result['message']['seq'] < rows[-1]['seq']
        denied = error_code(lambda: store.send_message(*actor, room_id, 'stage', '撤销后禁止', 'after-' + room_id))
        assert denied in (403, 409)

def test_pg_host_and_operator_report_resolution_share_lock_order(pg_factory):
    store = pg_factory()
    host, actor, observer, room_id = room(store)
    for index in range(6):
        message = store.send_message(*actor, room_id, 'stage', '可由房主处理的演员发言', f'resolution-race-{index:03}')['message']
        reporter = person(store)  # Distinct reporters keep the real 5/hour limit enabled.
        store.join(*reporter, room_id)
        report_id = store.create_report(*reporter, room_id, message['id'], 'other', '并发结案')['report']['id']
        barrier = threading.Barrier(2)
        def host_resolve():
            barrier.wait()
            return store.resolve_report(*host, room_id, report_id, 'resolved', '同一处理结论')
        def operator_resolve():
            barrier.wait()
            return store.operator_resolve_report(report_id, 'resolved', '同一处理结论', 'race-operator')
        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(host_resolve)
            right = pool.submit(operator_resolve)
            assert left.result(timeout=5)['report']['status'] == 'resolved'
            assert right.result(timeout=5)['report']['status'] == 'resolved'
        reports = store.operator_list_reports('resolved', 0, 50)['reports']
        assert len([report for report in reports if report['id'] == report_id]) == 1


def test_postgres_thousand_rooms_keyset(pg_factory):
    from test_room_pagination import verify_pages
    store = pg_factory()
    verify_pages(store)
    # Re-running controlled migration preserves all rows and makes indexes idempotent.
    store.migrate()
    with store.engine.connect() as connection:
        indexes = connection.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()" )).scalars().all()
        assert 'rooms_created_page' in indexes
        assert connection.execute(text('SELECT COUNT(*) FROM rooms')).scalar() == 1107
