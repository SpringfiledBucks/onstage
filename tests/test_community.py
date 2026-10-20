"""Business/privacy checks run on SQLite and an explicitly authorized isolated PG schema."""
import json
import secrets
from pathlib import Path

import pytest

from app.accounts import AccountMixin
from app.community import CommunityMixin
from app.migrations import execute_schema
from app.security import DomainError, Settings
from app.store import Store
from test_postgres import pg_factory


@pytest.fixture(params=['sqlite', 'postgres'])
def community(request, tmp_path):
    if request.param == 'postgres':
        store = request.getfixturevalue('pg_factory')()
        # Parent integrates the mixins into Store for both database implementations.
        if not isinstance(store, CommunityMixin):
            pytest.skip('Community integration into PostgresStore is pending')
        return store
    bases = tuple(base for base in (AccountMixin, CommunityMixin) if not issubclass(Store, base)) + (Store,)
    cls = type('CommunityTestStore', bases, {})
    store = cls(Settings.from_values(data_dir=tmp_path, public_origin='http://localhost'))
    store.initialize()
    with store._transaction(write=True) as conn:
        for name in ('schema_accounts.sql', 'schema_community.sql'):
            execute_schema(conn, (Path(__file__).parents[1] / 'app' / name).read_text(encoding='utf-8-sig'))
        conn.execute('CREATE TABLE IF NOT EXISTS room_options(room_id TEXT PRIMARY KEY REFERENCES rooms(id),access TEXT NOT NULL DEFAULT \'public\',share_enabled INTEGER NOT NULL DEFAULT 0)')
    return store


def account(store, name=None):
    guest = store.create_session(source=secrets.token_hex(8))
    row = store.account_register(name or ('user' + secrets.token_hex(5)), 'password-test-123', secrets.token_hex(8), guest['token'], guest['csrf_token'])
    return row['token'], row['csrf_token']


def setup_room(store):
    host, actor, outsider = [account(store) for _ in range(3)]
    room_id = store.create_room(*host, 'mygo_rehearsal', None)
    store.join(*actor, room_id)
    store.claim_character(*host, room_id, 'mygo.tomori')
    store.claim_character(*actor, room_id, 'mygo.anon')
    store.control(*host, room_id, 'start')
    with store._transaction() as conn:
        mids = {who[0]: store._historical_member(conn, room_id, store._session(conn, who[0])['id'])['id'] for who in (host, actor)}
    return host, actor, outsider, room_id, mids


def denied(code, call):
    with pytest.raises(DomainError) as caught:
        call()
    assert caught.value.status_code == code


def message(store, sender, room, text='台词', channel='stage', **kw):
    return store.send_message(*sender, room, channel, text, secrets.token_hex(12), **kw)['message']


def test_ratings_unique_ended_only_participants_and_left(community):
    s = community
    host, actor, stranger, room, mids = setup_room(s)
    denied(409, lambda: s.rate_room(*actor, room, 4))
    s.leave(*actor, room)
    s.control(*host, room, 'end')
    assert s.rate_room(*actor, room, 4)['count'] == 1
    assert s.rate_room(*actor, room, 2) == {'rating': 2, 'average': 2.0, 'count': 1}
    assert s.rate_room(*host, room, 4)['average'] == 3
    denied(403, lambda: s.rate_room(*stranger, room, 5))
    denied(422, lambda: s.rate_room(*actor, room, True))


def test_private_votes_reports_and_admin_access(community):
    s = community
    host, actor, stranger, room, mids = setup_room(s)
    s.leave(*actor, room)
    assert s.member_vote(*actor, room, mids[host[0]], 1) == {'value': 1}
    assert s.member_vote(*actor, room, mids[host[0]], -1) == {'value': -1}
    denied(403, lambda: s.member_vote(*actor, room, mids[actor[0]], 1))
    denied(403, lambda: s.member_vote(*stranger, room, mids[host[0]], 1))
    result = s.report_member(*actor, room, mids[host[0]], 'harassment', 'evidence')
    assert s.report_member(*actor, room, mids[host[0]], 'harassment')['id'] == result['id']
    denied(403, lambda: s.admin_reports(host[0]))
    with s._transaction(write=True) as conn:
        conn.execute('UPDATE users SET is_admin=1 WHERE principal_session_id=:id', {'id': s._session(conn, stranger[0])['id']})
    assert s.admin_member_feedback(stranger[0])['items'][0]['value'] == -1
    report = s.admin_reports(stranger[0])['items'][0]
    assert report['snapshot']['id'] == mids[host[0]]
    assert s.admin_resolve_report(*stranger, result['id'], 'resolved', 'reviewed')['status'] == 'resolved'
    denied(409, lambda: s.admin_resolve_report(*stranger, result['id'], 'dismissed'))
    assert s.member_vote(*actor, room, mids[host[0]], 0) == {'value': 0}


