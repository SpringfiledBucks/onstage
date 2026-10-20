"""Account-only interactions and revocable, explicitly consented public excerpts."""
from __future__ import annotations

import json
import secrets
from .security import DomainError, iso_time, new_id


# Internal anti-abuse policy: never serialized into ordinary user responses.
# A pair may contribute in at most this many distinct games, including withdrawals.
MEMBER_FEEDBACK_PAIR_GAME_LIMIT = 5


class CommunityMixin:
    def _community_identity(self, conn, token, csrf=None, *, write=False, admin=False):
        session = self._session(conn, token, csrf, require_csrf=write)
        return session, self._account(conn, session, admin=admin)

    @staticmethod
    def _historical_member(conn, room_id, principal):
        row = conn.execute('SELECT * FROM room_members WHERE room_id=:room AND session_id=:principal',
                           {'room': room_id, 'principal': principal}).fetchone()
        if row is None:
            raise DomainError(403, '仅本局参与者可执行此操作。')
        return row

    def _community_limit(self, conn, account, action, limit=30):
        # Serialize one account's limits across its devices/tokens and rooms.
        if hasattr(self, '_lock') and hasattr(conn, 'connection'):
            self._lock(conn.connection, 'community-rate:' + account['id'])
        self._check_rate(conn, action, account['id'], limit, 3600)
        self._record_rate(conn, action, account['id'])

    @staticmethod
    def _community_audit(conn, account, action, target, note=''):
        conn.execute('INSERT INTO account_audit(id,actor_user_id,action,target_id,note,created_at) '
                     'VALUES(:id,:actor,:action,:target,:note,:now)',
                     {'id': new_id(), 'actor': account['id'], 'action': action,
                      'target': target, 'note': note, 'now': iso_time()})

    def rate_room(self, token, csrf, room_id, rating):
        if type(rating) is not int or not 1 <= rating <= 5:
            raise DomainError(422, '评分须为 1 至 5 的整数。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            room = self._room(conn, room_id)
            self._historical_member(conn, room_id, account['principal_session_id'])
            if room['status'] != 'ended':
                raise DomainError(409, '对局结束后才能评分。')
            self._community_limit(conn, account, 'rating')
            now = iso_time()
            conn.execute('INSERT INTO room_ratings(room_id,user_id,rating,created_at,updated_at) '
                         'VALUES(:room,:user,:rating,:now,:now) ON CONFLICT(room_id,user_id) '
                         'DO UPDATE SET rating=excluded.rating,updated_at=excluded.updated_at',
                         {'room': room_id, 'user': account['id'], 'rating': rating, 'now': now})
            row = conn.execute('SELECT AVG(rating) AS average,COUNT(*) AS count FROM room_ratings WHERE room_id=:room', {'room': room_id}).fetchone()
            return {'rating': rating, 'average': float(row['average']), 'count': row['count']}

    def member_vote(self, token, csrf, room_id, target_member_id, value):
        if type(value) is not int or value not in {-1, 0, 1}:
            raise DomainError(422, '评价值须为 -1、0 或 1。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            own = self._historical_member(conn, room_id, account['principal_session_id'])
            target = conn.execute('SELECT * FROM room_members WHERE room_id=:room AND id=:target', {'room': room_id, 'target': target_member_id}).fetchone()
            if target is None:
                raise DomainError(404, '本局成员不存在。')
            if target['id'] == own['id']:
                raise DomainError(403, '不能评价自己。')
            # Stable account principal, rather than per-room member ID, ties the
            # same recipient across games without exposing their login username.
            if hasattr(self, '_lock') and hasattr(conn, 'connection'):
                self._lock(conn.connection, 'community-rate:' + account['id'])
            params = {'room': room_id, 'user': account['id'], 'target': target_member_id,
                      'principal': target['session_id']}
            current = conn.execute('SELECT value FROM member_feedback WHERE room_id=:room AND voter_user_id=:user AND target_member_id=:target', params).fetchone()
            if (current['value'] if current else 0) == value:
                return {'value': value}
            self._community_limit(conn, account, 'member_vote', 120)
            if value == 0:
                conn.execute('DELETE FROM member_feedback WHERE room_id=:room AND voter_user_id=:user AND target_member_id=:target', params)
            else:
                used = conn.execute('SELECT 1 FROM member_feedback_ledger WHERE room_id=:room AND voter_user_id=:user AND target_principal_session_id=:principal', params).fetchone()
                if used is None:
                    count = conn.execute('SELECT COUNT(*) AS count FROM member_feedback_ledger WHERE voter_user_id=:user AND target_principal_session_id=:principal', params).fetchone()['count']
                    if count >= MEMBER_FEEDBACK_PAIR_GAME_LIMIT:
                        raise DomainError(429, '暂时无法提交评价，请稍后再试。')
                    conn.execute('INSERT INTO member_feedback_ledger(room_id,voter_user_id,target_principal_session_id,created_at) VALUES(:room,:user,:principal,:now)', {**params, 'now': iso_time()})
                conn.execute('INSERT INTO member_feedback(room_id,voter_user_id,target_member_id,value,created_at,updated_at) VALUES(:room,:user,:target,:value,:now,:now) '
                             'ON CONFLICT(room_id,voter_user_id,target_member_id) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                             {**params, 'value': value, 'now': iso_time()})
            return {'value': value}

    def report_member(self, token, csrf, room_id, target_member_id, reason, details=''):
        if reason not in {'harassment', 'spam', 'unsafe', 'other'} or not isinstance(details, str) or len(details) > 1000:
            raise DomainError(422, '举报原因或说明无效。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            own = self._historical_member(conn, room_id, account['principal_session_id'])
            target = conn.execute('SELECT * FROM room_members WHERE room_id=:room AND id=:target', {'room': room_id, 'target': target_member_id}).fetchone()
            if target is None:
                raise DomainError(404, '本局成员不存在。')
            if own['id'] == target['id']:
                raise DomainError(403, '不能举报自己。')
            prior = conn.execute("SELECT id FROM member_reports WHERE room_id=:room AND reporter_user_id=:user AND target_member_id=:target AND status='open'", {'room': room_id, 'user': account['id'], 'target': target_member_id}).fetchone()
            if prior:
                return {'id': prior['id'], 'status': 'open'}
            self._community_limit(conn, account, 'member_report', self.settings.report_limit_per_hour)
            report_id = new_id()
            snapshot = {key: target[key] for key in ('id', 'display_alias', 'character_id', 'status')}
            conn.execute('INSERT INTO member_reports(id,room_id,reporter_user_id,target_member_id,reason,details,snapshot_json,created_at) VALUES(:id,:room,:user,:target,:reason,:details,:snapshot,:now)',
                         {'id': report_id, 'room': room_id, 'user': account['id'], 'target': target_member_id, 'reason': reason, 'details': details, 'snapshot': json.dumps(snapshot, ensure_ascii=False), 'now': iso_time()})
            return {'id': report_id, 'status': 'open'}

    def share_permission(self, token, csrf, room_id, allowed):
        if type(allowed) is not bool:
            raise DomainError(422, '授权状态须为布尔值。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            member = self._historical_member(conn, room_id, account['principal_session_id'])
            self._community_limit(conn, account, 'share_permission', 60)
            conn.execute('INSERT INTO room_share_permissions(room_id,member_id,allowed,updated_at) VALUES(:room,:member,:allowed,:now) ON CONFLICT(room_id,member_id) DO UPDATE SET allowed=excluded.allowed,updated_at=excluded.updated_at',
                         {'room': room_id, 'member': member['id'], 'allowed': int(allowed), 'now': iso_time()})
            if not allowed:
                # Revocation is permanent for existing excerpts even if consent is later restored.
                conn.execute('UPDATE conversation_shares SET revoked_at=:now WHERE revoked_at IS NULL AND id IN (SELECT share_id FROM share_messages WHERE member_id=:member)', {'now': iso_time(), 'member': member['id']})
            return {'allowed': allowed}

    @staticmethod
    def _share_room_allowed(conn, room_id):
        options = conn.execute('SELECT access,share_enabled FROM room_options WHERE room_id=:room', {'room': room_id}).fetchone()
        if options and not options['share_enabled']:
            raise DomainError(403, '房间未允许公开分享。')

    def create_share(self, token, csrf, room_id, message_ids):
        if not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 100 or any(not isinstance(item, str) or not item or len(item) > 128 for item in message_ids) or len(set(message_ids)) != len(message_ids):
            raise DomainError(422, '请选择 1 至 100 条不重复的舞台消息。')
        with self._transaction(write=True, room_id=room_id, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            self._room(conn, room_id)
            self._historical_member(conn, room_id, account['principal_session_id'])
            self._not_banned(conn, room_id, account['principal_session_id'])
            self._share_room_allowed(conn, room_id)
            self._community_limit(conn, account, 'create_share', 20)
            params = {'room': room_id, **{f'm{i}': value for i, value in enumerate(message_ids)}}
            placeholders = ','.join(f':m{i}' for i in range(len(message_ids)))
            rows = conn.execute(f'SELECT m.*,p.allowed FROM messages m LEFT JOIN room_share_permissions p ON p.room_id=m.room_id AND p.member_id=m.member_id WHERE m.room_id=:room AND m.id IN ({placeholders}) ORDER BY m.seq', params).fetchall()
            if len(rows) != len(message_ids) or any(row['channel'] != 'stage' or row['kind'] != 'speech' or not row['member_id'] or not row['allowed'] for row in rows):
                raise DomainError(403, '仅可分享作者已授权的本局舞台消息。')
            share_id, slug, now = new_id(), secrets.token_urlsafe(32), iso_time()
            conn.execute('INSERT INTO conversation_shares(id,slug,room_id,creator_user_id,created_at) VALUES(:id,:slug,:room,:user,:now)', {'id': share_id, 'slug': slug, 'room': room_id, 'user': account['id'], 'now': now})
            for row in rows:
                rendered = self._message_json(row, conn)
                snapshot = {key: rendered[key] for key in ('author', 'text', 'created_at', 'content_type', 'sticker', 'character_id')}
                # Frozen text/role/sticker only: no IDs usable to correlate accounts or client sends.
                conn.execute('INSERT INTO share_messages(share_id,message_id,member_id,seq,snapshot_json) VALUES(:share,:message,:member,:seq,:snapshot)',
                             {'share': share_id, 'message': row['id'], 'member': row['member_id'], 'seq': row['seq'], 'snapshot': json.dumps(snapshot, ensure_ascii=False)})
            return {'id': share_id, 'slug': slug, 'path': '/s/' + slug, 'created_at': now}

    def share_detail(self, slug):
        if not isinstance(slug, str) or len(slug) > 100:
            raise DomainError(404, '分享不存在或已撤销。')
        with self._transaction() as conn:
            share = conn.execute('SELECT * FROM conversation_shares WHERE slug=:slug', {'slug': slug}).fetchone()
            if share is None or share['revoked_at'] is not None:
                raise DomainError(404, '分享不存在或已撤销。')
            self._share_room_allowed(conn, share['room_id'])
            rows = conn.execute('SELECT m.snapshot_json,p.allowed FROM share_messages m LEFT JOIN room_share_permissions p ON p.member_id=m.member_id AND p.room_id=:room WHERE m.share_id=:share ORDER BY m.seq', {'room': share['room_id'], 'share': share['id']}).fetchall()
            if not rows or any(not row['allowed'] for row in rows):
                raise DomainError(404, '分享不存在或已撤销。')
            return {'created_at': share['created_at'], 'messages': [json.loads(row['snapshot_json']) for row in rows]}

    def revoke_share(self, token, csrf, slug):
        with self._transaction(write=True, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            share = conn.execute('SELECT * FROM conversation_shares WHERE slug=:slug', {'slug': slug}).fetchone()
            if share is None:
                raise DomainError(404, '分享不存在。')
            author = conn.execute('SELECT 1 FROM share_messages sm JOIN room_members m ON m.id=sm.member_id WHERE sm.share_id=:share AND m.session_id=:principal LIMIT 1', {'share': share['id'], 'principal': account['principal_session_id']}).fetchone()
            if not account['is_admin'] and share['creator_user_id'] != account['id'] and not author:
                raise DomainError(403, '仅作者、创建者或管理员可撤销分享。')
            conn.execute('UPDATE conversation_shares SET revoked_at=COALESCE(revoked_at,:now) WHERE id=:id', {'now': iso_time(), 'id': share['id']})
            if account['is_admin']:
                self._community_audit(conn, account, 'revoke_share', share['id'])
            return {'revoked': True}

    def developer_feedback(self, token, csrf, text):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 3000:
            raise DomainError(422, '反馈须为 1 至 3000 字。')
        with self._transaction(write=True, session_token=token) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True)
            self._community_limit(conn, account, 'developer_feedback', 10)
            feedback_id = new_id()
            conn.execute('INSERT INTO developer_feedback(id,user_id,text,created_at) VALUES(:id,:user,:text,:now)', {'id': feedback_id, 'user': account['id'], 'text': text.strip(), 'now': iso_time()})
            return {'id': feedback_id, 'status': 'open'}

    @staticmethod
    def _community_page(limit, cursor):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError(422, '分页大小须为 1 至 100。')
        if cursor is None or cursor == '':
            return 0
        if not isinstance(cursor, str) or not cursor.isdigit() or len(cursor) > 9:
            raise DomainError(422, '分页游标无效。')
        return int(cursor)

    @staticmethod
    def _community_result(rows, limit, offset):
        return {'items': [dict(row) for row in rows[:limit]],
                'next_cursor': str(offset + limit) if len(rows) > limit else None}

    def admin_rooms(self, token, *, user_id=None, status=None, limit=50, cursor=None):
        offset = self._community_page(limit, cursor)
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
            clauses, params = [], {'limit': limit + 1, 'offset': offset}
            if user_id:
                clauses.append('EXISTS(SELECT 1 FROM room_members m JOIN users u ON u.principal_session_id=m.session_id WHERE m.room_id=r.id AND u.id=:user)')
                params['user'] = user_id
            if status:
                if status not in {'preparing', 'live', 'paused', 'ended'}:
                    raise DomainError(422, '房间状态无效。')
                clauses.append('r.status=:status')
                params['status'] = status
            where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
            rows = conn.execute("SELECT r.id,r.title,r.scenario_id,r.status,r.created_at,r.updated_at,COALESCE(o.access,'public') AS access,COALESCE(o.hidden,0) AS hidden,r.stage_seq AS dialogue_count "
                                'FROM rooms r LEFT JOIN room_options o ON o.room_id=r.id' + where + ' ORDER BY r.created_at DESC,r.id DESC LIMIT :limit OFFSET :offset', params).fetchall()
            result = self._community_result(rows, limit, offset)
            result['total'] = conn.execute('SELECT COUNT(*) FROM rooms r' + where, params).fetchone()[0]
            return result

    def admin_shares(self, token, *, limit=50, cursor=None):
        offset = self._community_page(limit, cursor)
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
            rows = conn.execute('SELECT id,slug,room_id,creator_user_id,created_at,revoked_at FROM conversation_shares ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset', {'limit': limit + 1, 'offset': offset}).fetchall()
            return self._community_result(rows, limit, offset)

    def admin_reports(self, token, *, status=None, limit=50, cursor=None):
        offset = self._community_page(limit, cursor)
        if status is not None and status not in {'open', 'resolved', 'dismissed'}:
            raise DomainError(422, '举报状态无效。')
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
            query = ("SELECT id,room_id,'user' AS target_type,target_member_id AS target_key,reason,details,snapshot_json,status,created_at,resolved_at,resolution_note FROM member_reports "
                     "UNION ALL SELECT id,room_id,target_type,target_key,reason,details,snapshot_json,status,created_at,resolved_at,resolution_note FROM reports")
            where = ' WHERE status=:status' if status else ''
            rows = conn.execute('SELECT * FROM (' + query + ') all_reports' + where + ' ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset', {'status': status, 'limit': limit + 1, 'offset': offset}).fetchall()
            result = self._community_result(rows, limit, offset)
            for item in result['items']:
                item['snapshot'] = json.loads(item.pop('snapshot_json'))
            return result

    def admin_feedback(self, token, *, status=None, limit=50, cursor=None):
        offset = self._community_page(limit, cursor)
        if status is not None and status not in {'open', 'resolved', 'dismissed'}:
            raise DomainError(422, '反馈状态无效。')
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
            where = ' WHERE status=:status' if status else ''
            rows = conn.execute('SELECT * FROM developer_feedback' + where + ' ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset', {'status': status, 'limit': limit + 1, 'offset': offset}).fetchall()
            return self._community_result(rows, limit, offset)

    def admin_member_feedback(self, token, *, room_id=None, limit=50, cursor=None):
        offset = self._community_page(limit, cursor)
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
            where = ' WHERE room_id=:room' if room_id else ''
            rows = conn.execute('SELECT * FROM member_feedback' + where + ' ORDER BY updated_at DESC,room_id,voter_user_id,target_member_id LIMIT :limit OFFSET :offset', {'room': room_id, 'limit': limit + 1, 'offset': offset}).fetchall()
            return self._community_result(rows, limit, offset)

    def admin_audit(self, token, *, limit=50, cursor=None):
        offset = self._community_page(limit, cursor)
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
            rows = conn.execute('SELECT a.*,u.username AS actor_username FROM account_audit a '
                                'LEFT JOIN users u ON u.id=a.actor_user_id ORDER BY a.created_at DESC,a.id DESC '
                                'LIMIT :limit OFFSET :offset', {'limit': limit + 1, 'offset': offset}).fetchall()
            return self._community_result(rows, limit, offset)

    def admin_resolve_report(self, token, csrf, report_id, status, note=''):
        return self._community_resolve(token, csrf, report_id, status, note, 'report')

    def admin_resolve_feedback(self, token, csrf, feedback_id, status, note=''):
        return self._community_resolve(token, csrf, feedback_id, status, note, 'feedback')

    def _community_resolve(self, token, csrf, target_id, status, note, kind):
        if status not in {'resolved', 'dismissed'} or not isinstance(note, str) or len(note) > 1000:
            raise DomainError(422, '处理状态或说明无效。')
        with self._transaction(write=True, session_token=token, admission='community_moderation',
                               report_id=target_id if kind == 'report' else None) as conn:
            _, account = self._community_identity(conn, token, csrf, write=True, admin=True)
            table = 'developer_feedback' if kind == 'feedback' else 'member_reports'
            row = conn.execute(f'SELECT id,status FROM {table} WHERE id=:id', {'id': target_id}).fetchone()
            if row is None and kind == 'report':
                table = 'reports'
                row = conn.execute('SELECT id,status FROM reports WHERE id=:id', {'id': target_id}).fetchone()
            if row is None:
                raise DomainError(404, '记录不存在。')
            if row['status'] != 'open':
                raise DomainError(409, '记录已经处理。')
            extra = ",resolved_by='operator',operator_label=:actor" if table == 'reports' else ''
            conn.execute(f'UPDATE {table} SET status=:status,resolved_at=:now,resolution_note=:note' + extra + ' WHERE id=:id',
                         {'id': target_id, 'status': status, 'now': iso_time(), 'note': note, 'actor': account['id']})
            self._community_audit(conn, account, 'resolve_' + kind, target_id, note)
            return {'id': target_id, 'status': status}

    def admin_revoke_share(self, token, csrf, slug):
        with self._transaction() as conn:
            self._community_identity(conn, token, admin=True)
        return self.revoke_share(token, csrf, slug)
