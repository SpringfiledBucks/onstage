import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
import pytest

# Importing app.main constructs the ASGI object but must never connect to a
# fallback database. Actual HTTP fixtures replace this non-connecting DSN with
# a generated schema in the separately authorized test database.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://onstage@127.0.0.1:1/onstage_import_only")


def new_http_database(data_dir):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from app.postgres import PostgresStore, postgres_url
    from app.security import Settings
    value = os.environ.get("ONSTAGE_TEST_DATABASE_URL")
    if not value:
        pytest.fail("ONSTAGE_TEST_DATABASE_URL is required for HTTP integration; no SQLite fallback")
    admin = create_engine(postgres_url(value))
    schema = 'onstage_http_' + secrets.token_hex(10)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    url = make_url(postgres_url(value)).update_query_dict({'options': '-csearch_path=' + schema})
    store = PostgresStore(Settings.from_values(data_dir=data_dir, public_origin='http://localhost',
                                              database_url=url.render_as_string(hide_password=False)))
    try:
        store.migrate()
    except BaseException:
        store.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
        raise
    def cleanup():
        store.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
    return store, cleanup


@pytest.fixture
def http_database(tmp_path, monkeypatch):
    store, cleanup = new_http_database(tmp_path)
    monkeypatch.setenv('DATABASE_URL', store.settings.database_url)
    try:
        yield store
    finally:
        cleanup()

ROOT = Path(__file__).resolve().parents[1]


class LiveServer:
    def __init__(self, root, database_url=None):
        self.root = Path(root)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.cleanup_database = None
        if database_url is None:
            self.store, self.cleanup_database = new_http_database(self.data_dir)
            database_url = self.store.settings.database_url
        else:
            from app.store import create_store
            from app.security import Settings
            self.store = create_store(Settings.from_values(database_url=database_url, public_origin='http://localhost'))
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            self.port = candidate.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, "ONSTAGE_DATA_DIR": str(self.data_dir),
                    "DATABASE_URL": database_url,
                    "ONSTAGE_PUBLIC_ORIGIN": self.origin, "ONSTAGE_SECURE_COOKIE": "0",
                    # Existing v0.8 HTTP contracts explicitly exercise legacy guest mode.
                    # Account-mode API/privacy is exercised separately in test_account_http.py.
                    "ONSTAGE_LEGACY_GUESTS": "1",
                    "ONSTAGE_VERSION": "integration-test"}
        self.process = None
        self.log = None

    def start(self):
        self.log = (self.root / "server.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(self.port), "--workers", "1", "--no-access-log", "--no-proxy-headers",
             "--ws-per-message-deflate", "false"],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 12
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    if client.get(self.origin + "/readyz").status_code == 200:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        self.stop()
        raise AssertionError((self.root / "server.log").read_text(errors="replace")[-5000:])

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.log:
            self.log.close()
            self.log = None

    def restart(self):
        self.stop()
        return self.start()


class Person:
    def __init__(self, server):
        self.server = server
        self.client = httpx.Client(base_url=server.origin, timeout=5, trust_env=False,
                                   headers={"Origin": server.origin})
        result = self.client.post("/api/session", json={})
        assert result.status_code == 200, result.text
        self.csrf = result.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf

    def post(self, path, body=None):
        response = self.client.post(path, json=body or {})
        assert response.status_code in (200, 201), response.text
        return response.json()

    def get(self, path):
        response = self.client.get(path)
        assert response.status_code == 200, response.text
        return response.json()

    def websocket(self, room_id, *, origin=None):
        from websockets.sync.client import connect
        cookie = "; ".join(f"{name}={value}" for name, value in self.client.cookies.items())
        return connect(self.server.origin.replace("http://", "ws://") +
                       f"/api/rooms/{room_id}/stream",
                       origin=self.server.origin if origin is None else origin,
                       additional_headers={"Cookie": cookie}, proxy=None,
                       open_timeout=3, close_timeout=1, compression=None)

    def close(self):
        self.client.close()


@pytest.fixture
def server(tmp_path):
    instance = LiveServer(tmp_path)
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()
        if instance.cleanup_database:
            instance.cleanup_database()


@pytest.fixture
def people(server):
    created = []
    def factory():
        person = Person(server)
        created.append(person)
        return person
    yield factory
    for person in created:
        person.close()


def create_room(host, scenario="mygo_rehearsal"):
    return host.post("/api/rooms", {"scenario_id": scenario})["room"]["id"]


def seat(host, person, room_id, character_id):
    if host is not person:
        person.post(f"/api/rooms/{room_id}/join")
    return person.post(f"/api/rooms/{room_id}/characters/claim", {"character_id": character_id})["room"]



def live_pair(people):
    host, actor = people(), people()
    room_id = create_room(host)
    seat(host, host, room_id, "mygo.tomori")
    seat(host, actor, room_id, "mygo.anon")
    host.post(f"/api/rooms/{room_id}/control", {"action": "start"})
    return host, actor, room_id


def sticker(person, room_id, sticker_id, channel="stage", message_id=None):
    return person.post(f"/api/rooms/{room_id}/messages", {
        "channel": channel, "content_type": "sticker", "sticker_id": sticker_id,
        "client_message_id": message_id or secrets.token_hex(12),
    })["message"]
