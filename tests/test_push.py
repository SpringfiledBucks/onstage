"""Real PostgreSQL consent/outbox tests; provider transport is explicitly simulated."""
import base64
import json
import io
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import http_ece
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from py_vapid import Vapid

from app.push import PushConfig, ProviderSession, decode_key, send_push, validate_endpoint
from app.security import DomainError, iso_time, utc_now
from test_postgres import pg_factory
from test_community import setup_room
from test_account_http import account, post, WEB, OTHER


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def keys():
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, b64(public), b64(b'0123456789abcdef')


def subscribe(store, who, suffix='test'):
    _, public, auth = keys()
    return store.subscribe_notifications(*who, 'https://fcm.googleapis.com/send/' + suffix, public, auth, WEB)['id']


def query(store, sql, params=None):
    with store._transaction() as conn:
        return [dict(row) for row in conn.execute(sql, params or {}).fetchall()]


def test_pg_opt_in_coalescing_idempotent_message_and_generation_race(pg_factory):
    s = pg_factory()
    host, actor, _, rid, mids = setup_room(s)
    sub = subscribe(s, host)
    def send(identifier):
        return s.send_message(*actor, rid, 'stage', 'Private text never in notification', identifier)['message']
    send('no-consent-message')
    assert not query(s, 'SELECT * FROM push_outbox')
    s.follow_notifications(*host, rid, True)
    m = send('message-with-consent')
    send('message-with-consent')
    assert len(query(s, 'SELECT * FROM push_outbox')) == 1
    job = s.claim_push()
    assert job['message_id'] == m['id'] and job['subscription_id'] == sub
    assert s.claim_push() is None
    newer = send('new-generation-message')
    s.finish_push(job, 'sent')
    assert query(s, 'SELECT * FROM push_outbox')[0]['message_id'] == newer['id']
    # A crash/restart can only defer the owned lease, not discard its durable hint.
    with s._transaction(write=True) as conn:
        conn.execute('UPDATE push_outbox SET lease_until=:past', {'past':iso_time(utc_now()-timedelta(seconds=1))})
    restarted = s.claim_push()
    assert restarted['message_id'] == newer['id']
    s.finish_push(restarted, 'sent')
    assert not query(s, 'SELECT * FROM push_outbox')


def test_pg_push_permissions_backstage_self_kick_logout_and_ban(pg_factory):
    s = pg_factory()
    host, actor, observer, rid, mids = setup_room(s)
    s.join(*observer, rid)
    for i, who in enumerate((host,actor,observer)):
        subscribe(s, who, str(i)); s.follow_notifications(*who, rid, True)
    s.send_message(*actor, rid, 'backstage', 'secret', 'backstage-notification')
    jobs = query(s, 'SELECT * FROM push_outbox')
    assert len(jobs) == 1  # host only; self excluded, observer cannot read backstage
    s.follow_notifications(*host, rid, False)
    assert not query(s, 'SELECT * FROM push_outbox')
    s.send_message(*host, rid, 'stage', 'hello', 'stage-notification-one')
    assert len(query(s, 'SELECT * FROM push_outbox')) == 2
    s.remove_member(*host, rid, mids[actor[0]])
    # Either invalidated actor or permitted observer comes first; drain both.
    for _ in range(2):
        job = s.claim_push()
        if job:
            assert job['user_id'] == s.account_identity(observer[0])['id']
            s.finish_push(job,'sent')
    s.account_logout(*observer)
    assert len(query(s, 'SELECT * FROM push_subscriptions')) == 2
    s.operator_grant_admin(s.account_identity(host[0])['username'], operator='synthetic-test')
    s.admin_ban(*host, s.account_identity(actor[0])['id'], True, 'Synthetic ban')
    assert len(query(s, 'SELECT * FROM push_subscriptions')) == 1


