import json
import secrets
import subprocess
import sys
import time

import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from conftest import ROOT, Person, create_room, live_pair, seat
from test_multiplayer import speech


def report(person, room_id, message=None, reason="other", details="验收反馈"):
    return person.post(f"/api/rooms/{room_id}/reports", {
        "message_id": message["id"] if message else None, "reason": reason, "details": details,
    })["report"]


def moderate(server, *arguments):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "moderate.py"), *arguments],
        capture_output=True, text=True, timeout=10, env=server.env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_empty_post_restores_exact_cookie_identity_csrf_and_room_membership(people):
    host = people()
    room_id = create_room(host)
    original_cookie = host.client.cookies.get("onstage_session")
    original_csrf = host.csrf
    original_member = host.get(f"/api/rooms/{room_id}")["room"]["me"]["member_id"]
    for _ in range(3):
        response = host.client.post("/api/session")  # Truly empty request body.
        assert response.status_code == 200
        assert response.json()["csrf_token"] == original_csrf
        assert "set-cookie" not in response.headers
        assert host.client.cookies.get("onstage_session") == original_cookie
        assert host.get(f"/api/rooms/{room_id}")["room"]["me"]["member_id"] == original_member
    jar_cookie = next(iter(host.client.cookies.jar))
    assert jar_cookie.has_nonstandard_attr("HttpOnly")
    assert jar_cookie.get_nonstandard_attr("SameSite").lower() == "strict"


def test_host_background_announcement_and_aliases_are_real_and_ended_content_is_read_only(people):
    host, actor = people(), people()
    created = host.post("/api/rooms", {
        "scenario_id": "shared_greenroom", "background": "最初背景", "announcement": "最初公告",
    })["room"]
    room_id = created["id"]
    actor.post(f"/api/rooms/{room_id}/join")
    snapshot = actor.get(f"/api/rooms/{room_id}")["room"]
    assert {m["alias"] for m in snapshot["members"]} == {"观众 1", "观众 2"}
    actor_id = snapshot["me"]["member_id"]
    assert next(m for m in snapshot["members"] if m["id"] == actor_id)["display_name"] == "观众 2"
    assert actor.client.post(f"/api/rooms/{room_id}/settings", json={"background": "篡改", "announcement": ""}).status_code == 403
    assert host.client.post(f"/api/rooms/{room_id}/settings", json={"background": "x" * 2001, "announcement": ""}).status_code == 422
    assert host.client.post(f"/api/rooms/{room_id}/settings", json={"background": "", "announcement": "x" * 501}).status_code == 422
    updated = host.post(f"/api/rooms/{room_id}/settings", {"background": "共同设定\n第二行", "announcement": "新公告"})["room"]
    assert updated["background"] == "共同设定\n第二行" and updated["announcement"] == "新公告"
    assert actor.get(f"/api/rooms/{room_id}/sync")["room"]["announcement"] == "新公告"
    actor.post(f"/api/rooms/{room_id}/leave")
    rejoined = actor.post(f"/api/rooms/{room_id}/join")["room"]
    assert rejoined["me"]["member_id"] == actor_id
    assert next(m for m in rejoined["members"] if m["id"] == actor_id)["alias"] == "观众 2"
    host.post(f"/api/rooms/{room_id}/control", {"action": "end"})
    assert host.client.post(f"/api/rooms/{room_id}/settings", json={"background": "后来", "announcement": ""}).status_code == 409
    assert actor.client.post(f"/api/rooms/{room_id}/characters/claim", json={"character_id": "mygo.tomori"}).status_code == 409
    assert host.client.post(f"/api/rooms/{room_id}/members/{actor_id}/remove", json={}).status_code == 409
    assert host.get(f"/api/rooms/{room_id}")["room"]["background"] == updated["background"]
    # Moderation remains available after the performance is frozen.
    assert report(actor, room_id)["status"] == "open"


