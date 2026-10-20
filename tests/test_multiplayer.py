import concurrent.futures
import json
import secrets
import time

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from conftest import create_room, live_pair, seat, sticker


def speech(person, room_id, text, channel="stage", message_id=None):
    return person.post(f"/api/rooms/{room_id}/messages",
                       {"channel": channel, "text": text,
                        "client_message_id": message_id or secrets.token_hex(12)})["message"]


def test_auth_csrf_origin_and_static(server, people):
    with httpx.Client(base_url=server.origin, trust_env=False) as anonymous:
        assert anonymous.get("/api/catalog").status_code == 401
        assert anonymous.post("/api/session", json={"invite_code": "legacy-field"}).status_code == 422
        assert anonymous.post("/api/session", json={},
                              headers={"Origin": "https://unrelated.invalid"}).status_code == 403
        response = anonymous.get("/")
        assert response.status_code == 200
        assert "script-src" in response.headers.get("content-security-policy", "")
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert anonymous.get("/static/app.js").status_code == 200
    person = people()
    missing = person.client.post("/api/rooms", json={"scenario_id": "mygo_rehearsal"},
                                 headers={"X-CSRF-Token": ""})
    assert missing.status_code == 403
    crossed = person.client.post("/api/rooms", json={"scenario_id": "mygo_rehearsal"},
                                 headers={"Origin": "https://unrelated.invalid"})
    assert crossed.status_code == 403
    assert person.get("/api/session")["authenticated"] is True
    assert person.client.cookies.get("onstage_session")
    assert person.get("/readyz")["schema_version"] == 4


def test_catalog_and_all_locations_are_playable(server, people):
    host, actor = people(), people()
    catalog = host.get("/api/catalog")
    assert len(catalog["characters"]) == 10
    assert {b["id"] for b in catalog["bands"]} == {"mygo", "mujica"}
    assert len(catalog["scenarios"]) >= 12
    assert {"mygo_rehearsal", "mujica_changeover", "shared_greenroom"} <= {
        s["id"] for s in catalog["scenarios"]}
    # This is exhaustive catalog coverage, not a default per-IP capacity test.
    server.env["ONSTAGE_ROOM_IP_LIMIT_PER_HOUR"] = str(len(catalog["scenarios"]) + 1)
    server.restart()
    for scenario in catalog["scenarios"]:
        host = people()  # Each host remains inside the public per-session creation limit.
        room_id = create_room(host, scenario["id"])
        seat(host, host, room_id, scenario["character_ids"][0])
        seat(host, actor, room_id, scenario["character_ids"][-1])
        room = host.post(f"/api/rooms/{room_id}/control", {"action": "start"})["room"]
        assert room["status"] == "live"
        assert speech(actor, room_id, "这是独立会话提交的场景验收发言。")["text"]
        assert host.post(f"/api/rooms/{room_id}/control", {"action": "end"})["room"]["status"] == "ended"


def test_permissions_pause_end_and_host_control(people):
    host, actor, room_id = live_pair(people)
    audience, outsider = people(), people()
    audience.post(f"/api/rooms/{room_id}/join")
    assert outsider.client.get(f"/api/rooms/{room_id}/sync").status_code == 403
    assert audience.client.post(f"/api/rooms/{room_id}/control",
                                json={"action": "end"}).status_code == 403
    assert audience.client.post(f"/api/rooms/{room_id}/messages", json={
        "channel": "stage", "text": "cannot impersonate",
        "client_message_id": secrets.token_hex(12)}).status_code == 403
    private = speech(actor, room_id, "后台验收内容", "backstage")
    assert audience.client.get(f"/api/rooms/{room_id}/messages?channel=backstage").status_code == 403
    snapshot = audience.get(f"/api/rooms/{room_id}/sync?after_backstage=99")
    assert snapshot["messages"]["backstage"] == []
    assert not snapshot["room"]["me"]["can_backstage"]
    assert private["id"] in {m["id"] for m in host.get(
        f"/api/rooms/{room_id}/messages?channel=backstage")["messages"]}
    host.post(f"/api/rooms/{room_id}/control", {"action": "pause"})
    paused = actor.client.post(f"/api/rooms/{room_id}/messages", json={
        "channel": "stage", "text": "paused", "client_message_id": secrets.token_hex(12)})
    assert paused.status_code in (403, 409)
    speech(actor, room_id, "暂停期间仍可协调", "backstage")
    host.post(f"/api/rooms/{room_id}/control", {"action": "resume"})
    speech(actor, room_id, "继续演绎")
    host.post(f"/api/rooms/{room_id}/control", {"action": "end"})
    assert actor.get(f"/api/rooms/{room_id}")["room"]["me"]["can_backstage"]
    ended = actor.client.post(f"/api/rooms/{room_id}/messages", json={
        "channel": "backstage", "text": "ended", "client_message_id": secrets.token_hex(12)})
    assert ended.status_code in (403, 409)
    assert host.client.post(f"/api/rooms/{room_id}/control",
                            json={"action": "resume"}).status_code == 409