def test_pg_subscription_binding_concurrency_and_provider_expiry(pg_factory):
    s = pg_factory()
    host, actor, _, rid, _ = setup_room(s)
    sub = subscribe(s,host)
    _, public, auth = keys()
    with pytest.raises(DomainError) as denied:
        s.subscribe_notifications(*actor,'https://fcm.googleapis.com/send/test',public,auth,WEB)
    assert denied.value.status_code == 409
    s.unsubscribe_notifications(*actor,sub)
    assert len(query(s,'SELECT * FROM push_subscriptions')) == 1
    s.follow_notifications(*host,rid,True)
    s.send_message(*actor,rid,'stage','test','claim-concurrent-message')
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda _:s.claim_push(),range(2)))
    assert sum(job is not None for job in jobs) == 1
    s.finish_push(next(j for j in jobs if j),'gone')
    assert not query(s,'SELECT * FROM push_subscriptions')
    assert not query(s,'SELECT * FROM push_outbox')


def test_pg_retry_exhaustion_and_follow_cancellation(pg_factory):
    s=pg_factory();host,actor,_,rid,_=setup_room(s)
    subscribe(s,host);s.follow_notifications(*host,rid,True)
    s.send_message(*actor,rid,'stage','retry','bounded-retry-message')
    for attempt in range(5):
        job=s.claim_push();assert job['attempts']==attempt
        s.finish_push(job,'retry')
        with s._transaction(write=True) as conn:
            conn.execute('UPDATE push_outbox SET next_attempt_at=:now',{'now':iso_time()})
    assert not query(s,'SELECT * FROM push_outbox')
    s.send_message(*actor,rid,'stage','later','cancelled-follow-message')
    s.follow_notifications(*host,rid,False)
    assert s.claim_push() is None


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1/send','https://169.254.169.254/latest','https://fcm.googleapis.com.evil.test/a',
                                    'https://fcm.googleapis.com@evil.test/a','https://fcm.googleapis.com:8443/a','https://fcm.googleapis.com/a#x'])
def test_push_endpoint_rejects_ssrf(endpoint):
    with pytest.raises(DomainError):validate_endpoint(endpoint)


def test_real_webpush_encryption_vapid_and_no_redirect(monkeypatch):
    receiver, public, auth=keys()
    vapid=Vapid();vapid.generate_keys()
    config=PushConfig('public',vapid,'mailto:operator@example.com')
    captured={}
    def fake_send(self,prepared,**kwargs):
        captured.update(body=prepared.body,headers=requests.structures.CaseInsensitiveDict(prepared.headers),options=kwargs)
        response=requests.Response();response.status_code=201;response._content=b'';response.request=prepared;response.raw=io.BytesIO()
        return response
    monkeypatch.setattr(requests.Session,'send',fake_send)
    job={'endpoint':'https://fcm.googleapis.com/send/synthetic','p256dh':public,'auth_secret':auth,
         'room_id':'a'*32,'message_id':'b'*32}
    assert send_push(config,job)=='sent'
    assert captured['options']['allow_redirects'] is False
    assert captured['options']['timeout']==(3,5)
    assert captured['headers']['Authorization'].startswith('vapid ')
    plaintext=http_ece.decrypt(captured['body'],private_key=receiver,auth_secret=b'0123456789abcdef',version='aes128gcm')
    assert json.loads(plaintext)=={'type':'room_message','room_id':'a'*32,'notification_id':'b'*32}
    assert b'room_message' not in captured['body']


def test_push_config_never_generates_missing_key(tmp_path,monkeypatch):
    private=tmp_path/'missing.pem'
    monkeypatch.setenv('ONSTAGE_VAPID_PRIVATE_KEY_FILE',str(private))
    monkeypatch.setenv('ONSTAGE_VAPID_SUBJECT','mailto:operator@example.com')
    with pytest.raises(RuntimeError):PushConfig.from_env()
    assert not private.exists()


