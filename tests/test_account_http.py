"""Default account-mode HTTP/WS chain, independently of legacy guest fixtures."""
import secrets

import pytest
from fastapi.testclient import TestClient

COOKIE_NAME = '__Host-onstage_session'
from test_postgres import pg_factory

WEB = "https://play.neboer.site"
ADMIN = "https://admin.neboer.site"
OTHER = "https://untrusted.neboer.site"


@pytest.fixture
def web(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ONSTAGE_FRONTEND_ORIGINS", WEB)
    monkeypatch.setenv("ONSTAGE_ADMIN_ORIGINS", ADMIN)
    monkeypatch.setenv("ONSTAGE_METRICS_TOKEN", "isolated-metrics-test")
    monkeypatch.delenv("ONSTAGE_LEGACY_GUESTS", raising=False)
    from app.main import create_app
    url = request.getfixturevalue("pg_factory")().settings.database_url
    app = create_app(data_dir=tmp_path, database_url=url, public_origin="https://api.neboer.site", secure_cookie=True)
    with TestClient(app, base_url="https://api.neboer.site") as client:
        yield client, app.state.store


def account(client, name=None):
    client.cookies.clear()
    guest = client.post('/api/session', json={}, headers={'Origin': WEB})
    assert guest.status_code == 200, guest.text
    csrf = guest.json()['csrf_token']
    response = client.post('/api/auth/register', json={'username': name or 'test' + secrets.token_hex(5), 'password': 'password-test-123'},
                           headers={'Origin': WEB, 'X-CSRF-Token': csrf})
    assert response.status_code == 200, response.text
    cookie = client.cookies.get(COOKIE_NAME)
    headers = {'Origin': WEB, 'Cookie': COOKIE_NAME + '=' + cookie, 'X-CSRF-Token': response.json()['csrf_token']}
    return headers, response.json()


def post(client, path, headers, body=None):
    response = client.post(path, json=body or {}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_visitor_and_cors_trust_boundaries(web):
    client, store = web
    assert client.get('/api/catalog', headers={'Origin': OTHER}).status_code == 200
    assert client.get('/api/rooms', headers={'Origin': OTHER}).headers.get('access-control-allow-credentials') is None
    assert client.post('/api/rooms', json={'scenario_id': 'mygo_rehearsal'}, headers={'Origin': WEB}).status_code == 401
    for evil in ('https://neboer.site.evil.org', 'https://evilneboer.site', 'null', 'https://x.neboer.site:444'):
        assert client.get('/api/catalog', headers={'Origin': evil}).status_code == 403
    preflight = client.options('/api/auth/login', headers={'Origin': WEB, 'Access-Control-Request-Method': 'POST',
                                                          'Access-Control-Request-Headers': 'Content-Type,X-CSRF-Token'})
    assert preflight.status_code == 200
    assert preflight.headers['access-control-allow-origin'] == WEB
    assert preflight.headers['access-control-allow-credentials'] == 'true'
    assert client.options('/api/auth/login', headers={'Origin': OTHER, 'Access-Control-Request-Method': 'POST'}).status_code == 403
    host, me = account(client)
    room = post(client, '/api/rooms', host, {'scenario_id': 'mygo_rehearsal'})['room']
    # An untrusted subdomain must never consume a deliberately supplied account Cookie.
    hostile = {**host, 'Origin': OTHER}
    assert client.get('/api/rooms?filter=mine', headers=hostile).status_code == 401
    assert client.get('/api/rooms/' + room['id'], headers=hostile).status_code == 403
    assert client.get('/api/admin/users', headers=host).status_code == 403
    store.operator_grant_admin(me['username'], operator='isolated-test')
    assert client.get('/api/admin/users', headers=host).status_code == 403
    admin = {**host, 'Origin': ADMIN}
    users = client.get('/api/admin/users', headers=admin)
    assert users.status_code == 200, users.text
    assert users.json()['total'] == 1
    assert client.get('/metrics').status_code == 403
    assert client.get('/metrics', headers={'Authorization': 'Bearer isolated-metrics-test'}).status_code == 200


def test_account_room_chat_share_admin_and_revocation_chain(web):
    client, store = web
    host, owner = account(client, 'privateOwner' + secrets.token_hex(3))
    actor, person = account(client, 'privateActor' + secrets.token_hex(3))
    outsider, _ = account(client)
    room = post(client, '/api/rooms', host, {'scenario_id': 'mygo_rehearsal'})['room']
    rid = room['id']
    prefix = '/api/rooms/' + rid
    post(client, prefix + '/access', host, {'access': 'password', 'password': 'room-password-123'})
    assert client.get(prefix, headers=outsider).status_code == 403
    assert client.get('/api/rooms', headers=outsider).json()['rooms'] == []
    post(client, prefix + '/unlock', actor, {'password': 'room-password-123'})
    post(client, prefix + '/join', actor)
    post(client, prefix + '/characters/claim', host, {'character_id': 'mygo.tomori'})
    post(client, prefix + '/characters/claim', actor, {'character_id': 'mygo.anon'})
    post(client, prefix + '/control', host, {'action': 'start'})
    body = {'channel': 'stage', 'text': 'isolated stage', 'client_message_id': secrets.token_hex(12)}
    message = post(client, prefix + '/messages', actor, body)['message']
    sync = client.get(prefix + '/sync', headers=host).json()
    assert message['client_message_id'] == body['client_message_id']
    assert post(client, prefix + '/messages', actor, body)['message']['id'] == message['id']
    assert person['username'] not in str(sync) and owner['username'] not in str(sync)
    scene = post(client, prefix + '/scene', host, {'scenario_id': 'planetarium', 'action_id': 'scene-change-01'})
    assert not post(client, prefix + '/scene', host, {'scenario_id': 'planetarium', 'action_id': 'scene-change-01'})['changed']
    current = client.get(prefix, headers=host).json()['room']
    actor_id = next(member['id'] for member in current['members'] if member['character_id'] == 'mygo.anon')
    assert current['scenario_id'] == 'planetarium' and scene['changed']
    assert client.post(prefix + '/shares', headers=host, json={'message_ids': [message['id']]}).status_code == 403
    post(client, prefix + '/share-permission', actor, {'allowed': True})
    post(client, prefix + '/access', host, {'access': 'password', 'password': 'room-password-123', 'share_enabled': True})
    share = post(client, prefix + '/shares', host, {'message_ids': [message['id']]})
    public = client.get('/api/shares/' + share['slug'], headers={'Origin': OTHER})
    assert public.status_code == 200
    assert person['username'] not in public.text and message['client_message_id'] not in public.text
    post(client, prefix + '/share-permission', actor, {'allowed': False})
    assert client.get('/api/shares/' + share['slug']).status_code in {404, 410}
    post(client, prefix + '/members/' + actor_id + '/vote', host, {'value': 1})
    post(client, prefix + '/members/' + actor_id + '/report', host, {'reason': 'other', 'details': 'isolated test'})
    assert client.get(prefix + '/reports', headers=host).status_code == 403
    post(client, prefix + '/control', host, {'action': 'end'})
    post(client, prefix + '/leave', actor)
    post(client, prefix + '/rating', actor, {'rating': 5})
    post(client, prefix + '/visibility', host, {'hidden': True})
    assert client.get('/api/rooms', headers=host).json()['rooms'] == []
    participants = client.get(prefix + '/participants', headers=actor).json()
    assert participants['me']['member_id'] == actor_id
    store.operator_grant_admin(owner['username'], operator='isolated-test')
    admin = {**host, 'Origin': ADMIN}
    report = client.get('/api/admin/reports', headers=admin).json()['items'][0]
    post(client, '/api/admin/reports/' + report['id'] + '/resolve', admin, {'status': 'resolved', 'note': 'isolated test'})
    post(client, '/api/admin/users/' + person['id'] + '/ban', admin, {'banned': True, 'note': 'isolated test'})
    assert client.get('/api/auth/me', headers=actor).status_code == 403
    audits = client.get('/api/admin/audit', headers=admin).json()['items']
    assert any(item['action'] == 'ban' for item in audits)
    assert client.get('/api/admin/audit', headers=actor).status_code == 403


def test_private_creation_is_atomic_and_expired_login_keeps_public_reads(web):
    client, store = web
    host, _ = account(client)
    outsider, _ = account(client)
    private = post(client, '/api/rooms', host, {'scenario_id': 'mygo_rehearsal', 'access': 'password', 'password': 'private-at-creation'})['room']
    assert private['access'] == 'password' and not private['share_enabled']
    assert client.get('/api/rooms', headers=outsider).json()['rooms'] == []
    assert client.get('/api/rooms/' + private['id'], headers=outsider).status_code == 403
    assert client.post('/api/rooms/' + private['id'] + '/join', headers=outsider, json={}).status_code == 403
    post(client, '/api/rooms/' + private['id'] + '/unlock', outsider, {'password': 'private-at-creation'})
    post(client, '/api/rooms/' + private['id'] + '/join', outsider)
    stale = {**host, 'Cookie': COOKIE_NAME + '=expired-invalid-test-token'}
    assert client.get('/api/rooms', headers=stale).status_code == 200
    assert client.get('/api/rooms?filter=mine', headers=stale).status_code == 401
    # Old unprefixed cookies cannot inject an account into the production API.
    assert client.get('/api/auth/me', headers={**host, 'Cookie': host['Cookie'].replace(COOKIE_NAME, 'onstage_session')}).status_code == 401
