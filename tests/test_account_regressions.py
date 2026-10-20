from dataclasses import replace

import pytest

from app.security import DomainError
from test_accounts import accounts, register, guest


def test_account_principals_and_consumed_bootstraps_do_not_exhaust_capacity(accounts):
    accounts.settings = replace(accounts.settings, max_active_sessions=2)
    for index in range(4):
        register(accounts, 'CapacityUser' + str(index))
    assert guest(accounts)['token']


def test_anonymous_rank_refresh_reuses_snapshot_without_shared_quota_exhaustion(accounts):
    person = register(accounts)
    accounts.create_room(person['token'], person['csrf_token'], 'mygo_rehearsal', None)
    for _ in range(110):
        assert len(accounts.public_rooms(sort='rating')['rooms']) == 1
    assert len(accounts.public_rooms(sort='dialogue')['rooms']) == 1
    with accounts._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM room_list_snapshots WHERE owner_id=''").fetchone()[0] == 2


def test_public_room_share_switch_blocks_existing_and_new_links(accounts):
    from test_community import setup_room
    host, actor, _, rid, _ = setup_room(accounts)
    message = accounts.send_message(*actor, rid, 'stage', 'Public test', 'public-share-test-id')['message']
    accounts.share_permission(*actor, rid, True)
    share = accounts.create_share(*host, rid, [message['id']])
    accounts.configure_sharing(*host, rid, False)
    for call in (lambda: accounts.create_share(*host, rid, [message['id']]), lambda: accounts.share_detail(share['slug'])):
        with pytest.raises(DomainError) as caught:
            call()
        assert caught.value.status_code == 403