def test_removal_revokes_real_ws_releases_seat_and_persists_same_session_ban(server, people):
    host, actor, room_id = live_pair(people)
    audience, outsider = people(), people()
    audience.post(f"/api/rooms/{room_id}/join")
    actor_id = actor.get(f"/api/rooms/{room_id}")["room"]["me"]["member_id"]
    host_id = host.get(f"/api/rooms/{room_id}")["room"]["me"]["member_id"]
    other_room = create_room(outsider)
    other_member = outsider.get(f"/api/rooms/{other_room}")["room"]["me"]["member_id"]
    assert audience.client.post(f"/api/rooms/{room_id}/members/{actor_id}/remove", json={}).status_code == 403
    assert host.client.post(f"/api/rooms/{room_id}/members/{other_member}/remove", json={}).status_code == 404
    assert host.client.post(f"/api/rooms/{room_id}/members/{host_id}/remove", json={}).status_code == 409
    with actor.websocket(room_id) as socket:
        socket.recv(timeout=2)
        removed = host.post(f"/api/rooms/{room_id}/members/{actor_id}/remove")["room"]
        assert actor_id not in {m["id"] for m in removed["members"]}
        with pytest.raises(ConnectionClosed):
            while True:
                socket.recv(timeout=2)
    host.post(f"/api/rooms/{room_id}/members/{actor_id}/remove")  # Idempotent retry.
    assert actor.client.get(f"/api/rooms/{room_id}/sync").status_code == 403
    assert actor.client.post(f"/api/rooms/{room_id}/join", json={}).status_code == 403
    actor.post("/api/session")  # Restoring an anonymous identity must not evade its ban.
    assert actor.client.post(f"/api/rooms/{room_id}/join", json={}).status_code == 403
    with pytest.raises(InvalidStatus):
        with actor.websocket(room_id):
            pass
    host.post(f"/api/rooms/{room_id}/control", {"action": "open_join"})
    assert seat(host, audience, room_id, "mygo.anon")["me"]["character_id"] == "mygo.anon"
    server.restart()
    assert actor.client.post(f"/api/rooms/{room_id}/join", json={}).status_code == 403
    assert room_id not in {r["id"] for r in actor.get("/api/rooms")["rooms"]}
    # The product explicitly does not claim to ban a person across fresh browsers.
    fresh_browser = people()
    assert fresh_browser.post(f"/api/rooms/{room_id}/join")["room"]["me"] is not None


def test_report_target_checks_snapshot_privacy_and_host_conflict_rules(server, people):
    host, actor, room_id = live_pair(people)
    observer, outsider = people(), people()
    observer.post(f"/api/rooms/{room_id}/join")
    public = speech(actor, room_id, "原始可举报正文")
    private = speech(actor, room_id, "旁观者无权读取的后台内容", "backstage")
    other_room = create_room(outsider)
    foreign = speech(outsider, other_room, "其他房间消息", "backstage")
    assert observer.client.post(f"/api/rooms/{room_id}/reports", json={"message_id": private["id"], "reason": "other"}).status_code == 403
    assert observer.client.post(f"/api/rooms/{room_id}/reports", json={"message_id": foreign["id"], "reason": "other"}).status_code == 404
    assert outsider.client.post(f"/api/rooms/{room_id}/reports", json={"message_id": public["id"], "reason": "other"}).status_code == 403
    assert outsider.client.get(f"/api/rooms/{room_id}/reports").status_code == 403
    ordinary = report(observer, room_id, public)
    assert ordinary["is_own"] and not ordinary["against_host"]
    assert ordinary["snapshot"]["message"] == public
    assert report(observer, room_id, public)["id"] == ordinary["id"]
    assert observer.client.post(f"/api/rooms/{room_id}/reports", json={
        "message_id": public["id"], "reason": "spam", "details": "不能覆盖旧说明",
    }).status_code == 409
    room_report = report(observer, room_id)
    old_background = room_report["snapshot"]["room"]["background"]
    host.post(f"/api/rooms/{room_id}/settings", {"background": "新设定", "announcement": "新公告"})
    host_message = speech(host, room_id, "房主发言")
    host_report = report(observer, room_id, host_message)
    assert room_report["against_host"] and host_report["against_host"]
    visible_to_host = host.get(f"/api/rooms/{room_id}/reports")["reports"]
    assert [row["id"] for row in visible_to_host] == [ordinary["id"]]
    assert visible_to_host[0]["can_resolve"]
    for conflict in (room_report, host_report):
        assert host.client.post(f"/api/rooms/{room_id}/reports/{conflict['id']}/resolve", json={
            "status": "dismissed", "note": "房主不得自行结案",
        }).status_code == 403
    assert actor.get("/api/reports")["reports"] == []
    assert observer.client.post(f"/api/rooms/{room_id}/reports/{ordinary['id']}/resolve", json={"status": "resolved", "note": "无权"}).status_code == 403
    decision = host.post(f"/api/rooms/{room_id}/reports/{ordinary['id']}/resolve", {"status": "resolved", "note": "已联系该成员调整发言"})["report"]
    assert decision["resolved_by"] == "host" and decision["status"] == "resolved"
    assert not decision["can_resolve"]
    host.post(f"/api/rooms/{room_id}/reports/{ordinary['id']}/resolve", {"status": "resolved", "note": "已联系该成员调整发言"})
    assert host.client.post(f"/api/rooms/{room_id}/reports/{ordinary['id']}/resolve", json={"status": "dismissed", "note": "不得覆盖"}).status_code == 409
    # A separate data correction cannot rewrite the captured report evidence.
    with server.store._transaction(write=True) as database:
        database.execute("UPDATE messages SET text=:text WHERE id=:id", {"text":"后续变化", "id":public["id"]})
    server.restart()
    receipts = observer.get("/api/reports?limit=2")
    assert receipts["has_more"] and len(receipts["reports"]) == 2
    following = observer.get(f"/api/reports?after={receipts['cursor']}&limit=2")
    all_receipts = {row["id"]: row for row in receipts["reports"] + following["reports"]}
    assert set(all_receipts) == {ordinary["id"], room_report["id"], host_report["id"]}
    assert all_receipts[ordinary["id"]]["snapshot"]["message"]["text"] == public["text"]
    assert all_receipts[ordinary["id"]]["status"] == "resolved"
    assert all_receipts[room_report["id"]]["snapshot"]["room"]["background"] == old_background


