"""Real PostgreSQL anti-abuse checks across games, devices and concurrent writes."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.community import MEMBER_FEEDBACK_PAIR_GAME_LIMIT
from app.migrations import execute_schema
from app.security import DomainError
from test_community import account
from test_postgres import pg_factory


def pair_games(store, host, voter, count):
    games = []
    for _ in range(count):
        room = store.create_room(*host, 'mygo_rehearsal', None)
        store.join(*voter, room)
        with store._transaction() as conn:
            target = store._historical_member(conn, room, store._session(conn, host[0])['id'])['id']
        store.leave(*voter, room)
        store.control(*host, room, 'end')
        games.append((room, target))
    return games


@pytest.fixture
def votes(pg_factory):
    return pg_factory(room_limit_per_hour=100, room_ip_limit_per_hour=100)


def counts(store):
    with store._transaction() as conn:
        return tuple(conn.execute('SELECT COUNT(*) AS count FROM ' + table).fetchone()['count']
                     for table in ('member_feedback', 'member_feedback_ledger'))


def test_vote_unique_mutable_and_withdrawal_retains_occupancy(votes):
    host, voter = account(votes), account(votes)
    room, target = pair_games(votes, host, voter, 1)[0]
    for value in (1, 1, -1, -1):
        assert votes.member_vote(*voter, room, target, value) == {'value': value}
        assert counts(votes) == (1, 1)
    assert votes.member_vote(*voter, room, target, 0) == {'value': 0}
    assert counts(votes) == (0, 1)
    assert votes.member_vote(*voter, room, target, 1) == {'value': 1}
    assert counts(votes) == (1, 1)
    with votes._transaction() as conn:
        used = conn.execute("SELECT COUNT(*) AS count FROM rate_events WHERE action='member_vote'").fetchone()['count']
    assert used == 4  # Exact duplicate requests consumed no operation allowance.


def test_cross_game_pair_limit_withdrawals_and_other_recipient(votes):
    host, voter = account(votes), account(votes)
    games = pair_games(votes, host, voter, MEMBER_FEEDBACK_PAIR_GAME_LIMIT + 1)
    for room, target in games[:-1]:
        votes.member_vote(*voter, room, target, 1)
        votes.member_vote(*voter, room, target, 0)
    with pytest.raises(DomainError) as caught:
        votes.member_vote(*voter, *games[-1], 1)
    assert caught.value.status_code == 429
    assert str(MEMBER_FEEDBACK_PAIR_GAME_LIMIT) not in caught.value.detail
    assert counts(votes) == (0, MEMBER_FEEDBACK_PAIR_GAME_LIMIT)
    # Existing occupied games remain editable even once the pair is at capacity.
    votes.member_vote(*voter, *games[0], -1)
    other = account(votes)
    votes.member_vote(*voter, *pair_games(votes, other, voter, 1)[0], 1)
    assert counts(votes) == (2, MEMBER_FEEDBACK_PAIR_GAME_LIMIT + 1)


def test_cross_device_concurrent_last_slot_is_atomic(votes):
    host = account(votes)
    voter = account(votes, 'ConcurrentVoter')
    preauth = votes.create_session(source='second-device')
    logged = votes.account_login('ConcurrentVoter', 'password-test-123', source='second-device', token=preauth['token'], csrf=preauth['csrf_token'])
    second = logged['token'], logged['csrf_token']
    games = pair_games(votes, host, voter, MEMBER_FEEDBACK_PAIR_GAME_LIMIT + 1)
    for room, target in games[:-2]:
        votes.member_vote(*voter, room, target, 1)
    barrier = threading.Barrier(2)
    def attempt(credentials, game):
        barrier.wait(timeout=5)
        try:
            return votes.member_vote(*credentials, *game, 1)
        except DomainError as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, credentials, game) for credentials, game in zip((voter, second), games[-2:])]
        results = [future.result(timeout=15) for future in futures]
    assert sum(result == {'value': 1} for result in results) == 1
    assert results.count(429) == 1
    assert counts(votes) == (MEMBER_FEEDBACK_PAIR_GAME_LIMIT,) * 2


def test_schema_backfills_preexisting_votes_without_duplicate_occupancy(votes):
    from pathlib import Path
    host, voter = account(votes), account(votes)
    room, target = pair_games(votes, host, voter, 1)[0]
    votes.member_vote(*voter, room, target, 1)
    with votes._transaction(write=True) as conn:
        conn.execute('DELETE FROM member_feedback_ledger')
        schema = (Path(__file__).parents[1] / 'app/schema_community.sql').read_text(encoding='utf-8')
        execute_schema(conn, schema)
        execute_schema(conn, schema)
    assert counts(votes) == (1, 1)



def test_operation_rate_protection_keeps_retries_idempotent(votes):
    from app.security import iso_time
    host, voter = account(votes), account(votes)
    room, target = pair_games(votes, host, voter, 1)[0]
    votes.member_vote(*voter, room, target, 1)
    with votes._transaction(write=True) as conn:
        voter_id = votes._community_identity(conn, voter[0])[1]['id']
        for _ in range(119):
            conn.execute("INSERT INTO rate_events(action,subject_key,created_at) VALUES('member_vote',:user,:now)", {'user': voter_id, 'now': iso_time()})
    assert votes.member_vote(*voter, room, target, 1) == {'value': 1}
    with pytest.raises(DomainError) as caught:
        votes.member_vote(*voter, room, target, -1)
    assert caught.value.status_code == 429
    with votes._transaction() as conn:
        assert conn.execute('SELECT value FROM member_feedback').fetchone()['value'] == 1
    assert counts(votes) == (1, 1)
