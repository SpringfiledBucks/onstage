"""Account identities layered over stable legacy room principals.

Preferences are a bounded JSON array of catalog character IDs stored as TEXT.
Login secrets exist only in auth_sessions as hashes; principal sessions have an
unrecoverable random token and never contain the private login username.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import unicodedata
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from sqlalchemy.exc import IntegrityError

from .security import DomainError, iso_time, new_id, parse_time, token_hash, token_matches, utc_now

PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))


def normalize_username(value):
    if not isinstance(value, str):
        raise DomainError(422, '用户名须为文字。')
    value = unicodedata.normalize('NFKC', value).strip()
    if not 3 <= len(value) <= 32 or any(
        not (char.isalnum() or char in '_-.') for char in value
    ):
        raise DomainError(422, '用户名须为 3–32 个字母、数字、下划线、点或短横线。')
    return value, value.casefold()


def validate_password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 128:
        raise DomainError(422, '密码须为 8–128 个字符。')
    return value


class AccountMixin:
    def _session(self, connection, token, csrf=None, *, require_csrf=False):
        if not token or len(token) > 256:
            raise DomainError(401, '会话尚未建立。')
        auth = connection.execute(
            'SELECT a.*,u.principal_session_id,u.banned FROM auth_sessions a '
            'JOIN users u ON u.id=a.user_id WHERE a.token_hash=:hash',
            {'hash': token_hash(token)},
        ).fetchone()
        if auth is None:
            return super()._session(connection, token, csrf, require_csrf=require_csrf)
        if auth['banned']:
            raise DomainError(403, '账号已被封禁。')
        if auth['revoked_at'] or parse_time(auth['expires_at']) <= utc_now():
            raise DomainError(401, '登录已失效，请重新登录。')
        if require_csrf and (not csrf or not token_matches(csrf, auth['csrf_token'])):
            raise DomainError(403, 'CSRF 校验失败。')
        principal = connection.execute('SELECT * FROM sessions WHERE id=:id',
                                       {'id': auth['principal_session_id']}).fetchone()
        result = dict(principal)
        result.update(csrf_token=auth['csrf_token'], account_id=auth['user_id'],
                      auth_session_id=auth['id'], expires_at=auth['expires_at'])
        return result

    def _account(self, connection, session, admin=False):
        row = connection.execute('SELECT * FROM users WHERE principal_session_id=:id',
                                 {'id': session['id']}).fetchone()
        if row is None:
            raise DomainError(401, '此操作需要登录账号。')
        if row['banned'] or (admin and not row['is_admin']):
            raise DomainError(403, '没有此操作的权限。')
        return row

    @staticmethod
    def _private_account(row):
        return {'id': row['id'], 'username': row['username'], 'admin': bool(row['is_admin']),
                'banned': bool(row['banned']), 'preferences': json.loads(row['preferences_json']),
                'favorite_character_ids': json.loads(row['preferences_json']),
                'onboarding_done': bool(row['onboarding_done']), 'created_at': row['created_at']}

    def account_identity(self, token, csrf=None, admin=False, write=False):
        with self._transaction() as conn:
            session = self._session(conn, token, csrf, require_csrf=write)
            result = self._private_account(self._account(conn, session, admin))
            result['csrf_token'] = session['csrf_token']
            result['storage_scope'] = token_hash('account:' + session['id'])
            return result

    def get_session(self, token):
        with self._transaction() as conn:
            session = self._session(conn, token)
            result = {'authenticated': True, 'csrf_token': session['csrf_token'],
                      'version': self.settings.version, 'nickname': session['nickname'] or None,
                      'requires_nickname': not bool(session['nickname'])}
            if 'account_id' in session:
                result['user'] = self._private_account(self._account(conn, session))
                result['storage_scope'] = token_hash('account:' + session['id'])
            return result

    def _auth_attempt(self, action, source, username_key, token, csrf):
        # Own committed transaction: invalid passwords and duplicate signups must
        # consume attempts even when a later business transaction rolls back.
        with self._transaction(write=True, admission='accounts') as conn:
            self._session(conn, token, csrf, require_csrf=True)
            for scope, limit in [('ip:' + source, 20), ('name:' + username_key, 8)]:
                key = token_hash(scope)
                self._check_rate(conn, action, key, limit, 900)
                self._record_rate(conn, action, key)

    def _new_auth(self, conn, user, remember=False):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = utc_now()
        expires = iso_time(now + timedelta(days=30) if remember else now + timedelta(hours=12))
        conn.execute('INSERT INTO auth_sessions(id,user_id,token_hash,csrf_token,created_at,'
                     'expires_at,last_seen_at,remember) VALUES(:id,:user,:hash,:csrf,:now,:expires,:now,:remember)',
                     {'id': new_id(), 'user': user['id'], 'hash': token_hash(token), 'csrf': csrf,
                      'now': iso_time(now), 'expires': expires, 'remember': int(remember)})
        return {'token': token, 'csrf_token': csrf, 'expires_at': expires,
                'remember': bool(remember), 'user': self._private_account(user),
                'storage_scope': token_hash('account:' + user['principal_session_id'])}

    @staticmethod
    def _consume_bootstrap(conn, session):
        # Consume only a disposable pre-auth session. Preserve any old guest
        # principal with historical room ownership for controlled transfer.
        if 'account_id' not in session:
            conn.execute('UPDATE sessions SET revoked_at=:now WHERE id=:id '
                         'AND NOT EXISTS(SELECT 1 FROM room_members WHERE session_id=:id) '
                         'AND NOT EXISTS(SELECT 1 FROM users WHERE principal_session_id=:id)',
                         {'now': iso_time(), 'id': session['id']})

    def account_register(self, username, password, source, token=None, csrf=None):
        username, key = normalize_username(username)
        password = validate_password(password)
        self._auth_attempt('account-register', source, key, token, csrf)
        encoded = PASSWORDS.hash(password)  # deliberately outside write transaction
        try:
            with self._transaction(write=True, admission='accounts') as conn:
                bootstrap = self._session(conn, token, csrf, require_csrf=True)
                uid, principal, now = new_id(), new_id(), iso_time()
                conn.execute('INSERT INTO sessions(id,token_hash,csrf_token,created_at,expires_at,nickname) '
                             'VALUES(:id,:hash,:csrf,:now,:expires,:nickname)',
                             {'id': principal, 'hash': token_hash(secrets.token_urlsafe(64)),
                              'csrf': secrets.token_urlsafe(32), 'now': now,
                              'expires': '9999-01-01T00:00:00.000000Z',
                              'nickname': '演员' + secrets.token_hex(3)})
                conn.execute('INSERT INTO users(id,username,username_key,password_hash,principal_session_id,created_at) '
                             'VALUES(:id,:name,:key,:password,:principal,:now)',
                             {'id': uid, 'name': username, 'key': key, 'password': encoded,
                              'principal': principal, 'now': now})
                user = conn.execute('SELECT * FROM users WHERE id=:id', {'id': uid}).fetchone()
                self._consume_bootstrap(conn, bootstrap)
                return self._new_auth(conn, user)
        except (sqlite3.IntegrityError, IntegrityError):
            raise DomainError(409, '用户名已被使用。') from None

    def account_login(self, username, password, remember=False, source='unknown', token=None, csrf=None):
        _, key = normalize_username(username)
        password = validate_password(password)
        if not isinstance(remember, bool):
            raise DomainError(422, '记住我须为布尔值。')
        self._auth_attempt('account-login', source, key, token, csrf)
        with self._transaction() as conn:
            user = conn.execute('SELECT * FROM users WHERE username_key=:key', {'key': key}).fetchone()
        try:
            valid = PASSWORDS.verify(user['password_hash'] if user else _DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            valid = False
        if not user or not valid:
            raise DomainError(401, '用户名或密码错误。')
        with self._transaction(write=True, admission='accounts') as conn:
            bootstrap = self._session(conn, token, csrf, require_csrf=True)
            current = conn.execute('SELECT * FROM users WHERE id=:id', {'id': user['id']}).fetchone()
            if current['banned']:
                raise DomainError(403, '账号已被封禁。')
            self._consume_bootstrap(conn, bootstrap)
            return self._new_auth(conn, current, remember)

    def account_logout(self, token, csrf):
        with self._transaction(write=True, admission='accounts') as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            self._account(conn, session)
            conn.execute('UPDATE auth_sessions SET revoked_at=:now WHERE id=:id',
                         {'now': iso_time(), 'id': session['auth_session_id']})
            conn.execute('DELETE FROM push_subscriptions WHERE auth_session_id=:id', {'id': session['auth_session_id']})
            return {'ok': True}

    def logout(self, token, csrf):
        # Existing logout endpoint returns the stable principal for Hub cleanup.
        # Revoking the unreachable principal itself would log out other devices.
        with self._transaction(write=True, admission='accounts') as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            if 'auth_session_id' in session:
                conn.execute('UPDATE auth_sessions SET revoked_at=:now WHERE id=:id',
                             {'now': iso_time(), 'id': session['auth_session_id']})
                conn.execute('DELETE FROM push_subscriptions WHERE auth_session_id=:id', {'id': session['auth_session_id']})
            else:
                conn.execute('UPDATE sessions SET revoked_at=:now WHERE id=:id',
                             {'now': iso_time(), 'id': session['id']})
            return session['id']

    def set_preferences(self, token, csrf, character_ids):
        if (not isinstance(character_ids, list) or len(character_ids) > len(self.characters)
                or any(not isinstance(item, str) or item not in self.characters for item in character_ids)
                or len(set(character_ids)) != len(character_ids)):
            raise DomainError(422, '角色偏好不合法。')
        with self._transaction(write=True, admission='accounts') as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            user = self._account(conn, session)
            conn.execute('UPDATE users SET preferences_json=:prefs,onboarding_done=1 WHERE id=:id',
                         {'prefs': json.dumps(character_ids), 'id': user['id']})
        return self.account_identity(token)

    def account_heartbeat(self, token, csrf):
        with self._transaction(write=True, session_token=token) as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            self._account(conn, session)
            conn.execute('UPDATE auth_sessions SET last_seen_at=:now WHERE id=:id',
                         {'now': iso_time(), 'id': session['auth_session_id']})
        return {'ok': True, 'lease_seconds': 90}

    @staticmethod
    def _account_online_clause():
        return ("(users.banned=0 AND (EXISTS(SELECT 1 FROM auth_sessions a WHERE a.user_id=users.id "
                "AND a.revoked_at IS NULL AND a.expires_at>:now AND a.last_seen_at>:cutoff) "
                "OR EXISTS(SELECT 1 FROM room_presence_leases p "
                "JOIN auth_sessions a ON a.id=p.auth_session_id "
                "JOIN room_members m ON m.room_id=p.room_id AND m.session_id=p.session_id "
                "WHERE a.user_id=users.id AND a.revoked_at IS NULL AND a.expires_at>:now "
                "AND p.session_id=users.principal_session_id AND p.expires_at>:now AND m.status='active' "
                "AND NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=p.room_id AND b.session_id=p.session_id))))")

    def admin_users(self, token, limit=50, cursor=None, search=None, banned=None, online=None):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DomainError(422, '分页大小须为 1–100。')
        if online is not None and not isinstance(online, bool):
            raise DomainError(422, '在线筛选须为布尔值。')
        if search is not None and (not isinstance(search, str) or len(search) > 64):
            raise DomainError(422, '搜索内容过长。')
        if banned is not None and not isinstance(banned, bool):
            raise DomainError(422, '封禁筛选须为布尔值。')
        with self._transaction() as conn:
            session = self._session(conn, token)
            self._account(conn, session, admin=True)
            clauses, args = [], {'limit': limit + 1, 'now': iso_time(),
                                 'cutoff': iso_time(utc_now() - timedelta(seconds=90))}
            if search:
                clauses.append("username_key LIKE :search ESCAPE '!' ")
                key = unicodedata.normalize('NFKC', search).casefold()
                args['search'] = '%' + key.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            if banned is not None:
                clauses.append('banned=:banned')
                args['banned'] = int(banned)
            if online is not None:
                clauses.append(('' if online else 'NOT ') + self._account_online_clause())
            base = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            total = conn.execute('SELECT COUNT(*) FROM users' + base, args).fetchone()[0]
            if cursor:
                clauses.append('id>:cursor')
                args['cursor'] = cursor
            where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            rows = conn.execute('SELECT users.*,CASE WHEN ' + self._account_online_clause() +
                                ' THEN 1 ELSE 0 END AS online FROM users' + where + ' ORDER BY id LIMIT :limit', args).fetchall()
            return {'users': [{**self._private_account(row), 'online': bool(row['online'])} for row in rows[:limit]], 'total': total,
                    'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    def admin_ban(self, token, csrf, user_id, banned, note):
        if not isinstance(banned, bool) or not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
            raise DomainError(422, '封禁状态或处理说明不合法。')
        with self._transaction(write=True, admission='accounts') as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            actor = self._account(conn, session, admin=True)
            user = conn.execute('SELECT * FROM users WHERE id=:id', {'id': user_id}).fetchone()
            if user is None:
                raise DomainError(404, '账号不存在。')
            if user['id'] == actor['id']:
                raise DomainError(409, '不能封禁自己的管理账号。')
            conn.execute('UPDATE users SET banned=:banned WHERE id=:id', {'banned': int(banned), 'id': user_id})
            if banned:
                conn.execute('UPDATE auth_sessions SET revoked_at=:now WHERE user_id=:id AND revoked_at IS NULL',
                             {'now': iso_time(), 'id': user_id})
                conn.execute('DELETE FROM push_subscriptions WHERE user_id=:id', {'id': user_id})
            self._account_audit(conn, actor['id'], user_id, 'ban' if banned else 'unban', note.strip())
            return {'id': user_id, 'banned': banned, 'principal_session_id': user['principal_session_id']}

    @staticmethod
    def _account_audit(conn, actor, target, action, note):
        conn.execute('INSERT INTO account_audit(id,actor_user_id,target_id,action,note,created_at) '
                     'VALUES(:id,:actor,:target,:action,:note,:now)',
                     {'id': new_id(), 'actor': actor, 'target': target, 'action': action, 'note': note, 'now': iso_time()})

    def operator_grant_admin(self, username, *, enabled=True, operator):
        _, key = normalize_username(username)
        if not isinstance(operator, str) or not 1 <= len(operator.strip()) <= 200:
            raise DomainError(422, '须提供维护者审计标识。')
        with self._transaction(write=True, admission='accounts') as conn:
            user = conn.execute('SELECT * FROM users WHERE username_key=:key', {'key': key}).fetchone()
            if user is None:
                raise DomainError(404, '账号不存在。')
            conn.execute('UPDATE users SET is_admin=:enabled WHERE id=:id', {'enabled': int(enabled), 'id': user['id']})
            self._account_audit(conn, None, user['id'], 'grant_admin' if enabled else 'revoke_admin', operator.strip())
            return {'id': user['id'], 'admin': enabled}
