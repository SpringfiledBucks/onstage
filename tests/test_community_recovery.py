"""A fully populated new-version database survives controlled PG recovery."""
from test_postgres import pg_factory
from test_community import account, setup_room
from tools.database import snapshot, restore, compare
from tools.database import EXTENSION_TABLES
from sqlalchemy import text


def test_account_community_full_restore_preserves_identity_and_idempotency(pg_factory):
    source, target = pg_factory(), pg_factory()
    host, actor, _, rid, mids = setup_room(source)
    message = source.send_message(*actor, rid, 'stage', 'Recovery text', 'recovery-logical-message')['message']
    sticker_id = next(iter(source.stickers))
    source.send_message(*actor, rid, 'stage', '', 'recovery-sticker-message', 'sticker', sticker_id)
    source.share_permission(*actor, rid, True)
    share = source.create_share(*host, rid, [message['id']])
    source.heartbeat(*actor, rid, 'test-recovery-device')
    source.public_rooms(host[0], sort='rating')
    source.member_vote(*host, rid, mids[actor[0]], 1)
    source.report_member(*host, rid, mids[actor[0]], 'other', 'Synthetic restore test')
    source.developer_feedback(*host, 'Synthetic feedback')
    source.control(*host, rid, 'end')
    source.rate_room(*actor, rid, 5)
    payload = snapshot(source)
    restore(target, payload)
    with target.engine.connect() as conn:
        compare(conn, payload)
    assert target.account_identity(actor[0]) == source.account_identity(actor[0])
    assert target.share_detail(share['slug']) == source.share_detail(share['slug'])
    # Existing idempotent confirmation must still recover even after the game ended.
    assert target.send_message(*actor, rid, 'stage', 'Recovery text', 'recovery-logical-message')['message']['id'] == message['id']


def test_pg_precommunity_backup_precedes_explicit_migration(pg_factory):
    old, target = pg_factory(), pg_factory()
    # This fixture owns a freshly generated disposable schema, never a runtime DB.
    with old.engine.begin() as connection:
        for name in reversed(EXTENSION_TABLES):
            connection.execute(text('DROP TABLE ' + name))
    before = snapshot(old)
    assert before['format'] == 'onstage-postgres-private-v1'
    old.migrate()
    assert old.ready()['community_revision'] == '20261020_02'
    restore(target, before)
    with target.engine.connect() as connection:
        compare(connection, before)
