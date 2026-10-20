import concurrent.futures
import json
import secrets

import pytest
from websockets.exceptions import ConnectionClosed

from app.security import DomainError, Settings
from app.store import CHARACTER_IDS, Store
from conftest import create_room, live_pair, seat, sticker
from test_guest_moderation import moderate, report
from test_multiplayer import speech


def payload(sticker_id, **changes):
    return {"channel": "stage", "content_type": "sticker", "sticker_id": sticker_id,
            "client_message_id": secrets.token_hex(12), **changes}


def test_sticker_catalog_all_characters_and_legacy_text_compatibility(people):
    host, actor, room_id = live_pair(people)
    catalog = actor.get("/api/catalog")
    stickers = catalog["stickers"]
    assert len(stickers) >= len(CHARACTER_IDS)
    assert len({item["id"] for item in stickers}) == len(stickers)
    assigned = [item for item in stickers if item["character_id"] is not None]
    assert {item["character_id"] for item in assigned} == CHARACTER_IDS
    assert {item["character_id"].split(".")[0] for item in assigned} == {"mygo", "mujica"}
    assert all(item["image"].startswith("/static/") and item["keywords"] for item in stickers)
    # Exercise one real catalog sticker from each character, including cross-band usage.
    for index, character_id in enumerate(sorted(CHARACTER_IDS)):
        item = next(item for item in stickers if item["character_id"] == character_id)
        sender = host if index % 2 else actor
        sent = sticker(sender, room_id, item["id"])
        assert sent["kind"] == "speech" and sent["content_type"] == "sticker"
        assert sent["sticker_id"] == item["id"]
        assert sent["sticker"] == {key: item[key] for key in ("id", "label", "character_id", "image")}
        assert sent["text"] == "[贴纸] " + item["label"]
        assert sent["character_id"] == ("mygo.tomori" if sender is host else "mygo.anon")
        assert sent["author"] == ("高松灯" if sender is host else "千早爱音")
    legacy = speech(actor, room_id, "  原请求的文字与换行\n保持原样  ")
    explicit = actor.post(f"/api/rooms/{room_id}/messages", {
        "channel": "stage", "content_type": "text", "text": "显式文字请求",
        "client_message_id": secrets.token_hex(12),
    })["message"]
    assert legacy["text"] == "  原请求的文字与换行\n保持原样  "
    for message in (legacy, explicit):
        assert message["kind"] == "speech" and message["content_type"] == "text"
        assert message["sticker_id"] is None and message["sticker"] is None
    history = actor.get(f"/api/rooms/{room_id}/messages")["messages"]
    system = next(message for message in history if message["kind"] == "system")
    assert system["content_type"] == "text" and system["sticker_id"] is None and system["sticker"] is None


def test_supplementary_user_stickers_do_not_add_roles_or_change_sender(people):
    host, actor, room_id = live_pair(people)
    catalog = actor.get("/api/catalog")
    assert {item["id"] for item in catalog["characters"]} == CHARACTER_IDS
    items = [item for item in catalog["stickers"] if item["character_id"] is None]
    assert len(items) == 5
    assert {item["source_category"] for item in items} == {"真奈"}
    observer = people()
    observer.post(f"/api/rooms/{room_id}/join")
    assert observer.client.post(f"/api/rooms/{room_id}/characters/claim", json={
        "character_id": "supplementary.mana",
    }).status_code == 409
    for item in items:
        assert actor.client.get(item["image"]).status_code == 200
        message = sticker(actor, room_id, item["id"])
        assert message["sticker"]["character_id"] is None
        assert message["character_id"] == "mygo.anon"
        assert message["author"] == "千早爱音"
        assert message["sticker"]["label"] == item["label"]
        assert message["text"] == "[贴纸] " + item["label"]
    receipt = report(host, room_id, message)
    assert receipt["snapshot"]["message"] == message
    assert host.get(f"/api/rooms/{room_id}/messages")["messages"][-1] == message


def test_invalid_sticker_ids_urls_and_payload_combinations_are_rejected_without_write(people):
    host, actor, room_id = live_pair(people)
    item = actor.get("/api/catalog")["stickers"][0]
    endpoint = f"/api/rooms/{room_id}/messages"
    before = actor.get(endpoint)["messages"]
    invalid = [
        payload("not-in-the-catalog"),
        payload("https://example.invalid/uploaded.png"),
        payload(None), payload(""),
        payload(item["id"], text="混合正文"),
        payload(item["id"], text=" "),
        payload(item["id"], text=None),
        payload(item["id"], content_type="text", text="文字不能带贴纸"),
        payload(item["id"], content_type="video"),
        payload(item["id"], image="https://example.invalid/forged.png"),
        payload(item["id"], sticker=item),
        payload(item["id"], author="伪造作者", character_id="mujica.sakiko"),
        payload(item["id"], kind="system"),
        payload(item["id"], channel="other"),
        {"channel": "stage", "sticker_id": item["id"], "client_message_id": secrets.token_hex(12)},
        {"channel": "stage", "content_type": "sticker", "client_message_id": secrets.token_hex(12)},
    ]
    for body in invalid:
        response = actor.client.post(endpoint, json=body)
        assert response.status_code == 422, response.text
    assert actor.get(endpoint)["messages"] == before


