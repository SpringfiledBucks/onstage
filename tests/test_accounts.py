"""Shared account contracts on SQLite and opt-in isolated real PostgreSQL."""
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.accounts import AccountMixin
from app.postgres import PostgresStore, postgres_url
from app.security import DomainError, Settings, token_hash
from app.store import Store


@pytest.fixture(params=['sqlite', 'postgres'])
def accounts(request, tmp_path):
    settings = Settings.from_values(data_dir=tmp_path, public_origin='http://localhost')
    admin = schema = None
    base = Store
    if request.param == 'postgres':
        value = os.environ.get('ONSTAGE_TEST_DATABASE_URL')
        if not value:
            pytest.skip('ONSTAGE_TEST_DATABASE_URL required for real PostgreSQL')
        admin = create_engine(postgres_url(value))
        schema = 'onstage_accounts_' + secrets.token_hex(8)
        with admin.begin() as conn:
            conn.execute(text('CREATE SCHEMA ' + schema))
        url = make_url(postgres_url(value)).update_query_dict({'options': '-csearch_path=' + schema})
        settings = Settings.from_values(data_dir=tmp_path, public_origin='http://localhost',
                                        database_url=url.render_as_string(hide_password=False))
        base = PostgresStore
    cls = base if issubclass(base, AccountMixin) else type('TestAccounts', (AccountMixin, base), {})
    store = cls(settings)
    try:
        if schema:
            store.migrate()
        store.initialize()
        with store._transaction(write=True) as conn:
            for statement in (Path(__file__).parents[1] / 'app/schema_accounts.sql').read_text().split(';'):
                if statement.strip():
                    conn.execute(statement)
        yield store
    finally:
        store.close()
        if schema:
            with admin.begin() as conn:
                conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
            admin.dispose()


def guest(store):
    value = store.create_session(source=secrets.token_hex(8), nickname='访客')
    return {'token': value['token'], 'csrf': value['csrf_token']}


def register(store, name='TestUser'):
    return store.account_register(name, 'safe-password-123', 'test', **guest(store))


def test_username_canonical_unique_and_password_storage(accounts):
    value = register(accounts, 'ＴｅｓｔUser')
    with pytest.raises(DomainError) as error:
        register(accounts, 'testuser')
    assert error.value.status_code == 409
    with accounts._transaction() as conn:
        row = conn.execute('SELECT * FROM users WHERE id=:id', {'id': value['user']['id']}).fetchone()
        principal = conn.execute('SELECT * FROM sessions WHERE id=:id', {'id': row['principal_session_id']}).fetchone()
        assert row['password_hash'].startswith('$argon2id$')
        assert principal['nickname'] != value['user']['username']
        assert principal['token_hash'] != token_hash(value['token'])
        assert conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_registration_requires_preauth_csrf(accounts):
    with pytest.raises(DomainError) as error:
        accounts.account_register('NoCSRF', 'safe-password-123', 'test', **{**guest(accounts), 'csrf': 'wrong'})
    assert error.value.status_code == 403


def test_two_logins_share_principal_scope_and_logout_is_per_device(accounts):
    first = register(accounts)
    second = accounts.account_login('testuser', 'safe-password-123', True, 'other', **guest(accounts))
    assert first['storage_scope'] == second['storage_scope']
    assert first['csrf_token'] != second['csrf_token']
    room_id = accounts.create_room(first['token'], first['csrf_token'], 'mygo_rehearsal', None)
    accounts.claim_character(first['token'], first['csrf_token'], room_id, 'mygo.tomori')
    snapshot = accounts.get_room(second['token'], room_id, set())['room']
    assert snapshot['me']['is_host']
    assert snapshot['me']['character_id'] == 'mygo.tomori'
    with accounts._transaction() as conn:
        left = accounts._session(conn, first['token'])
        right = accounts._session(conn, second['token'])
        assert left['id'] == right['id']
        assert left['csrf_token'] != right['csrf_token']
    accounts.logout(first['token'], first['csrf_token'])
    with pytest.raises(DomainError):
        accounts.account_identity(first['token'])
    assert accounts.account_identity(second['token'])['username'] == 'TestUser'
    assert accounts.get_session(second['token'])['storage_scope'] == first['storage_scope']


def test_failed_logins_persist_rate_limit(accounts):
    register(accounts)
    preauth = guest(accounts)
    for _ in range(8):
        with pytest.raises(DomainError) as error:
            accounts.account_login('testuser', 'wrong-password-123', source='bad', **preauth)
        assert error.value.status_code == 401
    with pytest.raises(DomainError) as error:
        accounts.account_login('testuser', 'safe-password-123', source='bad', **preauth)
    assert error.value.status_code == 429


def test_admin_ban_all_sessions_preferences_and_visibility(accounts):
    admin, user = register(accounts, 'AdminUser'), register(accounts)
    second = accounts.account_login('TestUser', 'safe-password-123', source='another', **guest(accounts))
    accounts.operator_grant_admin('adminuser', operator='test-operator')
    accounts.set_preferences(user['token'], user['csrf_token'], ['mygo.tomori'])
    assert accounts.account_identity(user['token'])['favorite_character_ids'] == ['mygo.tomori']
    with pytest.raises(DomainError) as error:
        accounts.admin_users(user['token'])
    assert error.value.status_code == 403
    listing = accounts.admin_users(admin['token'], limit=1)
    assert listing['total'] == 2 and listing['next_cursor']
    accounts.admin_ban(admin['token'], admin['csrf_token'], user['user']['id'], True, 'test moderation')
    for result in [user, second]:
        with pytest.raises(DomainError) as error:
            accounts.account_identity(result['token'])
        assert error.value.status_code == 403
    accounts.admin_ban(admin['token'], admin['csrf_token'], user['user']['id'], False, 'review complete')
    with pytest.raises(DomainError) as error:
        accounts.account_identity(user['token'])
    assert error.value.status_code == 401  # unban does not revive revoked sessions


def test_online_lease_expiry_and_heartbeat(accounts):
    admin, user = register(accounts, 'AdminUser'), register(accounts)
    accounts.operator_grant_admin('AdminUser', operator='test')
    assert accounts.admin_users(admin['token'], search='TestUser', online=True)['total'] == 1
    with accounts._transaction(write=True) as conn:
        conn.execute("UPDATE auth_sessions SET last_seen_at='2000-01-01T00:00:00.000000Z' WHERE user_id=:id",
                     {'id': user['user']['id']})
    assert accounts.admin_users(admin['token'], search='TestUser', online=False)['total'] == 1
    accounts.account_heartbeat(user['token'], user['csrf_token'])
    result = accounts.admin_users(admin['token'], search='TestUser', online=True)
    assert result['users'][0]['online'] is True


def test_concurrent_registration_unique(accounts):
    preauths = [guest(accounts), guest(accounts)]
    def attempt(index):
        try:
            accounts.account_register('Concurrent', 'safe-password-123', str(index), **preauths[index])
            return 201
        except DomainError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [0, 1])) == [201, 409]
