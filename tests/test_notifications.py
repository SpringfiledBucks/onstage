"""Bounded notification delivery, ACK independence and cursor recovery."""
import asyncio
import time

from fastapi.testclient import TestClient

from app.main import Connection, Hub, create_app


class Peer:
    def __init__(self, blocked=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.sent = []
        self.closed = []

    async def send_json(self, payload):
        self.entered.set()
        await self.release.wait()
        self.sent.append(payload)

    async def close(self, code):
        self.closed.append(code)


def connection(peer, room="room", member="member"):
    return Connection(peer, room, member, member)


def test_notifications_are_coalesced_bounded_and_cleaned():
    async def scenario():
        hub = Hub()
        slow, fast = Peer(blocked=True), Peer()
        hub.register(connection(slow))
        hub.register(connection(fast, "other", "other"))
        hub.start()
        worker = hub._worker
        await hub.notify("room")
        await asyncio.wait_for(slow.entered.wait(), 0.5)
        for i in range(1000):
            await hub.notify("room")
            await hub.notify(f"no-peer-{i}")
        await hub.notify("other")
        assert set(hub._pending_rooms) == {"room", "other"}
        assert hub._worker is worker
        # Healthy rooms receive hints while another peer remains blocked.
        for _ in range(20):
            if fast.sent:
                break
            await asyncio.sleep(0.01)
        assert fast.sent == [{"type": "sync"}]
        slow.release.set()
        for _ in range(20):
            if len(slow.sent) == 2:
                break
            await asyncio.sleep(0.01)
        assert slow.sent == [{"type": "sync"}, {"type": "sync"}]
        await hub.close_all()
        assert worker.done()
        assert not hub._pending_rooms
        assert not hub.connections
        await hub.notify("room")
        assert not hub._pending_rooms
    asyncio.run(scenario())


def test_close_revokes_immediately_and_includes_lock_in_deadline():
    async def scenario():
        hub, peer = Hub(), Peer()
        member = connection(peer)
        hub.register(member)
        await member.lock.acquire()
        started = time.perf_counter()
        close = asyncio.create_task(hub.close_member("room", "member"))
        await asyncio.sleep(0.01)
        assert hub.online("room") == set()
        assert member.closed
        await asyncio.wait_for(close, 1.3)
        assert time.perf_counter() - started < 1.3
        member.lock.release()
    asyncio.run(scenario())


def test_slow_peer_times_out_and_http_sync_remains_authoritative():
    async def scenario():
        hub, peer = Hub(), Peer(blocked=True)
        member = connection(peer)
        hub.register(member)
        hub.start()
        started = time.perf_counter()
        await hub.notify("room")
        assert time.perf_counter() - started < 0.1
        await asyncio.wait_for(peer.entered.wait(), 0.5)
        for _ in range(230):
            if member.closed:
                break
            await asyncio.sleep(0.01)
        assert member.closed
        assert peer.closed == [1011]
        await hub.close_all()
    asyncio.run(scenario())


def test_message_ack_does_not_wait_for_notification_and_sync_recovers(tmp_path, monkeypatch, http_database):
    monkeypatch.setenv('ONSTAGE_LEGACY_GUESTS', '1')
    app = create_app(data_dir=tmp_path, public_origin="http://testserver", secure_cookie=False)
    with TestClient(app) as client:
        result = client.post("/api/session", json={})
        client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
        response = client.post("/api/rooms", json={"scenario_id": "mygo_rehearsal"})
        room = response.json()["room"]["id"]
        assert client.post(f"/api/rooms/{room}/characters/claim",
                           json={"character_id": "mygo.tomori"}).status_code == 200
        host_cookie = client.cookies.get("onstage_session")
        host_csrf = client.headers["X-CSRF-Token"]
        client.cookies.clear()
        client.headers.pop("X-CSRF-Token")
        actor = client.post("/api/session", json={}).json()
        client.headers["X-CSRF-Token"] = actor["csrf_token"]
        assert client.post(f"/api/rooms/{room}/join", json={}).status_code == 200
        assert client.post(f"/api/rooms/{room}/characters/claim",
                           json={"character_id": "mygo.anon"}).status_code == 200
        client.cookies.clear()
        client.cookies.set("onstage_session", host_cookie)
        client.headers["X-CSRF-Token"] = host_csrf
        assert client.post(f"/api/rooms/{room}/control", json={"action": "start"}).status_code == 200
        async def add_peer():
            peer = Peer(blocked=True)
            app.state.hub.register(connection(peer, room))
            return peer
        peer = client.portal.call(add_peer)
        started = time.perf_counter()
        sent = client.post(f"/api/rooms/{room}/messages", json={
            "channel": "stage", "text": "synthetic notification regression",
            "client_message_id": "notification-ack-1"})
        elapsed = time.perf_counter() - started
        assert sent.status_code == 200, sent.text
        assert elapsed < 1.0, "ACK waited for the slow peer's 2s send timeout"
        assert "app;dur=" in sent.headers["Server-Timing"]
        assert "db;dur=" in sent.headers["Server-Timing"]
        assert len(sent.headers["X-Request-ID"]) == 32
        recovered = client.get(f"/api/rooms/{room}/sync").json()
        assert sent.json()["message"]["id"] in {
            row["id"] for row in recovered["messages"]["stage"]}
        assert not peer.sent
    assert not app.state.hub.connections
    assert app.state.hub._worker is None


def test_write_freeze_keeps_readiness_and_reads_but_blocks_mutations(tmp_path, monkeypatch, http_database):
    monkeypatch.setenv('ONSTAGE_LEGACY_GUESTS', '1')
    app = create_app(data_dir=tmp_path, public_origin="http://testserver", secure_cookie=False)
    with TestClient(app) as client:
        result = client.post("/api/session", json={})
        cookie = client.cookies.get("onstage_session")
    monkeypatch.setenv("ONSTAGE_WRITE_FROZEN", "1")
    frozen = create_app(data_dir=tmp_path, public_origin="http://testserver", secure_cookie=False)
    with TestClient(frozen) as client:
        client.cookies.set("onstage_session", cookie)
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/api/session").json()["authenticated"]
        for path in ("/api/session", "/api/logout", "/api/rooms", "/api/rooms/old/messages"):
            response = client.post(path, json={})
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "60"
        assert client.get("/api/session").json()["authenticated"]