def test_sticker_concurrent_idempotency_and_conflicting_content_type_id_channel(people):
    host, actor, room_id = live_pair(people)
    items = actor.get("/api/catalog")["stickers"]
    endpoint = f"/api/rooms/{room_id}/messages"
    body = payload(items[0]["id"])
    def send_once(_):
        response = actor.client.post(endpoint, json=body)
        assert response.status_code == 200, response.text
        return response.json()["message"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(send_once, range(2)))
    assert first == second
    assert actor.post(endpoint, {**body, "text": ""})["message"] == first
    conflicts = [
        {**body, "sticker_id": items[1]["id"]},
        {**body, "channel": "backstage"},
        {"channel": "stage", "content_type": "text", "text": first["text"],
         "client_message_id": body["client_message_id"]},
    ]
    for changed in conflicts:
        assert actor.client.post(endpoint, json=changed).status_code == 409
    text_key = secrets.token_hex(12)
    speech(actor, room_id, "原来是文字", message_id=text_key)
    assert actor.client.post(endpoint, json={**body, "client_message_id": text_key}).status_code == 409
    host.post(f"/api/rooms/{room_id}/control", {"action": "end"})
    # Acknowledgement recovery remains allowed after end; it doesn't create a new message.
    assert actor.post(endpoint, body)["message"] == first
    assert sum(message["id"] == first["id"] for message in actor.get(endpoint)["messages"]) == 1


def test_sticker_phase_channel_member_permissions_and_removal_are_enforced(people):
    host, actor, observer, outsider = people(), people(), people(), people()
    room_id = create_room(host)
    seat(host, host, room_id, "mygo.tomori")
    seat(host, actor, room_id, "mygo.anon")
    observer.post(f"/api/rooms/{room_id}/join")
    sticker_id = host.get("/api/catalog")["stickers"][0]["id"]
    endpoint = f"/api/rooms/{room_id}/messages"
    assert actor.client.post(endpoint, json=payload(sticker_id)).status_code == 409
    for person in (observer, outsider):
        for channel in ("stage", "backstage"):
            assert person.client.post(endpoint, json=payload(sticker_id, channel=channel)).status_code == 403
    backstage = sticker(actor, room_id, sticker_id, channel="backstage")
    assert observer.client.get(endpoint + "?channel=backstage").status_code == 403
    assert observer.get(f"/api/rooms/{room_id}/sync")["messages"]["backstage"] == []
    host.post(f"/api/rooms/{room_id}/control", {"action": "start"})
    sticker(actor, room_id, sticker_id)
    host.post(f"/api/rooms/{room_id}/control", {"action": "pause"})
    assert actor.client.post(endpoint, json=payload(sticker_id)).status_code == 409
    sticker(actor, room_id, sticker_id, channel="backstage")
    host.post(f"/api/rooms/{room_id}/control", {"action": "resume"})
    member_id = actor.get(f"/api/rooms/{room_id}")["room"]["me"]["member_id"]
    with actor.websocket(room_id) as socket:
        socket.recv(timeout=2)
        host.post(f"/api/rooms/{room_id}/members/{member_id}/remove")
        with pytest.raises(ConnectionClosed):
            while True:
                socket.recv(timeout=3)
    assert actor.client.post(endpoint, json=payload(sticker_id)).status_code == 403
    assert actor.client.get(endpoint + "?channel=backstage").status_code == 403
    host.post(f"/api/rooms/{room_id}/control", {"action": "end"})
    for channel in ("stage", "backstage"):
        assert host.client.post(endpoint, json=payload(sticker_id, channel=channel)).status_code == 409
    assert backstage["id"] in {message["id"] for message in host.get(endpoint + "?channel=backstage")["messages"]}


