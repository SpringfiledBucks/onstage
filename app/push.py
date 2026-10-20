"""Opt-in Web Push: private endpoints, transactional coalescing and one bounded sender.

Never sends message text, character names, usernames or room titles to providers.
Each subscription owns at most one pending hint; HTTP sync remains authoritative.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from py_vapid import Vapid
from pywebpush import webpush, WebPushException
import requests

from .security import DomainError, iso_time, new_id, utc_now

logger = logging.getLogger('uvicorn.error.onstage')
DEFAULT_PUSH_HOSTS = frozenset({'fcm.googleapis.com', 'updates.push.services.mozilla.com',
                                'web.push.apple.com', 'wns.windows.com'})


def decode_key(value, length):
    if not isinstance(value, str) or len(value) > 120:
        raise DomainError(422, '通知订阅无效。')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        if len(raw) != length:
            raise ValueError()
        if length == 65:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        return value
    except (ValueError, TypeError):
        raise DomainError(422, '通知订阅无效。') from None


def validate_endpoint(endpoint):
    try:
        url = urlsplit(endpoint)
        # No IP literals, userinfo, custom ports, fragments, redirects or arbitrary
        # client-selected servers. Extra *exact* provider hosts are operator config.
        hosts = DEFAULT_PUSH_HOSTS | set(filter(None, os.environ.get('ONSTAGE_PUSH_PROVIDER_HOSTS', '').split(',')))
        if (not isinstance(endpoint, str) or len(endpoint) > 2048 or url.scheme != 'https'
                or url.hostname not in hosts or url.port not in (None, 443)
                or url.username or url.password or url.fragment or not url.path.startswith('/')
                or any(c.isspace() or ord(c) < 32 for c in endpoint)):
            raise ValueError()
        return endpoint
    except (ValueError, TypeError):
        raise DomainError(422, '通知服务不受支持。') from None


@dataclass
class PushConfig:
    public_key: str = ''
    vapid: object = field(default=None, repr=False)
    subject: str = ''

    @classmethod
    def from_env(cls):
        path = os.environ.get('ONSTAGE_VAPID_PRIVATE_KEY_FILE', '')
        subject = os.environ.get('ONSTAGE_VAPID_SUBJECT', '')
        if not path and not subject:
            return cls()
        try:
            # Do not use Vapid.from_file: it silently generates a missing key.
            parsed = urlsplit(subject)
            if parsed.scheme not in {'mailto', 'https'} or not parsed.path and not parsed.hostname or len(subject) > 200:
                raise ValueError()
            raw = Path(path).read_bytes()
            if len(raw) > 4096:
                raise ValueError()
            key = Vapid.from_pem(raw)
            public = key.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            return cls(base64.urlsafe_b64encode(public).rstrip(b'=').decode(), key, subject)
        except Exception:
            raise RuntimeError('Invalid Web Push VAPID configuration; provide a readable mounted P-256 PEM Secret and contact subject.') from None

    @property
    def enabled(self):
        return bool(self.public_key and self.vapid)


class NotificationMixin:
    def notification_rooms(self, token):
        with self._transaction() as conn:
            session = self._session(conn, token)
            user = self._account(conn, session)
            rows = conn.execute("SELECT f.room_id FROM room_notification_follows f JOIN room_members m ON m.room_id=f.room_id "
                                "WHERE f.user_id=:user AND m.session_id=:principal AND m.status='active' "
                                "AND NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=f.room_id AND b.session_id=:principal)",
                                {'user': user['id'], 'principal': session['id']}).fetchall()
            return {'room_ids': [row[0] for row in rows]}

    def follow_notifications(self, token, csrf, room_id, enabled):
        if type(enabled) is not bool:
            raise DomainError(422, '通知设置无效。')
        with self._transaction(write=True, session_token=token, room_id=room_id) as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            user = self._account(conn, session)
            if enabled:
                self._room(conn, room_id)
                self._member(conn, room_id, session['id'])
                count = conn.execute('SELECT COUNT(*) FROM room_notification_follows WHERE user_id=:user', {'user': user['id']}).fetchone()[0]
                current = conn.execute('SELECT 1 FROM room_notification_follows WHERE room_id=:room AND user_id=:user', {'room': room_id, 'user': user['id']}).fetchone()
                if not current and count >= 20:
                    raise DomainError(429, '关注的房间较多，请先关闭部分通知。')
                conn.execute('INSERT INTO room_notification_follows(user_id,room_id,created_at) VALUES(:user,:room,:now) ON CONFLICT(user_id,room_id) DO NOTHING', {'user': user['id'], 'room': room_id, 'now': iso_time()})
            else:
                conn.execute('DELETE FROM room_notification_follows WHERE user_id=:user AND room_id=:room', {'user': user['id'], 'room': room_id})
                conn.execute('DELETE FROM push_outbox WHERE room_id=:room AND subscription_id IN(SELECT id FROM push_subscriptions WHERE user_id=:user)', {'user': user['id'], 'room': room_id})
            return {'enabled': enabled}

    def subscribe_notifications(self, token, csrf, endpoint, p256dh, auth, frontend_origin):
        endpoint = validate_endpoint(endpoint)
        decode_key(p256dh, 65); decode_key(auth, 16)
        digest = hashlib.sha256(endpoint.encode()).hexdigest()
        with self._transaction(write=True, session_token=token, admission='push-subscriptions') as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            user = self._account(conn, session)
            self._purge_push(conn)
            existing = conn.execute('SELECT * FROM push_subscriptions WHERE endpoint_hash=:hash', {'hash': digest}).fetchone()
            if existing and (existing['user_id'] != user['id'] or existing['auth_session_id'] != session['auth_session_id']):
                raise DomainError(409, '请先关闭此浏览器的旧通知订阅，再重新开启。')
            if not existing:
                if conn.execute('SELECT COUNT(*) FROM push_subscriptions WHERE user_id=:user', {'user': user['id']}).fetchone()[0] >= 5 or conn.execute('SELECT COUNT(*) FROM push_subscriptions').fetchone()[0] >= 5000:
                    raise DomainError(429, '通知订阅暂满，请稍后重试。')
            identity = existing['id'] if existing else new_id()
            conn.execute('INSERT INTO push_subscriptions(id,user_id,auth_session_id,endpoint_hash,endpoint,p256dh,auth_secret,frontend_origin,created_at,updated_at) '
                         'VALUES(:id,:user,:session,:hash,:endpoint,:key,:auth,:origin,:now,:now) ON CONFLICT(endpoint_hash) '
                         'DO UPDATE SET p256dh=excluded.p256dh,auth_secret=excluded.auth_secret,frontend_origin=excluded.frontend_origin,updated_at=excluded.updated_at',
                         {'id': identity, 'user': user['id'], 'session': session['auth_session_id'], 'hash': digest, 'endpoint': endpoint, 'key': p256dh, 'auth': auth, 'origin': frontend_origin, 'now': iso_time()})
            return {'id': identity}

    def unsubscribe_notifications(self, token, csrf, subscription_id):
        with self._transaction(write=True, session_token=token) as conn:
            session = self._session(conn, token, csrf, require_csrf=True)
            self._account(conn, session)
            conn.execute('DELETE FROM push_subscriptions WHERE id=:id AND auth_session_id=:session', {'id': subscription_id, 'session': session['auth_session_id']})
            return {'ok': True}

    @staticmethod
    def _purge_push(conn):
        conn.execute('DELETE FROM push_subscriptions WHERE auth_session_id IN(SELECT a.id FROM auth_sessions a JOIN users u ON u.id=a.user_id WHERE a.revoked_at IS NOT NULL OR a.expires_at<=:now OR u.banned=1)', {'now': iso_time()})
        conn.execute('DELETE FROM push_outbox WHERE queued_at<:cutoff', {'cutoff': iso_time(utc_now() - timedelta(hours=1))})

    def _insert_message(self, conn, room_id, channel, **kwargs):
        message = super()._insert_message(conn, room_id, channel, **kwargs)
        if kwargs.get('kind') == 'speech':
            # Same DB transaction as the message; retrying a committed ID never
            # reaches this insertion path. Coalescing cannot grow per message.
            conn.execute("INSERT INTO push_outbox(subscription_id,room_id,message_id,channel,queued_at,next_attempt_at) "
                         "SELECT s.id,:room,:message,:channel,:now,:now FROM push_subscriptions s "
                         "JOIN auth_sessions a ON a.id=s.auth_session_id JOIN users u ON u.id=s.user_id "
                         "JOIN room_notification_follows f ON f.user_id=u.id AND f.room_id=:room "
                         "JOIN room_members m ON m.room_id=:room AND m.session_id=u.principal_session_id JOIN rooms r ON r.id=:room "
                         "WHERE a.revoked_at IS NULL AND a.expires_at>:now AND u.banned=0 AND m.status='active' AND m.id<>:sender "
                         "AND NOT EXISTS(SELECT 1 FROM room_bans b WHERE b.room_id=:room AND b.session_id=u.principal_session_id) "
                         "AND (:channel='stage' OR r.host_session_id=u.principal_session_id OR m.character_id IS NOT NULL) "
                         "ON CONFLICT(subscription_id) DO UPDATE SET room_id=excluded.room_id,message_id=excluded.message_id,channel=excluded.channel,queued_at=excluded.queued_at,next_attempt_at=excluded.next_attempt_at,attempts=0",
                         {'room': room_id, 'message': message['id'], 'channel': channel, 'now': iso_time(), 'sender': kwargs.get('member_id')})
        return message

    def claim_push(self):
        with self._transaction(write=True, admission='push-delivery') as conn:
            self._purge_push(conn)
            row = conn.execute('SELECT o.*,s.endpoint,s.p256dh,s.auth_secret,s.frontend_origin,s.user_id,u.principal_session_id '
                               'FROM push_outbox o JOIN push_subscriptions s ON s.id=o.subscription_id JOIN users u ON u.id=s.user_id '
                               'WHERE o.next_attempt_at<=:now AND (o.lease_until IS NULL OR o.lease_until<=:now) ORDER BY o.next_attempt_at,o.subscription_id LIMIT 1', {'now': iso_time()}).fetchone()
            if row is None:
                return None
            room, principal = row['room_id'], row['principal_session_id']
            member = conn.execute("SELECT m.*,r.host_session_id FROM room_members m JOIN rooms r ON r.id=m.room_id WHERE m.room_id=:room AND m.session_id=:principal AND m.status='active'", {'room': room, 'principal': principal}).fetchone()
            ban = conn.execute('SELECT 1 FROM room_bans WHERE room_id=:room AND session_id=:principal', {'room': room, 'principal': principal}).fetchone()
            follow = conn.execute('SELECT 1 FROM room_notification_follows WHERE user_id=:user AND room_id=:room', {'user': row['user_id'], 'room': room}).fetchone()
            if not member or ban or not follow or row['channel'] == 'backstage' and member['host_session_id'] != principal and not member['character_id']:
                conn.execute('DELETE FROM push_outbox WHERE subscription_id=:id', {'id': row['subscription_id']})
                return None
            claim = new_id()
            conn.execute('UPDATE push_outbox SET lease_until=:lease,claim_token=:claim WHERE subscription_id=:id', {'lease': iso_time(utc_now()+timedelta(seconds=60)), 'claim': claim, 'id': row['subscription_id']})
            return {**dict(row), 'claim_token': claim}

    def finish_push(self, job, outcome):
        with self._transaction(write=True, admission='push-delivery') as conn:
            params = {'id': job['subscription_id'], 'message': job['message_id'], 'claim': job['claim_token']}
            if outcome == 'gone':
                conn.execute('DELETE FROM push_subscriptions WHERE id=:id', params)
            elif outcome == 'sent' or job['attempts'] >= 4:
                conn.execute('DELETE FROM push_outbox WHERE subscription_id=:id AND message_id=:message AND claim_token=:claim', params)
            else:
                params.update(attempts=job['attempts']+1, next=iso_time(utc_now()+timedelta(seconds=min(300,15*2**job['attempts']))))
                conn.execute('UPDATE push_outbox SET attempts=:attempts,next_attempt_at=:next,lease_until=NULL,claim_token=NULL WHERE subscription_id=:id AND message_id=:message AND claim_token=:claim', params)

class ProviderSession(requests.Session):
    def request(self, method, url, **kwargs):
        validate_endpoint(url)
        kwargs.update(allow_redirects=False, stream=True)
        response = super().request(method, url, **kwargs)
        # Provider bodies can contain endpoint credentials; neither read nor log.
        response._content = b''
        response.close()
        return response


def send_push(config, job):
    try:
        with ProviderSession() as session:
            response = webpush({'endpoint': validate_endpoint(job['endpoint']), 'keys': {'p256dh': job['p256dh'], 'auth': job['auth_secret']}},
                               data=json.dumps({'type':'room_message','room_id':job['room_id'],'notification_id':job['message_id']}),
                               vapid_private_key=config.vapid, vapid_claims={'sub':config.subject}, ttl=300,
                               headers={'Topic':'onstage-room'}, timeout=(3,5), requests_session=session)
        if response.status_code in (404,410):
            return 'gone'
        return 'sent' if 200 <= response.status_code < 300 else 'retry'
    except WebPushException as exc:
        return 'gone' if exc.response is not None and exc.response.status_code in (404,410) else 'retry'
    except Exception:
        return 'retry'  # Never emit provider exceptions, endpoints or key material.


class PushWorker:
    def __init__(self, store, config):
        self.store, self.config = store, config

    async def run(self):
        while True:
            try:
                # One owned worker, sequential network calls, four jobs per tick.
                # Restarted leases expire and replay the same notification ID.
                for _ in range(4):
                    job = await anyio.to_thread.run_sync(self.store.claim_push)
                    if not job:
                        break
                    outcome = await anyio.to_thread.run_sync(send_push, self.config, job)
                    await anyio.to_thread.run_sync(self.store.finish_push, job, outcome)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error('push_delivery_failed')
            await asyncio.sleep(2)


class PushKeys(BaseModel):
    model_config = ConfigDict(extra='forbid')
    p256dh: str = Field(max_length=120)
    auth: str = Field(max_length=32)


class PushSubscription(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoint: str = Field(max_length=2048)
    keys: PushKeys


class Follow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


def mount_push(application, store, run_db, credentials, config):
    router = APIRouter(prefix='/api')

    @router.get('/notifications/config')
    async def configuration(request: Request):
        await run_db(store.account_identity, credentials(request)[0])
        return {'enabled':config.enabled, 'public_key':config.public_key}

    @router.get('/notifications/rooms')
    async def follows(request: Request):
        return await run_db(store.notification_rooms, credentials(request)[0])

    @router.post('/notifications/subscriptions')
    async def subscribe(body: PushSubscription, request: Request):
        if not config.enabled:
            raise DomainError(503,'消息通知尚未由维护者启用。')
        return await run_db(store.subscribe_notifications, *credentials(request), body.endpoint, body.keys.p256dh, body.keys.auth, request.headers.get('origin',''))

    @router.delete('/notifications/subscriptions/{subscription_id}')
    async def unsubscribe(subscription_id: str, request: Request):
        return await run_db(store.unsubscribe_notifications, *credentials(request), subscription_id)

    @router.post('/rooms/{room_id}/notifications')
    async def follow(room_id: str, body: Follow, request: Request):
        if body.enabled and not config.enabled:
            raise DomainError(503,'消息通知尚未由维护者启用。')
        return await run_db(store.follow_notifications, *credentials(request), room_id, body.enabled)

    application.include_router(router)
