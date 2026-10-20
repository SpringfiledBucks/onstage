"""Room behavior contracts run against SQLite and isolated PostgreSQL schemas."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.security import DomainError, iso_time, utc_now
from test_accounts import accounts, guest, register


def identity(store, name):
    user = register(store, name)
    return user['token'], user['csrf_token']


def new_room(store):
    host = identity(store, 'HostUser')
    actor = identity(store, 'ActorUser')
    observer = identity(store, 'ObserverUser')
    room = store.create_room(*host, 'mygo_rehearsal', 'Room')
    store.join(*actor, room)
    store.join(*observer, room)
    store.claim_character(*host, room, 'mygo.tomori')
    store.claim_character(*actor, room, 'mygo.anon')
    store.control(*host, room, 'start')
    return host, actor, observer, room


def test_private_access_grants_invite_usage_and_no_directory_leak(accounts):
    host, actor, _, room = new_room(accounts)
    outsider = identity(accounts, 'Outsider')
    accounts.configure_room_access(*host, room, 'invite')
    assert accounts.public_rooms()['rooms'] == []
    assert accounts.list_rooms(outsider[0], {})['rooms'] == []
    with pytest.raises(DomainError):
        accounts.get_room(outsider[0], room, set())
    with pytest.raises(DomainError):
        accounts.join(*outsider, room)
    # Existing participants remain authorized; invitation is not a role claim.
    assert accounts.get_room(actor[0], room, set())['room']['access'] == 'invite'
    invite = accounts.create_invite(*host, room, max_uses=1)['invite']
    accounts.room_unlock(*outsider, room, invite=invite)
    accounts.join(*outsider, room)
    another = identity(accounts, 'AnotherUser')
    with pytest.raises(DomainError):
        accounts.room_unlock(*another, room, invite=invite)
    with accounts._transaction() as conn:
        assert conn.execute('SELECT token_hash FROM room_invites').fetchone()[0] != invite
    assert 'password_hash' not in json.dumps(accounts.get_room(host[0], room, set()))


def test_password_unlock_rate_survives_failed_attempt(accounts):
    host, _, _, room = new_room(accounts)
    outsider = identity(accounts, 'Outsider')
    accounts.configure_room_access(*host, room, 'password', 'room-password')
    with pytest.raises(DomainError):
        accounts.room_unlock(*outsider, room, password='wrong')
    accounts.room_unlock(*outsider, room, password='room-password')
    assert accounts.get_room(outsider[0], room, set())['room']['access'] == 'password'
    with accounts._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rate_events WHERE action='room_unlock'").fetchone()[0] == 2


def test_scene_switch_idempotent_preserves_cast_and_history(accounts):
    host, _, _, room = new_room(accounts)
    target = 'test_new_scene'
    accounts.scenarios[target] = {**accounts.scenarios['mygo_rehearsal'], 'id': target, 'title': '切换场景', 'character_ids': ['mujica.uika']}
    first = accounts.switch_scene(*host, room, target, 'switch-1')
    again = accounts.switch_scene(*host, room, target, 'switch-1')
    assert first['message_id'] == again['message_id'] and not again['changed']
    with pytest.raises(DomainError):
        accounts.switch_scene(*host, room, 'mygo_rehearsal', 'switch-1')
    newcomer = identity(accounts, 'NewPlayer')
    accounts.control(*host, room, 'open_join')
    accounts.join(*newcomer, room)
    accounts.claim_character(*newcomer, room, 'mygo.rana')
    value = accounts.get_room(host[0], room, set())['room']
    assert value['scenario_id'] == target and value['cast_scenario_id'] == 'mygo_rehearsal'
    with accounts._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE text LIKE '房主将游戏场景切换为%'").fetchone()[0] == 1


def test_hide_only_ended_and_mine_keeps_archive(accounts):
    host, _, _, room = new_room(accounts)
    with pytest.raises(DomainError):
        accounts.hide_room(*host, room, True)
    accounts.control(*host, room, 'end')
    accounts.hide_room(*host, room, True)
    assert accounts.public_rooms()['rooms'] == []
    assert accounts.public_rooms(host[0], filter='mine')['rooms'][0]['id'] == room


def test_thousand_rooms_time_pagination_and_rank_snapshot_access_recheck(accounts):
    host = identity(accounts, 'HostUser')
    with accounts._transaction(write=True) as conn:
        principal = accounts._session(conn, host[0])['id']
        for i in range(1107):
            conn.execute("INSERT INTO rooms(id,title,scenario_id,status,host_session_id,created_at,updated_at) VALUES(:id,:title,'mygo_rehearsal','ended',:host,:now,:now)",
                         {'id': f'room-{i:05}', 'title': 'public', 'host': principal, 'now': iso_time()})
    seen, cursor = [], None
    while True:
        page = accounts.public_rooms(limit=100, cursor=cursor)
        seen.extend(item['id'] for item in page['rooms'])
        if not page['has_more']:
            break
        cursor = page['next_cursor']
    assert len(seen) == len(set(seen)) == 1107
    page = accounts.public_rooms(limit=24, sort='rating')
    first = {row['id'] for row in page['rooms']}
    with accounts._transaction(write=True) as conn:
        hidden = seen[30]
        room = accounts._room(conn, hidden)
        accounts._options(conn, room, create=True)
        conn.execute('UPDATE room_options SET hidden=1 WHERE room_id=:r', {'r': hidden})
    following = accounts.public_rooms(limit=100, sort='rating', cursor=page['next_cursor'])
    assert hidden not in {row['id'] for row in following['rooms']}
    assert not first.intersection(row['id'] for row in following['rooms'])
    assert all('members' not in row and 'host_session_id' not in row for row in following['rooms'])


def sweep_to(store, start, seconds):
    ended = []
    for offset in range(20, seconds + 1, 20):
        ended.extend(store.sweep_offline(start + timedelta(seconds=offset)))
    return ended


def test_offline_ten_minutes_observer_does_not_keep_alive_and_end_once(accounts):
    host, actor, observer, room = new_room(accounts)
    accounts.recover_presence()
    start = utc_now()
    accounts.heartbeat(*observer, room, 'observer')
    assert not sweep_to(accounts, start, 600)
    assert accounts.sweep_offline(start + timedelta(seconds=620)) == [room]
    assert accounts.sweep_offline(start + timedelta(seconds=640)) == []
    assert accounts.get_room(host[0], room, set())['room']['status'] == 'ended'
    with pytest.raises(DomainError):
        accounts.send_message(*actor, room, 'stage', 'after-end', 'late')
    with accounts._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE text LIKE '房主及全部演员离线%'").fetchone()[0] == 1


def test_outage_resets_grace_and_actor_heartbeat_cancels_deadline(accounts):
    host, actor, _, room = new_room(accounts)
    accounts.recover_presence()
    start = utc_now()
    assert not sweep_to(accounts, start, 580)
    # Worker scheduling interruption must not immediately end a game.
    assert accounts.sweep_offline(start + timedelta(seconds=900)) == []
    with accounts._transaction() as conn:
        assert conn.execute('SELECT offline_since FROM room_options WHERE room_id=:r', {'r': room}).fetchone()[0] is None
    accounts.heartbeat(*actor, room, 'mobile')
    with accounts._transaction() as conn:
        assert conn.execute('SELECT offline_since FROM room_options WHERE room_id=:r', {'r': room}).fetchone()[0] is None


def test_recruitment_preferences_and_scene_pages(accounts):
    host, _, _, room = new_room(accounts)
    accounts.recruit_roles(*host, room, ['mygo.rana', 'mygo.tomori'])
    accounts.control(*host, room, 'open_join')
    newer = accounts.create_room(*host, 'mygo_rehearsal', 'newer')
    accounts.recruit_roles(*host, newer, ['mygo.soyo'])
    accounts.set_preferences(*host, ['mygo.rana'])
    preferred = accounts.public_rooms(host[0], preferred=True)['rooms']
    assert preferred[0]['id'] == room
    assert preferred[0]['recruitment'] == ['mygo.rana']
    pages, cursor = [], None
    while True:
        page = accounts.public_scenes(limit=3, cursor=cursor)
        pages.extend(row['id'] for row in page['scenarios'])
        if not page['has_more']:
            break
        cursor = page['next_cursor']
    assert len(pages) == len(set(pages)) == len(accounts.scenarios)


def test_concurrent_single_use_invite_is_not_double_consumed(accounts):
    host, _, _, room = new_room(accounts)
    accounts.configure_room_access(*host, room, 'invite')
    invitation = accounts.create_invite(*host, room)['invite']
    candidates = [identity(accounts, name) for name in ['CandidateA', 'CandidateB']]
    def unlock(identity):
        try:
            return accounts.room_unlock(*identity, room, invite=invitation)['unlocked']
        except DomainError as exc:
            assert exc.status_code == 403
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(unlock, candidates))
    assert sorted(results) == [False, True]
    # A successful recipient retry is idempotent and consumes no extra use.
    accounts.room_unlock(*candidates[results.index(True)], room, invite=invitation)
    with accounts._transaction() as conn:
        assert conn.execute('SELECT uses FROM room_invites').fetchone()[0] == 1


def test_multiple_devices_and_reconnect_reset_full_deadline(accounts, monkeypatch):
    import app.room_features as features
    host, actor, _, room = new_room(accounts)
    clock = [utc_now()]
    monkeypatch.setattr(features, 'utc_now', lambda: clock[0])
    accounts.recover_presence()
    start = clock[0]
    accounts.heartbeat(*actor, room, 'device-a')
    accounts.heartbeat(*actor, room, 'device-b')
    for offset in range(20, 661, 20):
        clock[0] = start + timedelta(seconds=offset)
        assert accounts.sweep_offline() == []
    # Initial leases expire at90s; a late reconnect cancels the pending end.
    accounts.heartbeat(*host, room, 'reconnected')
    for offset in range(680, 1341, 20):
        clock[0] = start + timedelta(seconds=offset)
        assert accounts.sweep_offline() == []
    clock[0] = start + timedelta(seconds=1360)
    assert accounts.sweep_offline() == [room]