def test_pg_old_v2_backup_and_migration_preserve_feedback_occupancy(pg_factory,tmp_path):
    from sqlalchemy import text
    from tools.database import snapshot,read_snapshot,restore,compare
    source,target=pg_factory(),pg_factory()
    host,actor,_,rid,mids=setup_room(source)
    source.member_vote(*host,rid,mids[actor[0]],1)
    # Own synthetic schema is put at the exact deployed _01 table/revision shape.
    with source.engine.begin() as conn:
        for name in ('push_outbox','room_notification_follows','push_subscriptions','member_feedback_ledger'):
            conn.execute(text('DROP TABLE '+name))
        conn.execute(text("UPDATE alembic_version SET version_num='20261020_01'"))
    old=snapshot(source)
    assert old['format']=='onstage-postgres-private-v2'
    path=tmp_path/'private-v2.json';path.write_text(json.dumps(old),encoding='utf8')
    old=read_snapshot(path)
    source.migrate()
    assert source.ready()['community_revision']=='20261020_02'
    assert len(query(source,'SELECT * FROM member_feedback_ledger'))==1
    restore(target,old)
    assert len(query(target,'SELECT * FROM member_feedback_ledger'))==1
    with target.engine.connect() as conn:compare(conn,old)
    assert target.account_identity(actor[0])==source.account_identity(actor[0])


def test_operator_key_generation_preserves_existing_secret(tmp_path):
    from tools.push_keys import generate
    path=tmp_path/'synthetic.pem'
    public=generate(path)
    decode_key(public,65)
    original=path.read_bytes()
    with pytest.raises(FileExistsError):generate(path)
    assert path.read_bytes()==original


def test_pg_push_http_contract_csrf_cors_and_restore(pg_factory,monkeypatch,tmp_path):
    from app.main import create_app
    from tools.database import snapshot,restore
    s=pg_factory();target=pg_factory()
    vapid=Vapid();vapid.generate_keys()
    pem=vapid.private_key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    private=tmp_path/'synthetic.pem';private.write_bytes(pem)
    monkeypatch.setenv('ONSTAGE_VAPID_PRIVATE_KEY_FILE',str(private))
    monkeypatch.setenv('ONSTAGE_VAPID_SUBJECT','mailto:synthetic@example.com')
    monkeypatch.setenv('ONSTAGE_FRONTEND_ORIGINS',WEB)
    monkeypatch.delenv('ONSTAGE_LEGACY_GUESTS',raising=False)
    # Suppress the autonomous sender: this HTTP test deliberately never contacts
    # a public provider. Encryption transport is tested separately above.
    async def no_sender(self):
        import asyncio
        await asyncio.Event().wait()
    monkeypatch.setattr('app.push.PushWorker.run',no_sender)
    app=create_app(database_url=s.settings.database_url,public_origin='https://api.neboer.site',secure_cookie=True)
    with TestClient(app,base_url='https://api.neboer.site') as client:
        h,me=account(client)
        rid=post(client,'/api/rooms',h,{'scenario_id':'mygo_rehearsal'})['room']['id']
        config=client.get('/api/notifications/config',headers=h)
        assert config.status_code==200 and config.json()['enabled']
        decode_key(config.json()['public_key'],65)
        _,public,auth=keys()
        body={'endpoint':'https://fcm.googleapis.com/send/http-synthetic','keys':{'p256dh':public,'auth':auth}}
        no_csrf={key:value for key,value in h.items() if key!='X-CSRF-Token'}
        assert client.post('/api/notifications/subscriptions',headers=no_csrf,json=body).status_code==403
        assert client.post('/api/notifications/subscriptions',headers={**h,'Origin':OTHER},json=body).status_code==403
        sub=post(client,'/api/notifications/subscriptions',h,body)['id']
        post(client,f'/api/rooms/{rid}/notifications',h,{'enabled':True})
        assert client.get('/api/notifications/rooms',headers=h).json()=={'room_ids':[rid]}
        # New sensitive subscription material is included in controlled PG recovery.
        before=snapshot(s);restore(target,before)
        assert query(target,'SELECT id,endpoint FROM push_subscriptions')==query(s,'SELECT id,endpoint FROM push_subscriptions')
        preflight=client.options(f'/api/notifications/subscriptions/{sub}',headers={'Origin':WEB,'Access-Control-Request-Method':'DELETE','Access-Control-Request-Headers':'X-CSRF-Token'})
        assert preflight.status_code==200
        assert client.delete(f'/api/notifications/subscriptions/{sub}',headers=h).status_code==200
        assert not query(s,'SELECT * FROM push_subscriptions')