def test_sticker_send_snapshot_idempotency_report_and_restart_ignore_later_catalog_changes(server, people):
    host, actor, room_id = live_pair(people)
    observer = people()
    observer.post(f"/api/rooms/{room_id}/join")
    items = actor.get("/api/catalog")["stickers"]
    item = items[0]
    message_key = secrets.token_hex(12)
    original = sticker(actor, room_id, item["id"], message_id=message_key)
    private_key = secrets.token_hex(12)
    private = sticker(actor, room_id, item["id"], channel="backstage", message_id=private_key)
    receipt = report(observer, room_id, original)
    assert receipt["snapshot"]["message"] == original
    # A second Store represents a catalog revision without modifying shared source files.
    from app.store import create_store
    revised = create_store(Settings.from_values(database_url=server.env["DATABASE_URL"], public_origin=server.origin, secure_cookie=False))
    revised.initialize()
    revised.stickers[item["id"]] = {**revised.stickers[item["id"]],
                                    "label": "测试中的新版目录名称", "image": items[1]["image"]}
    token = actor.client.cookies.get("onstage_session")
    retry = revised.send_message(token, actor.csrf, room_id, "stage", "", message_key,
                                 "sticker", item["id"])["message"]
    assert retry == original
    updated = revised.send_message(token, actor.csrf, room_id, "stage", "", secrets.token_hex(12),
                                   "sticker", item["id"])["message"]
    assert updated["sticker"]["label"] == "测试中的新版目录名称"
    assert updated["sticker"]["image"] == items[1]["image"]
    del revised.stickers[item["id"]]
    assert revised.send_message(token, actor.csrf, room_id, "stage", "", message_key,
                                "sticker", item["id"])["message"] == original
    assert revised.send_message(token, actor.csrf, room_id, "backstage", "", private_key,
                                "sticker", item["id"])["message"] == private
    with pytest.raises(DomainError) as retired:
        revised.send_message(token, actor.csrf, room_id, "stage", "", secrets.token_hex(12),
                             "sticker", item["id"])
    assert retired.value.status_code == 422
    with pytest.raises(DomainError) as denied:
        revised.send_message(observer.client.cookies.get("onstage_session"), observer.csrf, room_id,
                             "backstage", "", private_key, "sticker", item["id"])
    assert denied.value.status_code == 403
    host.post(f"/api/rooms/{room_id}/reports/{receipt['id']}/resolve", {
        "status": "resolved", "note": "贴纸反馈测试已处理",
    })
    host.post(f"/api/rooms/{room_id}/control", {"action": "end"})
    server.restart()
    history = observer.get(f"/api/rooms/{room_id}/messages")["messages"]
    assert next(message for message in history if message["id"] == original["id"]) == original
    assert next(message for message in history if message["id"] == updated["id"]) == updated
    restored = observer.get("/api/reports")["reports"][0]
    assert restored["status"] == "resolved"
    assert restored["snapshot"]["message"] == original
    assert actor.get("/api/catalog")["stickers"][0] == item


def test_backstage_sticker_report_conflict_operator_and_removed_reporter_privacy(server, people):
    host, actor, room_id = live_pair(people)
    observer, outsider = people(), people()
    observer.post(f"/api/rooms/{room_id}/join")
    sticker_id = host.get("/api/catalog")["stickers"][0]["id"]
    private = sticker(host, room_id, sticker_id, channel="backstage")
    assert observer.client.post(f"/api/rooms/{room_id}/reports", json={
        "message_id": private["id"], "reason": "other",
    }).status_code == 403
    foreign_room = create_room(outsider)
    foreign = sticker(outsider, foreign_room, sticker_id, channel="backstage")
    assert actor.client.post(f"/api/rooms/{room_id}/reports", json={
        "message_id": foreign["id"], "reason": "other",
    }).status_code == 404
    receipt = report(actor, room_id, private)
    assert receipt["against_host"] and receipt["snapshot"]["message"] == private
    assert host.get(f"/api/rooms/{room_id}/reports")["reports"] == []
    assert host.client.post(f"/api/rooms/{room_id}/reports/{receipt['id']}/resolve", json={
        "status": "dismissed", "note": "房主不能处理针对自己的举报",
    }).status_code == 403
    member_id = actor.get(f"/api/rooms/{room_id}")["room"]["me"]["member_id"]
    host.post(f"/api/rooms/{room_id}/members/{member_id}/remove")
    assert actor.post("/api/session")["csrf_token"] == actor.csrf
    for endpoint in ("/api/reports", f"/api/rooms/{room_id}/reports"):
        own = actor.get(endpoint)["reports"][0]
        assert own["id"] == receipt["id"] and own["snapshot_redacted"] and own["snapshot"] is None
    assert actor.client.get(f"/api/rooms/{room_id}/messages?channel=backstage").status_code == 403
    decision = moderate(server, "resolve", receipt["id"], "--status", "resolved",
                        "--note", "维护者核对贴纸反馈", "--operator", "sticker-test-operator")["report"]
    assert decision["snapshot"]["message"] == private and decision["resolved_by"] == "operator"
    own = actor.get("/api/reports")["reports"][0]
    assert own["status"] == "resolved" and own["snapshot"] is None
    assert own["resolution_note"] == "维护者核对贴纸反馈"
