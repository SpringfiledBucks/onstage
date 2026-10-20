"""Room access, stable directory pagination, scene changes and durable presence.

All state changes run in the store's existing short transaction and room lock.
This module is shared by SQLite compatibility tests and the PostgreSQL runtime.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

from .security import DomainError, iso_time, new_id, parse_time, token_hash, utc_now


_PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


class RoomFeaturesMixin:
    def _options(self, connection, room, *, create=False):
        row = connection.execute('SELECT * FROM room_options WHERE room_id=:id', {'id': room['id']}).fetchone()
        if row is not None:
            return dict(row)
        if create:
            connection.execute(
                'INSERT INTO room_options(room_id,cast_scenario_id) VALUES(:id,:scene) ON CONFLICT(room_id) DO NOTHING',
                {'id': room['id'], 'scene': room['scenario_id']})
            return dict(connection.execute('SELECT * FROM room_options WHERE room_id=:id', {'id': room['id']}).fetchone())
        return {'room_id': room['id'], 'access': 'public', 'hidden': 0, 'share_enabled': 1,
                'cast_scenario_id': room['scenario_id'], 'recruitment_json': '[]', 'offline_since': None}

    def _not_banned(self, connection, room_id, session_id):
        super()._not_banned(connection, room_id, session_id)
        room = self._room(connection, room_id)
        options = self._options(connection, room)
        if options['access'] != 'public' and room['host_session_id'] != session_id:
            member = connection.execute('SELECT 1 FROM room_members WHERE room_id=:r AND session_id=:s', {'r': room_id, 's': session_id}).fetchone()
            grant = connection.execute('SELECT 1 FROM room_access_grants WHERE room_id=:r AND session_id=:s', {'r': room_id, 's': session_id}).fetchone()
            if member is None and grant is None:
                raise DomainError(403, '此房间需要邀请或密码。')

    def _snapshot(self, connection, room, session_id, online, **kwargs):
        cached_options = kwargs.pop('options_override', None)
        result = super()._snapshot(connection, room, session_id, online, **kwargs)
        options = cached_options if cached_options is not None else self._options(connection, room)
        member_rows = kwargs.get('member_rows')
        if member_rows is not None:
            occupied = {row['character_id'] for row in member_rows if row['character_id']}
            recruitment = [role for role in json.loads(options['recruitment_json']) if role not in occupied]
            if room['status'] == 'ended' or (room['status'] != 'preparing' and not room['allow_join']):
                recruitment = []
        else:
            recruitment = self._vacant_recruitment(connection, [dict(room, recruitment_json=options['recruitment_json'])])[room['id']]
        result.update(access=options['access'], hidden=bool(options['hidden']),
                      share_enabled=bool(options['share_enabled']),
                      cast_scenario_id=options['cast_scenario_id'],
                      recruitment=recruitment)
        # A room host is not a site moderator and cannot inspect reports.
        if result.get('me') and os.environ.get('ONSTAGE_LEGACY_GUESTS') != '1':
            result['me']['can_manage_reports'] = False
        return result

    @staticmethod
    def _vacant_recruitment(connection, rooms):
        occupied = {}
        for offset in range(0, len(rooms), 100):
            binds = {'r' + str(i): room['id'] for i, room in enumerate(rooms[offset:offset + 100])}
            if not binds:
                continue
            rows = connection.execute("SELECT room_id,character_id FROM room_members WHERE status='active' AND character_id IS NOT NULL AND room_id IN (" + ','.join(':' + key for key in binds) + ')', binds).fetchall()
            for row in rows:
                occupied.setdefault(row['room_id'], set()).add(row['character_id'])
        return {room['id']: [role for role in json.loads(room['recruitment_json']) if role not in occupied.get(room['id'], set())] for room in rooms}

    def _validate_character(self, connection, room, member, character_id):
        cast = self._options(connection, room)['cast_scenario_id']
        original = dict(room)
        original['scenario_id'] = cast
        return super()._validate_character(connection, original, member, character_id)

    def _host_operation(self, connection, token, csrf, room_id):
        session = self._session(connection, token, csrf, require_csrf=True)
        self._account(connection, session)
        room = self._room(connection, room_id)
        self._host(connection, room, session['id'])
        return session, room, self._options(connection, room, create=True)

    def configure_room_access(self, token, csrf, room_id, access, password=None):
        if access not in {'public', 'password', 'invite'}:
            raise DomainError(422, '访问方式不合法。')
        encoded = None
        if access == 'password':
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise DomainError(422, '房间密码须为 8–128 个字符。')
            encoded = _PASSWORDS.hash(password)
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            _, room, _ = self._host_operation(c, token, csrf, room_id)
            if room['status'] == 'ended':
                raise DomainError(409, '对局已结束。')
            c.execute('UPDATE room_options SET access=:a,password_hash=:p,share_enabled=:share WHERE room_id=:r',
                      {'a': access, 'p': encoded, 'share': int(access == 'public'), 'r': room_id})
            c.execute('DELETE FROM room_access_grants WHERE room_id=:r', {'r': room_id})
            c.execute('UPDATE room_invites SET revoked_at=:now WHERE room_id=:r AND revoked_at IS NULL', {'now': iso_time(), 'r': room_id})
            c.execute('UPDATE rooms SET revision=revision+1 WHERE id=:r', {'r': room_id})
        return {'access': access, 'share_enabled': access == 'public'}

    def configure_sharing(self, token, csrf, room_id, enabled):
        if type(enabled) is not bool:
            raise DomainError(422, '分享设置不合法。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            self._host_operation(c, token, csrf, room_id)
            c.execute('UPDATE room_options SET share_enabled=:v WHERE room_id=:r', {'v': int(enabled), 'r': room_id})
            c.execute('UPDATE rooms SET revision=revision+1 WHERE id=:r', {'r': room_id})
        return {'share_enabled': enabled}

    def create_invite(self, token, csrf, room_id, expires_hours=24, max_uses=1):
        if type(expires_hours) is not int or not 1 <= expires_hours <= 168 or type(max_uses) is not int or not 1 <= max_uses <= 100:
            raise DomainError(422, '邀请有效期或使用次数不合法。')
        secret = secrets.token_urlsafe(32)
        expires = iso_time(utc_now() + timedelta(hours=expires_hours))
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            _, room, options = self._host_operation(c, token, csrf, room_id)
            if options['access'] == 'public' or room['status'] == 'ended':
                raise DomainError(409, '仅未结束的私密房间需要邀请。')
            c.execute('DELETE FROM room_invites WHERE room_id=:r AND expires_at<:now', {'r': room_id, 'now': iso_time()})
            count = c.execute('SELECT COUNT(*) FROM room_invites WHERE room_id=:r AND revoked_at IS NULL AND uses<max_uses', {'r': room_id}).fetchone()[0]
            if count >= 100:
                raise DomainError(429, '未使用邀请数量已达上限。')
            c.execute('INSERT INTO room_invites(id,room_id,token_hash,expires_at,max_uses) VALUES(:id,:r,:hash,:expires,:uses)',
                      {'id': new_id(), 'r': room_id, 'hash': token_hash(secret), 'expires': expires, 'uses': max_uses})
        return {'invite': secret, 'expires_at': expires, 'max_uses': max_uses}

    def room_unlock(self, token, csrf, room_id, password=None, invite=None):
        if password is not None and (not isinstance(password, str) or len(password) > 128):
            raise DomainError(422, '密码不合法。')
        if invite is not None and (not isinstance(invite, str) or len(invite) > 128):
            raise DomainError(422, '邀请不合法。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            session = self._session(c, token, csrf, require_csrf=True)
            self._account(c, session)
            super()._not_banned(c, room_id, session['id'])
            room = self._room(c, room_id)
            options = self._options(c, room)
            if room['status'] == 'ended':
                raise DomainError(409, '对局已结束。')
            self._check_rate(c, 'room_unlock', session['id'], 10, 60)
            self._record_rate(c, 'room_unlock', session['id'])
            allowed = options['access'] == 'public' or room['host_session_id'] == session['id'] or bool(c.execute('SELECT 1 FROM room_access_grants WHERE room_id=:r AND session_id=:s', {'r': room_id, 's': session['id']}).fetchone())
            if not allowed and invite:
                row = c.execute('SELECT * FROM room_invites WHERE room_id=:r AND token_hash=:hash', {'r': room_id, 'hash': token_hash(invite)}).fetchone()
                if row and row['revoked_at'] is None and row['expires_at'] > iso_time() and row['uses'] < row['max_uses']:
                    c.execute('UPDATE room_invites SET uses=uses+1 WHERE id=:id', {'id': row['id']})
                    allowed = True
            if not allowed and options['access'] == 'password' and password and options['password_hash']:
                try:
                    allowed = _PASSWORDS.verify(options['password_hash'], password)
                except (VerificationError, InvalidHashError):
                    pass
            # Commit failed-attempt rate records as well; raise outside transaction.
            if allowed:
                c.execute('INSERT INTO room_access_grants(room_id,session_id,created_at) VALUES(:r,:s,:now) ON CONFLICT(room_id,session_id) DO NOTHING', {'r': room_id, 's': session['id'], 'now': iso_time()})
        if not allowed:
            raise DomainError(403, '密码或邀请无效。')
        return {'unlocked': True}

    def hide_room(self, token, csrf, room_id, hidden):
        if type(hidden) is not bool:
            raise DomainError(422, '隐藏设置不合法。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            _, room, _ = self._host_operation(c, token, csrf, room_id)
            if room['status'] != 'ended':
                raise DomainError(409, '只有已结束对局可以隐藏。')
            c.execute('UPDATE room_options SET hidden=:v WHERE room_id=:r', {'v': int(hidden), 'r': room_id})
        return {'hidden': hidden}

    def recruit_roles(self, token, csrf, room_id, character_ids):
        if not isinstance(character_ids, list) or len(character_ids) > 10 or any(not isinstance(v, str) for v in character_ids) or len(set(character_ids)) != len(character_ids):
            raise DomainError(422, '招募角色列表不合法。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            _, room, options = self._host_operation(c, token, csrf, room_id)
            if room['status'] == 'ended':
                raise DomainError(409, '对局已结束。')
            if not set(character_ids).issubset(self.scenarios[options['cast_scenario_id']]['character_ids']):
                raise DomainError(422, '招募角色不在本局角色范围内。')
            c.execute('UPDATE room_options SET recruitment_json=:roles WHERE room_id=:r', {'roles': json.dumps(character_ids), 'r': room_id})
            c.execute('UPDATE rooms SET revision=revision+1 WHERE id=:r', {'r': room_id})
        return {'recruitment': character_ids}

    def switch_scene(self, token, csrf, room_id, scenario_id, action_id):
        if scenario_id not in self.scenarios or not isinstance(action_id, str) or not 1 <= len(action_id) <= 100:
            raise DomainError(422, '场景或操作 ID 不合法。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            _, room, _ = self._host_operation(c, token, csrf, room_id)
            old = c.execute('SELECT * FROM room_scene_actions WHERE room_id=:r AND action_id=:a', {'r': room_id, 'a': action_id}).fetchone()
            if old:
                if old['scenario_id'] != scenario_id:
                    raise DomainError(409, '同一操作 ID 不能改变场景。')
                return {'scenario_id': scenario_id, 'message_id': old['message_id'], 'changed': False}
            if room['status'] == 'ended':
                raise DomainError(409, '对局已结束。')
            c.execute('UPDATE rooms SET scenario_id=:scene,revision=revision+1,updated_at=:now WHERE id=:r', {'scene': scenario_id, 'now': iso_time(), 'r': room_id})
            message = self._insert_message(c, room_id, 'stage', text='房主将游戏场景切换为' + self.scenarios[scenario_id]['title'] + '。', member_id=None, character_id=None, author='系统', kind='system')
            c.execute('INSERT INTO room_scene_actions(room_id,action_id,scenario_id,message_id,created_at) VALUES(:r,:a,:scene,:m,:now)', {'r': room_id, 'a': action_id, 'scene': scenario_id, 'm': message['id'], 'now': iso_time()})
            return {'scenario_id': scenario_id, 'message_id': message['id'], 'changed': True}

    def public_scenes(self, limit=24, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError(422, '分页大小不合法。')
        ids = sorted(self.scenarios)
        if cursor is not None and cursor not in self.scenarios:
            raise DomainError(422, '分页游标不合法。')
        remaining = [key for key in ids if cursor is None or key > cursor]
        selected = remaining[:limit]
        return {'scenarios': [self.scenarios[key] for key in selected], 'has_more': len(remaining) > limit, 'next_cursor': selected[-1] if len(remaining) > limit else None}

    @staticmethod
    def _cursor_pack(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')

    @staticmethod
    def _cursor_unpack(value):
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError()
            return json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
        except (ValueError, TypeError, UnicodeError):
            raise DomainError(422, '分页游标不合法。') from None

    def public_rooms(self, token=None, limit=24, cursor=None, filter='all', sort='time', preferred=False):
        if type(limit) is not int or not 1 <= limit <= 100 or filter not in {'all', 'active', 'ended', 'mine'} or sort not in {'time', 'rating', 'dialogue'} or type(preferred) is not bool:
            raise DomainError(422, '房间分页参数不合法。')
        # A bounded snapshot freezes rank only. Access is checked again below.
        ranked = sort != 'time' or preferred
        criteria = json.dumps([filter, sort, preferred], separators=(',', ':'))
        decoded = self._cursor_unpack(cursor) if cursor else None
        with self._transaction(write=ranked, session_token=token,
                               admission='room-list-snapshots' if ranked else None) as c:
            session_id = ''
            account = None
            if token:
                session = self._session(c, token)
                session_id = session['id']
                try:
                    account = self._account(c, session)
                except DomainError as exc:
                    if exc.status_code != 401:
                        raise
            if filter == 'mine' and account is None:
                raise DomainError(401, '请先登录。')
            conditions = ["NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=r.id AND b.session_id=:sid)"]
            conditions.append("(COALESCE(o.access,'public')='public' OR r.host_session_id=:sid OR EXISTS(SELECT 1 FROM room_members own WHERE own.room_id=r.id AND own.session_id=:sid) OR EXISTS(SELECT 1 FROM room_access_grants g WHERE g.room_id=r.id AND g.session_id=:sid))")
            if filter != 'mine':
                conditions.append('COALESCE(o.hidden,0)=0')
            if filter == 'active':
                conditions.append("r.status<>'ended'")
            elif filter == 'ended':
                conditions.append("r.status='ended'")
            elif filter == 'mine':
                conditions.append('EXISTS(SELECT 1 FROM room_members own WHERE own.room_id=r.id AND own.session_id=:sid)')
            base = ('SELECT r.id,r.title,r.scenario_id,r.status,r.allow_join,r.created_at,r.stage_seq,'
                    "COALESCE(o.access,'public') AS access,COALESCE(o.recruitment_json,'[]') AS recruitment_json,"
                    '(SELECT COUNT(*) FROM room_members m WHERE m.room_id=r.id AND m.status=\'active\') AS member_count,'
                    '(SELECT AVG(rating) FROM room_ratings rr WHERE rr.room_id=r.id) AS rating,'
                    '(SELECT COUNT(*) FROM room_ratings rr WHERE rr.room_id=r.id) AS rating_count,'
                    "(SELECT COUNT(*) FROM messages msg WHERE msg.room_id=r.id AND msg.channel='stage' AND msg.kind<>'system') AS dialogue_count "
                    'FROM rooms r LEFT JOIN room_options o ON o.room_id=r.id WHERE ' + ' AND '.join(conditions))
            params = {'sid': session_id}
            if ranked:
                now = iso_time()
                c.execute('DELETE FROM room_list_snapshots WHERE expires_at<=:now', {'now': now})
                position = 0
                # Identical public reads share a short-lived ranking. Anonymous
                # visitors cannot exhaust one shared quota merely by refreshing.
                if decoded is None:
                    reusable = c.execute('SELECT id FROM room_list_snapshots WHERE owner_id=:owner '
                                         'AND criteria=:criteria AND expires_at>:now ORDER BY expires_at DESC LIMIT 1',
                                         {'owner': session_id, 'criteria': criteria, 'now': now}).fetchone()
                    if reusable:
                        decoded = {'snapshot': reusable['id'], 'offset': 0}
                if decoded:
                    if not isinstance(decoded, dict) or set(decoded) != {'snapshot', 'offset'} or type(decoded['offset']) is not int or decoded['offset'] < 0:
                        raise DomainError(422, '分页游标不合法。')
                    snap = c.execute('SELECT * FROM room_list_snapshots WHERE id=:id AND owner_id=:owner AND criteria=:criteria AND expires_at>:now', {'id': decoded['snapshot'], 'owner': session_id, 'criteria': criteria, 'now': now}).fetchone()
                    if not snap:
                        raise DomainError(409, '列表已更新，请刷新列表。')
                    ids, snapshot_id, position = json.loads(snap['room_ids']), snap['id'], decoded['offset']
                else:
                    if c.execute('SELECT COUNT(*) FROM room_list_snapshots').fetchone()[0] >= 1000:
                        raise DomainError(429, '列表快照繁忙，请稍后刷新。')
                    if c.execute('SELECT COUNT(*) FROM room_list_snapshots WHERE owner_id=:owner', {'owner': session_id}).fetchone()[0] >= 24:
                        raise DomainError(429, '列表刷新过于频繁，请稍后重试。')
                    rows = c.execute(base + ' ORDER BY r.created_at DESC,r.id ASC LIMIT 10000', params).fetchall()
                    vacancies = self._vacant_recruitment(c, rows) if preferred else {}
                    favorites = set()
                    if preferred and account:
                        favorites = set(json.loads(account['preferences_json']))
                    def rank(row):
                        match = len(favorites.intersection(vacancies.get(row['id'], [])))
                        score = row['rating'] or 0 if sort == 'rating' else row['dialogue_count'] if sort == 'dialogue' else 0
                        return (match, score, row['created_at'])
                    rows = sorted(rows, key=rank, reverse=True)
                    ids, snapshot_id = [row['id'] for row in rows], new_id()
                    c.execute('INSERT INTO room_list_snapshots(id,owner_id,criteria,room_ids,expires_at) VALUES(:id,:owner,:criteria,:ids,:expires)', {'id': snapshot_id, 'owner': session_id, 'criteria': criteria, 'ids': json.dumps(ids), 'expires': iso_time(utc_now() + timedelta(minutes=5))})
                selected = []
                # Re-evaluate current authorization, hiding and filters on every page.
                while position < len(ids) and len(selected) < limit + 1:
                    batch = ids[position:position + 100]
                    bind = {'rank' + str(i): value for i, value in enumerate(batch)}
                    candidates = c.execute(base + ' AND r.id IN (' + ','.join(':' + key for key in bind) + ')', {**params, **bind}).fetchall()
                    by_id = {row['id']: row for row in candidates}
                    for room_id in batch:
                        position += 1
                        if room_id in by_id:
                            selected.append((dict(by_id[room_id]), position))
                        if len(selected) >= limit + 1:
                            break
                more = len(selected) > limit
                rows = [item[0] for item in selected[:limit]]
                next_cursor = self._cursor_pack({'snapshot': snapshot_id, 'offset': selected[limit - 1][1]}) if more else None
            else:
                if decoded:
                    if not isinstance(decoded, dict) or set(decoded) != {'criteria', 'created_at', 'id'} or decoded['criteria'] != criteria or not isinstance(decoded['id'], str) or not isinstance(decoded['created_at'], str):
                        raise DomainError(422, '分页游标不合法。')
                    base += ' AND (r.created_at<:created OR (r.created_at=:created AND r.id>:last))'
                    params.update(created=decoded['created_at'], last=decoded['id'])
                rows = c.execute(base + ' ORDER BY r.created_at DESC,r.id ASC LIMIT :size', {**params, 'size': limit + 1}).fetchall()
                more, rows = len(rows) > limit, rows[:limit]
                next_cursor = self._cursor_pack({'criteria': criteria, 'created_at': rows[-1]['created_at'], 'id': rows[-1]['id']}) if more else None
            rooms = []
            vacancies = self._vacant_recruitment(c, rows)
            for row in rows:
                value = {key: row[key] for key in ('id', 'title', 'scenario_id', 'status', 'created_at', 'access', 'member_count', 'rating', 'rating_count', 'dialogue_count')}
                value['rating'] = float(value['rating']) if value['rating'] is not None else None
                value['recruitment'] = vacancies[row['id']] if row['status'] != 'ended' and (row['status'] == 'preparing' or row['allow_join']) else []
                rooms.append(value)
            return {'rooms': rooms, 'next_cursor': next_cursor, 'has_more': more}

    def heartbeat(self, token, csrf, room_id, connection_id='http'):
        if not isinstance(connection_id, str) or not 1 <= len(connection_id) <= 128:
            raise DomainError(422, '连接标识不合法。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as c:
            session = self._session(c, token, csrf, require_csrf=True)
            self._account(c, session)
            room = self._room(c, room_id)
            self._not_banned(c, room_id, session['id'])
            member = self._member(c, room_id, session['id'])
            now = iso_time()
            c.execute('DELETE FROM room_presence_leases WHERE expires_at<=:now', {'now': now})
            # Auth token hash prevents devices from overwriting one another's lease.
            scope = token_hash(str(token) + ':' + connection_id)
            exists = c.execute('SELECT 1 FROM room_presence_leases WHERE room_id=:r AND session_id=:s AND connection_id=:id', {'r': room_id, 's': session['id'], 'id': scope}).fetchone()
            if not exists and c.execute('SELECT COUNT(*) FROM room_presence_leases WHERE room_id=:r AND session_id=:s', {'r': room_id, 's': session['id']}).fetchone()[0] >= 12:
                raise DomainError(429, '在线连接数量已达上限。')
            expires = iso_time(utc_now() + timedelta(seconds=90))
            auth_id = session.get('auth_session_id')
            c.execute('INSERT INTO room_presence_leases(room_id,session_id,connection_id,auth_session_id,expires_at) VALUES(:r,:s,:id,:auth,:expires) ON CONFLICT(room_id,session_id,connection_id) DO UPDATE SET expires_at=:expires', {'r': room_id, 's': session['id'], 'id': scope, 'auth': auth_id, 'expires': expires})
            if auth_id:
                c.execute('UPDATE auth_sessions SET last_seen_at=:now WHERE id=:id', {'now': now, 'id': auth_id})
            if room['status'] != 'ended' and (room['host_session_id'] == session['id'] or member['character_id']):
                self._options(c, room, create=True)
                c.execute('UPDATE room_options SET offline_since=NULL WHERE room_id=:r', {'r': room_id})
            return {'expires_at': expires}

    def recover_presence(self):
        """Called on process start: a crash is not evidence of player absence."""
        with self._transaction(write=True) as c:
            c.execute('DELETE FROM room_presence_leases')
            c.execute('UPDATE room_options SET offline_since=NULL')
        self._presence_last_sweep = utc_now()

    def sweep_offline(self, now=None):
        now = now or utc_now()
        previous = getattr(self, '_presence_last_sweep', None)
        self._presence_last_sweep = now
        if previous is None or (now - previous).total_seconds() > 90 or now < previous:
            with self._transaction(write=True) as c:
                c.execute('UPDATE room_options SET offline_since=NULL')
            # Resume observation after a scheduler outage; do not end on this sweep.
            return []
        stamp, changed = iso_time(now), []
        with self._transaction() as c:
            # Rotate bounded batches, so active older rooms cannot starve later
            # rooms. 1,000 live rooms are covered in five 20-second sweeps.
            last_id = getattr(self, '_presence_room_cursor', '')
            candidates = [row['id'] for row in c.execute("SELECT id FROM rooms WHERE status<>'ended' AND id>:last ORDER BY id LIMIT 200", {'last': last_id}).fetchall()]
            if not candidates and last_id:
                candidates = [row['id'] for row in c.execute("SELECT id FROM rooms WHERE status<>'ended' ORDER BY id LIMIT 200").fetchall()]
            self._presence_room_cursor = candidates[-1] if len(candidates) == 200 else ''
        for room_id in candidates:
            with self._transaction(write=True, room_id=room_id) as c:
                room = self._room(c, room_id)
                if room['status'] == 'ended':
                    continue
                options = self._options(c, room, create=True)
                online = c.execute("SELECT 1 FROM room_presence_leases p JOIN room_members m ON m.room_id=p.room_id AND m.session_id=p.session_id JOIN sessions s ON s.id=p.session_id WHERE p.room_id=:r AND p.expires_at>:now AND s.revoked_at IS NULL AND m.status='active' AND (m.session_id=:host OR m.character_id IS NOT NULL) AND (p.auth_session_id IS NULL OR EXISTS(SELECT 1 FROM auth_sessions a JOIN users u ON u.id=a.user_id WHERE a.id=p.auth_session_id AND a.revoked_at IS NULL AND a.expires_at>:now AND u.banned=0)) AND NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=m.room_id AND b.session_id=m.session_id) LIMIT 1", {'r': room_id, 'now': stamp, 'host': room['host_session_id']}).fetchone()
                if online:
                    if options['offline_since'] is not None:
                        c.execute('UPDATE room_options SET offline_since=NULL WHERE room_id=:r', {'r': room_id})
                elif options['offline_since'] is None:
                    c.execute('UPDATE room_options SET offline_since=:now WHERE room_id=:r', {'now': stamp, 'r': room_id})
                elif (now - parse_time(options['offline_since'])).total_seconds() >= 600:
                    c.execute("UPDATE rooms SET status='ended',allow_join=0,revision=revision+1,updated_at=:now WHERE id=:r", {'r': room_id, 'now': stamp})
                    c.execute("UPDATE role_requests SET status='cancelled',decided_at=:now WHERE room_id=:r AND status='pending'", {'r': room_id, 'now': stamp})
                    self._insert_message(c, room_id, 'stage', text='房主及全部演员离线已满 10 分钟，对局自动结束。', member_id=None, character_id=None, author='系统', kind='system')
                    changed.append(room_id)
        return changed