def test_two_independent_claims_compete_for_one_seat(people):
    host, alice, bob = people(), people(), people()
    room_id = create_room(host)
    for person in (alice, bob):
        person.post(f"/api/rooms/{room_id}/join")
    def claim(person):
        return person.client.post(f"/api/rooms/{room_id}/characters/claim",
                                  json={"character_id": "mygo.tomori"}).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(claim, (alice, bob)))
    assert sorted(statuses) == [200, 409]
    room = host.get(f"/api/rooms/{room_id}")["room"]
    assert sum(m["character_id"] == "mygo.tomori" for m in room["members"]) == 1
    winner = alice if statuses[0] == 200 else bob
    assert claim(winner) == 200  # Same-seat retries are idempotent.


def test_leave_keeps_first_role_and_late_join_needs_host(people):
    host, actor, room_id = live_pair(people)
    newcomer = people()
    newcomer.post(f"/api/rooms/{room_id}/join")
    host.post(f"/api/rooms/{room_id}/control", {"action": "close_join"})
    result = newcomer.client.post(f"/api/rooms/{room_id}/characters/claim",
                                  json={"character_id": "mygo.rana"})
    assert result.status_code in (403, 409)
    actor.post(f"/api/rooms/{room_id}/leave")
    actor.post(f"/api/rooms/{room_id}/join")
    assert actor.get(f"/api/rooms/{room_id}")["room"]["me"]["first_character_id"] == "mygo.anon"
    host.post(f"/api/rooms/{room_id}/control", {"action": "open_join"})
    wrong_role = actor.client.post(f"/api/rooms/{room_id}/characters/claim",
                                   json={"character_id": "mygo.rana"})
    assert wrong_role.status_code in (403, 409)
    seat(host, actor, room_id, "mygo.anon")
    assert host.client.post(f"/api/rooms/{room_id}/leave", json={}).status_code == 409