def test_share_requires_each_author_stage_same_room_and_revokes(community):
    s = community
    host, actor, stranger, room, mids = setup_room(s)
    a = message(s, host, room)
    b = message(s, actor, room, text='', content_type='sticker', sticker_id=next(iter(s.stickers)))
    backstage = message(s, actor, room, channel='backstage')
    denied(403, lambda: s.create_share(*host, room, [a['id'], b['id']]))
    s.share_permission(*host, room, True)
    denied(403, lambda: s.create_share(*host, room, [a['id'], b['id']]))
    s.share_permission(*actor, room, True)
    denied(403, lambda: s.create_share(*host, room, [backstage['id']]))
    denied(403, lambda: s.create_share(*stranger, room, [a['id']]))
    denied(422, lambda: s.create_share(*host, room, [a['id'], a['id']]))
    share = s.create_share(*host, room, [b['id'], a['id']])
    detail = s.share_detail(share['slug'])
    assert detail['messages'][0]['text'] == '台词'
    assert detail['messages'][1]['sticker']['image'].startswith('/static/')
    assert all(set(row) == {'author','text','created_at','content_type','sticker','character_id'} for row in detail['messages'])
    denied(403, lambda: s.revoke_share(*stranger, share['slug']))
    s.share_permission(*actor, room, False)
    denied(404, lambda: s.share_detail(share['slug']))
    s.share_permission(*actor, room, True)
    denied(404, lambda: s.share_detail(share['slug']))
    share2 = s.create_share(*host, room, [a['id'], b['id']])
    s.revoke_share(*actor, share2['slug'])
    denied(404, lambda: s.share_detail(share2['slug']))


def test_private_sharing_feedback_and_admin_audit(community):
    s = community
    host, actor, stranger, room, mids = setup_room(s)
    a = message(s, host, room)
    s.share_permission(*host, room, True)
    with s._transaction(write=True) as conn:
        conn.execute("INSERT INTO room_options(room_id,cast_scenario_id,access,share_enabled) VALUES(:id,'mygo_rehearsal','password',0) ON CONFLICT(room_id) DO UPDATE SET access='password',share_enabled=0", {'id': room})
    denied(403, lambda: s.create_share(*host, room, [a['id']]))
    feedback = s.developer_feedback(*actor, '移动端反馈')
    denied(403, lambda: s.admin_feedback(actor[0]))
    with s._transaction(write=True) as conn:
        conn.execute('UPDATE users SET is_admin=1 WHERE principal_session_id=:id', {'id': s._session(conn, stranger[0])['id']})
        conn.execute('UPDATE room_options SET share_enabled=1 WHERE room_id=:id', {'id': room})
    share = s.create_share(*host, room, [a['id']])
    assert s.admin_feedback(stranger[0])['items'][0]['text'] == '移动端反馈'
    assert s.admin_rooms(stranger[0], user_id=s.account_identity(actor[0])['id'])['total'] == 1
    s.admin_resolve_feedback(*stranger, feedback['id'], 'resolved', 'fixed')
    s.admin_revoke_share(*stranger, share['slug'])
    with s._transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM account_audit').fetchone()[0] == 2
    denied(404, lambda: s.share_detail(share['slug']))


def test_shares_cross_room_csrf_frozen_and_admin_pagination(community):
    s = community
    host, actor, stranger, room, mids = setup_room(s)
    other = s.create_room(*stranger, 'mygo_rehearsal', None)
    extra = account(s)
    s.join(*extra, other)
    s.claim_character(*stranger, other, 'mygo.tomori')
    s.claim_character(*extra, other, 'mygo.anon')
    s.control(*stranger, other, 'start')
    foreign = message(s, stranger, other, '其他房间')
    own = message(s, host, room, '冻结正文')
    s.share_permission(*host, room, True)
    s.share_permission(*stranger, other, True)
    denied(403, lambda: s.create_share(*host, room, [foreign['id']]))
    denied(403, lambda: s.create_share(host[0], 'bad-csrf', room, [own['id']]))
    first = s.create_share(*host, room, [own['id']])
    second = s.create_share(*host, room, [own['id']])
    with s._transaction(write=True) as conn:
        conn.execute('UPDATE messages SET text=:text WHERE id=:id', {'id': own['id'], 'text': '后台修正不能改变已授权快照'})
        conn.execute('UPDATE users SET is_admin=1 WHERE principal_session_id=:id', {'id': s._session(conn, actor[0])['id']})
    assert s.share_detail(first['slug'])['messages'][0]['text'] == '冻结正文'
    page = s.admin_shares(actor[0], limit=1)
    assert len(page['items']) == 1 and page['next_cursor']
    next_page = s.admin_shares(actor[0], limit=1, cursor=page['next_cursor'])
    assert page['items'][0]['id'] != next_page['items'][0]['id']
    assert {page['items'][0]['id'], next_page['items'][0]['id']} == {first['id'],second['id']}
    s.admin_revoke_share(*actor, first['slug'])
    denied(404, lambda: s.share_detail(first['slug']))


def test_concurrent_ratings_keep_one_row_and_feedback_rate_bound(community):
    from concurrent.futures import ThreadPoolExecutor
    s = community
    host, actor, stranger, room, mids = setup_room(s)
    s.control(*host, room, 'end')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda value: s.rate_room(*actor, room, value), [1,2,3,4]))
    assert all(result['count'] == 1 for result in results)
    assert s.rate_room(*actor, room, 5)['count'] == 1
    for index in range(10):
        s.developer_feedback(*actor, f'feedback {index}')
    denied(429, lambda: s.developer_feedback(*actor, 'over the limit'))
    guest = s.create_session(source=secrets.token_hex(8))
    denied(401, lambda: s.developer_feedback(guest['token'], guest['csrf_token'], 'guest feedback'))