def test_backstage_report_receipt_redacts_after_loss_of_read_permission(people):
    host, actor, room_id = live_pair(people)
    private = speech(host, room_id, "后台举报快照", "backstage")
    receipt = report(actor, room_id, private)
    assert receipt["snapshot"]["message"]["text"] == private["text"]
    actor.post(f"/api/rooms/{room_id}/leave")
    own = actor.get("/api/reports")["reports"][0]
    assert own["id"] == receipt["id"] and own["snapshot_redacted"]
    assert own["snapshot"] is None and own["status"] == "open"
    assert actor.get(f"/api/rooms/{room_id}/reports")["reports"][0]["snapshot"] is None
    assert actor.client.post(f"/api/rooms/{room_id}/reports", json={"reason": "other"}).status_code == 403
    assert host.get(f"/api/rooms/{room_id}/reports")["reports"] == []


def test_private_operator_can_resolve_host_reports_and_end_orphan_room_with_audit(server, people):
    host, actor, room_id = live_pair(people)
    host_message = speech(host, room_id, "被反馈的主持发言")
    receipt = report(actor, room_id, host_message)
    pending = moderate(server, "list", "--status", "open")
    assert receipt["id"] in {row["id"] for row in pending["reports"]}
    resolved = moderate(server, "resolve", receipt["id"], "--status", "resolved",
                        "--note", "维护者已核查", "--operator", "local-test-operator")["report"]
    assert resolved["resolved_by"] == "operator" and resolved["operator_label"] == "local-test-operator"
    own = actor.get("/api/reports")["reports"][0]
    assert own["resolved_by"] == "operator" and "operator_label" not in own
    with server.store._transaction(write=True) as database:
        original_host = database.execute("SELECT host_session_id FROM rooms WHERE id=:id", {"id":room_id}).fetchone()[0]
    with actor.websocket(room_id) as socket:
        socket.recv(timeout=2)
        host.post("/api/logout")
        while True:
            try:
                socket.recv(timeout=0.05)
            except TimeoutError:
                break
        ended = moderate(server, "end-room", room_id, "--note", "失联主持收场验收", "--operator", "local-test-operator")
        assert ended["changed"] and ended["status"] == "ended"
        # CLI writes are noticed by the live server's periodic revision check.
        assert json.loads(socket.recv(timeout=3))["type"] == "sync"
    assert actor.get(f"/api/rooms/{room_id}")["room"]["status"] == "ended"
    blocked = actor.client.post(f"/api/rooms/{room_id}/messages", json={
        "channel": "stage", "text": "结束后不能再发", "client_message_id": secrets.token_hex(12),
    })
    assert blocked.status_code == 409
    again = moderate(server, "end-room", room_id, "--note", "重复命令", "--operator", "local-test-operator")
    assert not again["changed"]
    with server.store._transaction(write=True) as database:
        assert database.execute("SELECT host_session_id FROM rooms WHERE id=:id", {"id":room_id}).fetchone()[0] == original_host
        actions = database.execute("SELECT action,operator_label,note FROM moderation_actions WHERE room_id=:id", {"id":room_id}).fetchall()
        assert [tuple(row.values()) for row in actions] == [("end_room", "local-test-operator", "失联主持收场验收")]
    messages = actor.get(f"/api/rooms/{room_id}/messages")["messages"]
    assert sum(m["kind"] == "system" and "服务器维护者结束" in m["text"] for m in messages) == 1
    assert all("失联主持收场验收" not in m["text"] for m in messages)