def test_idempotency_concurrent_retries_and_server_owned_identity(people):
    host, actor, room_id = live_pair(people)
    message_id = secrets.token_hex(12)
    payload = {"channel": "stage", "text": "唯一的一次发言", "client_message_id": message_id}
    def post_one(_):
        response = actor.client.post(f"/api/rooms/{room_id}/messages", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["message"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(post_one, range(2)))
    assert first["id"] == second["id"]
    assert first["character_id"] == "mygo.anon"
    messages = host.get(f"/api/rooms/{room_id}/messages")["messages"]
    assert sum(m["id"] == first["id"] for m in messages) == 1
    conflict = actor.client.post(f"/api/rooms/{room_id}/messages",
                                 json={**payload, "text": "changed"})
    assert conflict.status_code == 409
    forged = actor.client.post(f"/api/rooms/{room_id}/messages", json={
        **payload, "client_message_id": secrets.token_hex(12),
        "author": "administrator", "character_id": "mujica.sakiko"})
    if forged.status_code == 200:
        message = forged.json()["message"]
        assert message["author"] != "administrator"
        assert message["character_id"] == "mygo.anon"
    else:
        assert forged.status_code == 422


def test_sync_pagination_does_not_skip_committed_messages(people):
    host, actor, room_id = live_pair(people)
    sticker_id = host.get("/api/catalog")["stickers"][0]["id"]
    expected = set()
    expected_stickers = set()
    for index in range(103):
        sender = host if index % 2 else actor
        if index % 3:
            result = speech(sender, room_id, f"分页消息 {index}")
        else:
            result = sticker(sender, room_id, sticker_id)
            expected_stickers.add(result["id"])
        expected.add(result["id"])
    received = []
    cursor = 0
    for _ in range(5):
        page = actor.get(f"/api/rooms/{room_id}/sync?after_stage={cursor}")
        received.extend(page["messages"]["stage"])
        cursor = page["cursors"]["stage"]
        if not page["has_more"]["stage"]:
            break
    assert expected <= {message["id"] for message in received}
    assert {message["id"] for message in received if message["content_type"] == "sticker"} == expected_stickers
    assert len({message["id"] for message in received}) == len(received)
    assert cursor == max(message["seq"] for message in received)
    assert actor.get(f"/api/rooms/{room_id}/sync?after_stage={cursor}")["messages"]["stage"] == []


def test_message_limit_and_idempotent_retry_do_not_conflict(people):
    host, actor, room_id = live_pair(people)
    sticker_id = host.get("/api/catalog")["stickers"][0]["id"]
    first_id = secrets.token_hex(12)
    original = speech(actor, room_id, "first", message_id=first_id)
    original_sticker_id = secrets.token_hex(12)
    original_sticker = sticker(actor, room_id, sticker_id, message_id=original_sticker_id)
    for index in range(58):
        if index % 2:
            speech(actor, room_id, f"rate limit {index}")
        else:
            sticker(actor, room_id, sticker_id)
    blocked = actor.client.post(f"/api/rooms/{room_id}/messages", json={
        "channel": "stage", "text": "61", "client_message_id": secrets.token_hex(12)})
    assert blocked.status_code == 429
    assert actor.client.post(f"/api/rooms/{room_id}/messages", json={
        "channel": "stage", "content_type": "sticker", "sticker_id": sticker_id,
        "client_message_id": secrets.token_hex(12),
    }).status_code == 429
    assert speech(actor, room_id, "first", message_id=first_id)["id"] == original["id"]
    assert sticker(actor, room_id, sticker_id, message_id=original_sticker_id)["id"] == original_sticker["id"]


def test_real_websocket_reconnect_presence_and_catchup(people):
    host, actor, room_id = live_pair(people)
    sticker_id = host.get("/api/catalog")["stickers"][0]["id"]
    with host.websocket(room_id) as host_ws, host.websocket(room_id) as same_person_ws:
        assert json.loads(host_ws.recv(timeout=2))["type"] == "sync"
        assert json.loads(same_person_ws.recv(timeout=2))["type"] == "sync"
        assert host.get(f"/api/rooms/{room_id}")["room"]["online_count"] == 1
        with actor.websocket(room_id) as actor_ws:
            assert json.loads(actor_ws.recv(timeout=2))["type"] == "sync"
            assert host.get(f"/api/rooms/{room_id}")["room"]["online_count"] == 2
            actor_ws.send("ping")
            while json.loads(actor_ws.recv(timeout=2))["type"] != "pong":
                pass
            cursor = actor.get(f"/api/rooms/{room_id}/sync")["cursors"]["stage"]
        # The disconnected participant keeps her claimed seat.
        assert actor.get(f"/api/rooms/{room_id}")["room"]["me"]["character_id"] == "mygo.anon"
        missing = speech(host, room_id, "对方断线期间提交，重连后应可补齐。")
        missing_sticker = sticker(host, room_id, sticker_id)
        with actor.websocket(room_id) as restored:
            assert json.loads(restored.recv(timeout=2))["type"] == "sync"
            page = actor.get(f"/api/rooms/{room_id}/sync?after_stage={cursor}")
            assert missing["id"] in {m["id"] for m in page["messages"]["stage"]}
            assert missing_sticker == next(m for m in page["messages"]["stage"] if m["id"] == missing_sticker["id"])
            again = actor.get(f"/api/rooms/{room_id}/sync?after_stage={page['cursors']['stage']}")
            assert again["messages"]["stage"] == []


def test_websocket_origin_room_isolation_and_revocation(people):
    host, actor, room_id = live_pair(people)
    outsider = people()
    with pytest.raises(InvalidStatus):
        with actor.websocket(room_id, origin="https://unrelated.invalid"):
            pass
    with pytest.raises(InvalidStatus):
        with outsider.websocket(room_id):
            pass
    other_id = create_room(outsider, "mujica_changeover")
    assert outsider.client.get(f"/api/rooms/{room_id}/messages").status_code == 403
    with outsider.websocket(other_id) as other_ws:
        assert json.loads(other_ws.recv(timeout=2))["type"] == "sync"
        # Drain the membership notification belonging to this room, if present.
        while True:
            try:
                other_ws.recv(timeout=0.03)
            except TimeoutError:
                break
        speech(actor, room_id, "不得跨房广播")
        with pytest.raises(TimeoutError):
            other_ws.recv(timeout=0.2)
    with actor.websocket(room_id) as actor_ws:
        actor_ws.recv(timeout=2)
        actor.post("/api/logout")
        with pytest.raises(ConnectionClosed):
            while True:
                actor_ws.recv(timeout=2)
    assert actor.client.get("/api/session").status_code == 401


def test_process_restart_and_consistent_backup_restore(server, people, tmp_path):
    host, actor, room_id = live_pair(people)
    message = speech(actor, room_id, "服务重启后仍应保留的真人记录")
    server.restart()
    assert actor.get("/api/session")["authenticated"] is True
    room = actor.get(f"/api/rooms/{room_id}")["room"]
    assert room["status"] == "live"
    assert room["me"]["character_id"] == "mygo.anon"
    assert message["id"] in {m["id"] for m in actor.get(
        f"/api/rooms/{room_id}/messages")["messages"]}
    from tools.database import backup as make_backup, read_snapshot, restore
    from conftest import new_http_database
    backup = tmp_path / "backups" / "verified-private.json"
    result = make_backup(server.store, backup)
    assert result["row_counts"]["messages"] > 0
    with pytest.raises(FileExistsError):
        make_backup(server.store, backup)
    from hashlib import sha256
    assert sha256(backup.read_bytes()).hexdigest() == result["sha256"]
    # Restore into another empty generated PostgreSQL schema. No tables/files
    # are removed to manufacture an empty destination.
    server.stop()
    recovered, cleanup = new_http_database(tmp_path / 'recovered')
    original_cleanup = server.cleanup_database
    def cleanup_both():
        cleanup()
        original_cleanup()
    server.cleanup_database = cleanup_both
    restore(recovered, read_snapshot(backup))
    server.store = recovered
    server.env['DATABASE_URL'] = recovered.settings.database_url
    server.start()
    assert message["id"] in {m["id"] for m in actor.get(
        f"/api/rooms/{room_id}/messages")["messages"]}


def test_session_expiry_blocks_http_and_closes_existing_websocket(server, people):
    from hashlib import sha256
    host, actor, room_id = live_pair(people)
    raw_cookie = actor.client.cookies.get("onstage_session")
    with actor.websocket(room_id) as connection:
        connection.recv(timeout=2)
        # Make this one persisted session expire without delaying the whole suite.
        with server.store._transaction(write=True) as database:
            database.execute("UPDATE sessions SET expires_at=:expires WHERE token_hash=:token",
                             {"expires":"2000-01-01T00:00:00.000000Z", "token":sha256(raw_cookie.encode()).hexdigest()})
        assert actor.client.get("/api/session").status_code == 401
        with pytest.raises(ConnectionClosed):
            while True:
                connection.recv(timeout=3)
    assert host.get(f"/api/rooms/{room_id}")["room"]["status"] == "live"


def test_old_approval_routes_are_gone_and_repeated_end_is_idempotent(people):
    host, actor = people(), people()
    room_id = create_room(host)
    actor.post(f"/api/rooms/{room_id}/join")
    assert actor.client.post(f"/api/rooms/{room_id}/requests", json={"character_id": "mygo.anon"}).status_code == 404
    assert host.client.post(f"/api/rooms/{room_id}/requests/obsolete/decide", json={"approve": True}).status_code == 404
    assert "requests" not in host.get(f"/api/rooms/{room_id}")["room"]
    before = host.post(f"/api/rooms/{room_id}/control", {"action": "end"})["room"]
    first = host.get(f"/api/rooms/{room_id}/messages")["messages"]
    after = host.post(f"/api/rooms/{room_id}/control", {"action": "end"})["room"]
    second = host.get(f"/api/rooms/{room_id}/messages")["messages"]
    assert before["status"] == after["status"] == "ended"
    assert first == second