def test_session_creation_rate_survives_restart_and_valid_identity_restore_is_exempt(server):
    server.env["ONSTAGE_SESSION_LIMIT_PER_MINUTE"] = "3"
    server.restart()
    clients = []
    try:
        for _ in range(3):
            person = Person(server)
            clients.append(person)
        cookie = clients[0].client.cookies.get("onstage_session")
        assert clients[0].client.post("/api/session", json={}).status_code == 200
        with httpx.Client(base_url=server.origin, trust_env=False) as fresh:
            blocked = fresh.post("/api/session", json={}, headers={"X-Forwarded-For": "198.51.100.1"})
            assert blocked.status_code == 429 and blocked.headers["retry-after"]
            server.restart()
            assert fresh.post("/api/session", json={}).status_code == 429
        assert clients[0].client.post("/api/session").status_code == 200
        assert clients[0].client.cookies.get("onstage_session") == cookie
    finally:
        for person in clients:
            person.close()


def test_global_session_capacity_is_released_by_real_logout(server):
    server.env["ONSTAGE_MAX_ACTIVE_SESSIONS"] = "2"
    server.restart()
    first, second = Person(server), Person(server)
    try:
        with httpx.Client(base_url=server.origin, trust_env=False) as fresh:
            assert fresh.post("/api/session", json={}).status_code == 429
            first.post("/api/logout")
            assert fresh.post("/api/session", json={}).status_code == 200
    finally:
        first.close()
        second.close()


def test_room_creation_session_and_source_limits_persist_after_ending_and_restart(server, people):
    server.env["ONSTAGE_ROOM_LIMIT_PER_HOUR"] = "1"
    server.env["ONSTAGE_ROOM_IP_LIMIT_PER_HOUR"] = "2"
    server.restart()
    first, second, third = people(), people(), people()
    first_room = create_room(first)
    first.post(f"/api/rooms/{first_room}/control", {"action": "end"})
    assert first.client.post("/api/rooms", json={"scenario_id": "mygo_rehearsal"}).status_code == 429
    second_room = create_room(second)
    second.post(f"/api/rooms/{second_room}/control", {"action": "end"})
    assert third.client.post("/api/rooms", json={"scenario_id": "mygo_rehearsal"}).status_code == 429
    server.restart()
    assert third.client.post("/api/rooms", json={"scenario_id": "mygo_rehearsal"}).status_code == 429


def test_report_rate_is_durable_and_identical_retry_does_not_consume_another_slot(server, people):
    host, actor, room_id = live_pair(people)
    observer = people()
    observer.post(f"/api/rooms/{room_id}/join")
    targets = [speech(actor, room_id, f"频次验收目标 {index}") for index in range(6)]
    first = None
    for target in targets[:5]:
        created = report(observer, room_id, target)
        first = first or created
    assert report(observer, room_id, targets[0])["id"] == first["id"]
    payload = {"message_id": targets[5]["id"], "reason": "other", "details": "第六次"}
    assert observer.client.post(f"/api/rooms/{room_id}/reports", json=payload).status_code == 429
    server.restart()
    assert observer.client.post(f"/api/rooms/{room_id}/reports", json=payload).status_code == 429
    assert len(observer.get("/api/reports")["reports"]) == 5
