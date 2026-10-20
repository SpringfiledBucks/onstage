from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Literal

import anyio
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.gzip import GZipMiddleware
from sqlalchemy.exc import IntegrityError as SQLIntegrityError, SQLAlchemyError

from .security import COOKIE_NAME, DomainError, Settings, new_id
from .store import create_store
from .cors import OriginPolicy
from .community_api import mount_community
from .telemetry import Telemetry
from .push import PushConfig, PushWorker, mount_push


logger = logging.getLogger("uvicorn.error.onstage")
_db_timings: ContextVar[list[float] | None] = ContextVar("db_timings", default=None)
_request_id: ContextVar[str] = ContextVar("request_id", default="background")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Some Windows MIME databases omit WebP. Keep original bundled artwork served
# as images on every supported host instead of application/octet-stream.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/manifest+json", ".webmanifest")


class TextCompression:
    """Compress text responses, never spend CPU recompressing artwork or WS."""
    def __init__(self, app):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=500, compresslevel=5)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        text = path == "/" or path.startswith("/api/") or path.endswith((".js", ".css", ".json", ".svg", ".webmanifest"))
        target = self.compressed if scope["type"] == "http" and text else self.app
        await target(scope, receive, send)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionBody(Body):
    nickname: str | None = None


class RoomBody(Body):
    scenario_id: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=80)
    background: str | None = Field(default=None, max_length=2000)
    announcement: str = Field(default="", max_length=500)
    access: Literal["public", "password", "invite"] = "public"
    password: str | None = Field(default=None, max_length=128)


class CharacterBody(Body):
    character_id: str = Field(min_length=1, max_length=80)


class RoomSettingsBody(Body):
    background: str = Field(max_length=2000)
    announcement: str = Field(max_length=500)


class ReportBody(Body):
    message_id: str | None = Field(default=None, min_length=1, max_length=80)
    reason: Literal["harassment", "spam", "unsafe", "other"]
    details: str = Field(default="", max_length=1000)


class ResolutionBody(Body):
    status: Literal["resolved", "dismissed"]
    note: str = Field(min_length=1, max_length=500)


class ControlBody(Body):
    action: Literal["start", "pause", "resume", "end", "open_join", "close_join"]


class MessageBody(Body):
    channel: Literal["stage", "backstage"]
    content_type: Literal["text", "sticker"] = "text"
    text: str = Field(default="", max_length=2000)
    sticker_id: str | None = Field(default=None, min_length=1, max_length=80)
    client_message_id: str = Field(min_length=8, max_length=80)


@dataclass(eq=False)
class Connection:
    websocket: WebSocket
    room_id: str
    session_id: str
    member_id: str
    id: str = field(default_factory=new_id)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class Hub:
    """Single-process presence and bounded invalidation delivery; HTTP sync recovers hints."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}
        self._pending_rooms: dict[str, str] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._send_tasks: dict[str, asyncio.Task] = {}
        self._dirty: dict[str, str] = {}
        self._stopping = False

    def start(self) -> None:
        if self._worker is None:
            self._stopping = False
            self._worker = asyncio.create_task(self._deliver(), name="onstage-notifications")

    async def _deliver(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            rooms, self._pending_rooms = self._pending_rooms, {}
            # One owned sender per registered connection, capped by register().
            # A slow peer must not delay the next room's notification batch.
            for item in list(self.connections.values()):
                if item.room_id in rooms and not item.closed:
                    self._dirty[item.id] = rooms[item.room_id]
                    if item.id not in self._send_tasks:
                        self._send_tasks[item.id] = asyncio.create_task(
                            self._flush_connection(item), name="onstage-peer-notification")

    async def _flush_connection(self, connection: Connection) -> None:
        try:
            while connection.id in self._dirty and not connection.closed:
                request_id = self._dirty.pop(connection.id)
                started = time.perf_counter()
                success = await self.send(connection, {"type": "sync"})
                logger.info("notification request_id=%s duration_ms=%.3f success=%s",
                            request_id, (time.perf_counter() - started) * 1000, success)
        finally:
            self._dirty.pop(connection.id, None)
            self._send_tasks.pop(connection.id, None)

    def online(self, room_id: str) -> set[str]:
        return {
            item.member_id for item in self.connections.values()
            if item.room_id == room_id and not item.closed
        }

    def online_all(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for item in self.connections.values():
            if not item.closed:
                result[item.room_id].add(item.member_id)
        return dict(result)

    def register(self, connection: Connection) -> None:
        if len(self.connections.keys() | self._send_tasks.keys()) >= 100:
            raise DomainError(429, "当前连接已满，请稍后再试。")
        per_session = sum(
            item.session_id == connection.session_id
            for item in self.connections.values()
        )
        if per_session >= 6:
            raise DomainError(429, "同一会话打开的连接过多，请关闭多余标签页。")
        self.connections[connection.id] = connection

    async def close(self, connection: Connection, code: int = 1000) -> None:
        if connection.closed:
            return
        # Revoke immediately, before waiting for a transport or its send lock.
        connection.closed = True
        self.connections.pop(connection.id, None)
        sender = self._send_tasks.get(connection.id)
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()
        if not any(item.room_id == connection.room_id for item in self.connections.values()):
            self._pending_rooms.pop(connection.room_id, None)
        try:
            # The deadline includes lock acquisition, not only websocket.close.
            async with asyncio.timeout(1.0):
                async with connection.lock:
                    await connection.websocket.close(code=code)
        except Exception:
            pass

    async def send(self, connection: Connection, payload: dict) -> bool:
        if connection.closed:
            return False
        if payload.get("type") == "sync" and connection.lock.locked():
            return True
        try:
            async with asyncio.timeout(2.0):
                async with connection.lock:
                    if connection.closed:
                        return False
                    await connection.websocket.send_json(payload)
            return True
        except Exception:
            await self.close(connection, code=1011)
            return False

    async def notify(self, room_id: str) -> None:
        # This is enqueue-only: committed writes never wait for a slow browser.
        # Ignore rooms without peers, so client-controlled room IDs cannot grow
        # the map beyond the registered-connection limit.
        if not self._stopping and any(
            item.room_id == room_id and not item.closed for item in self.connections.values()
        ):
            self._pending_rooms[room_id] = _request_id.get()
            self._wake.set()

    async def close_member(self, room_id: str, member_id: str) -> None:
        connections = [
            item for item in self.connections.values()
            if item.room_id == room_id and item.member_id == member_id
        ]
        await asyncio.gather(*(self.close(item, 1008) for item in connections))

    async def close_session(self, session_id: str) -> None:
        connections = [item for item in self.connections.values() if item.session_id == session_id]
        rooms = {item.room_id for item in connections}
        await asyncio.gather(*(self.close(item, 1008) for item in connections))
        await asyncio.gather(*(self.notify(room_id) for room_id in rooms))

    async def close_all(self) -> None:
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._pending_rooms.clear()
        send_tasks = list(self._send_tasks.values())
        for task in send_tasks:
            task.cancel()
        await asyncio.gather(*send_tasks, return_exceptions=True)
        self._send_tasks.clear()
        self._dirty.clear()
        await asyncio.gather(*(self.close(item, 1001) for item in list(self.connections.values())))


def create_app(
    *,
    data_dir: str | Path | None = None,
    database_url: str | None = None,
    public_origin: str | None = None,
    secure_cookie: bool | None = None,
    version: str | None = None,
    session_ttl_seconds: int | None = None,
    message_limit_per_minute: int | None = None,
) -> FastAPI:
    settings = Settings.from_values(
        data_dir=data_dir, database_url=database_url, public_origin=public_origin,
        secure_cookie=secure_cookie, version=version,
        session_ttl_seconds=session_ttl_seconds,
        message_limit_per_minute=message_limit_per_minute,
    )
    write_frozen = os.environ.get("ONSTAGE_WRITE_FROZEN", "0") == "1"
    legacy_guests = os.environ.get("ONSTAGE_LEGACY_GUESTS", "0") == "1"
    if not legacy_guests:
        settings = replace(settings, session_ttl_seconds=min(settings.session_ttl_seconds, 900))
    store = create_store(settings)
    hub = Hub()
    push_config = PushConfig.from_env()
    origins = OriginPolicy(settings)
    cookie_name = COOKIE_NAME if legacy_guests else ("__Host-onstage_session" if settings.secure_cookie else "onstage_account_dev")

    async def run_db(operation, *args, **kwargs):
        started = time.perf_counter()
        try:
            return await anyio.to_thread.run_sync(partial(operation, *args, **kwargs))
        except SQLIntegrityError as exc:
            logger.error("数据库约束拒绝了操作：%s", type(exc).__name__)
            raise DomainError(409, "当前状态与操作冲突，请重新同步房间。") from exc
        except SQLAlchemyError as exc:
            # Never include driver exception text: it may contain SQL/credentials.
            logger.error("数据库操作失败：%s", type(exc).__name__)
            raise DomainError(503, "数据库暂不可用，操作未被确认。") from exc
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            timings = _db_timings.get()
            if timings is not None:
                timings.append(elapsed)
            logger.info("database_operation request_id=%s operation=%s duration_ms=%.3f",
                        _request_id.get(), operation.__name__, elapsed)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        maintenance = None
        push_task = None
        try:
            # PostgreSQL initialize checks the explicit migration; it never DDLs.
            await run_db(store.initialize)
            await run_db(store.recover_presence)
            hub.start()
            async def maintain():
                while True:
                    try:
                        changed = await run_db(store.sweep_offline)
                        for room_id in changed:
                            await hub.notify(room_id)
                        telemetry = getattr(application.state, "telemetry", None)
                        if telemetry:
                            await run_db(telemetry.sample)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.error("background_maintenance_failed")
                    await asyncio.sleep(20)
            maintenance = asyncio.create_task(maintain(), name="onstage-maintenance")
            if push_config.enabled:
                push_task = asyncio.create_task(PushWorker(store, push_config).run(), name="onstage-web-push")
            yield
        finally:
            if push_task:
                push_task.cancel()
                await asyncio.gather(push_task, return_exceptions=True)
            if maintenance:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            await hub.close_all()
            await run_db(store.close)

    application = FastAPI(
        title="Onstage", version=settings.version, lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    application.state.settings = settings
    application.add_middleware(TextCompression)
    application.state.store = store
    application.state.hub = hub
    application.state.push_config = push_config
    application.state.telemetry = Telemetry(store, hub)
    application.state.db_path = None if settings.database_url else store.db_path

    @application.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        headers = {"Retry-After": "60"} if exc.status_code == 429 else None
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Field locations are safe to report; error input values may contain secrets.
        fields = sorted({str(error["loc"][-1]) for error in exc.errors() if error.get("loc")})
        summary = "、".join(fields[:8])
        return JSONResponse(
            {"detail": "请求参数不合法" + ("：" + summary if summary else "。")}, status_code=422
        )

    @application.middleware("http")
    async def protect_http(request: Request, call_next):
        started = time.perf_counter()
        request_id = new_id()
        request_token = _request_id.set(request_id)
        timings: list[float] = []
        timing_token = _db_timings.set(timings)
        try:
            return await protected_response(request, call_next, started, request_id, timings)
        finally:
            _db_timings.reset(timing_token)
            _request_id.reset(request_token)

    async def protected_response(request, call_next, started, request_id, timings):
        response = None
        cors_headers = {}
        if write_frozen and request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            response = JSONResponse({"detail": "站点正在迁移，当前入口已暂停写入。请保留身份与草稿。"},
                                    status_code=503, headers={"Retry-After": "60"})
        if response is None and request.url.path.startswith("/api/"):
            try:
                method = request.headers.get("access-control-request-method", request.method) if request.method == "OPTIONS" else request.method
                origin = request.headers.get("origin")
                credentialed = origins.check(origin, request.url.path, method)
                request.state.anonymous_public = bool(origin and not credentialed)
                cors_headers = origins.headers(origin, request.url.path, method)
                if request.method == "OPTIONS":
                    if method not in {"GET", "POST", "DELETE", "HEAD"}:
                        raise DomainError(403, "请求方法不被允许。")
                    requested = {value.strip().lower() for value in request.headers.get("access-control-request-headers", "").split(",") if value.strip()}
                    if not requested.issubset({"content-type", "x-csrf-token", "accept"}):
                        raise DomainError(403, "请求头不被允许。")
                    response = JSONResponse({}, status_code=200, headers={
                        "Access-Control-Allow-Methods": method,
                        "Access-Control-Allow-Headers": "Content-Type, X-CSRF-Token, Accept",
                        "Access-Control-Max-Age": "300"})
                elif not legacy_guests:
                    public = origins.public_path(request.url.path, request.method)
                    auth_bootstrap = request.url.path in {"/api/session", "/api/auth/register", "/api/auth/login"}
                    if not public and not auth_bootstrap:
                        token, csrf = credentials(request)
                        await run_db(store.account_identity, token, csrf,
                                     admin=request.url.path.startswith("/api/admin/") or
                                     ("/reports" in request.url.path and request.method == "GET") or
                                     request.url.path.endswith("/resolve"),
                                     write=request.method not in {"GET", "HEAD"})
            except DomainError as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if response is None:
            length = request.headers.get("content-length")
            if length:
                try:
                    if int(length) > 65536:
                        response = JSONResponse({"detail": "请求正文过大。"}, status_code=413)
                except ValueError:
                    response = JSONResponse({"detail": "请求长度无效。"}, status_code=400)
        if response is None:
            response = await call_next(request)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        for name, value in cors_headers.items():
            response.headers[name] = value
        response.headers["Server-Timing"] = f"app;dur={elapsed:.3f}, db;dur={sum(timings):.3f}"
        route = request.scope.get("route")
        logger.info("http_request request_id=%s method=%s route=%s status=%d duration_ms=%.3f database_ms=%.3f",
                    request_id, request.method, getattr(route, "path", "unmatched"),
                    response.status_code, elapsed, sum(timings))
        telemetry = getattr(application.state, "telemetry", None)
        if telemetry:
            telemetry.observe(request.method, getattr(route, "path", "unmatched"), response.status_code,
                              elapsed / 1000, sum(timings) / 1000)
        path = request.url.path
        if response.status_code in {200, 304} and re.fullmatch(r"/static/assets/optimized/[0-9a-f]{64}\.webp", path):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif response.status_code in {200, 304} and (path == "/" or path == "/sw.js" or path.startswith("/static/")):
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; "
            f"connect-src 'self' {settings.websocket_origin}; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def credentials(request: Request) -> tuple[str | None, str | None]:
        if getattr(request.state, "anonymous_public", False):
            return None, None
        return request.cookies.get(cookie_name), request.headers.get("x-csrf-token")

    async def room_response(token: str | None, room_id: str) -> dict:
        return await run_db(store.get_room, token, room_id, hub.online(room_id))

    @application.get("/")
    async def index():
        path = STATIC_DIR / "index.html"
        if not path.is_file():
            raise DomainError(503, "前端资源尚未就绪。")
        return FileResponse(path)

    application.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @application.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/"})

    @application.get("/healthz")
    async def health():
        return {"status": "ok", "version": settings.version}

    @application.get("/readyz")
    async def ready():
        return await run_db(store.ready)

    @application.get("/metrics")
    async def metrics(request: Request):
        expected = os.environ.get("ONSTAGE_METRICS_TOKEN", "")
        provided = request.headers.get("authorization", "")
        if not expected or not secrets.compare_digest(provided.encode(), ("Bearer " + expected).encode()):
            raise DomainError(403, "指标接口需要独立授权。")
        return Response(application.state.telemetry.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @application.post("/api/session")
    async def create_session(request: Request, body: SessionBody | None = None):
        address = request.client.host if request.client else "unknown"
        token, csrf = credentials(request)
        if token and not legacy_guests:
            try:
                await run_db(store.account_identity, token)
                return await run_db(store.get_session, token)
            except DomainError as exc:
                if exc.status_code not in {401, 403}:
                    raise
                if exc.status_code == 403:
                    token = None
        created = await run_db(
            store.create_session, token, address, body.nickname if body is not None else None, csrf,
        )
        await asyncio.gather(*(hub.notify(room_id) for room_id in created["changed_rooms"]))
        response = JSONResponse({
            "authenticated": True, "csrf_token": created["csrf_token"], "version": settings.version,
            "nickname": created["nickname"], "requires_nickname": created["requires_nickname"],
        })
        if created["token"] is not None:
            response.set_cookie(
                cookie_name, created["token"], max_age=settings.session_ttl_seconds,
                httponly=True, secure=settings.secure_cookie, samesite="strict", path="/",
            )
        return response

    @application.get("/api/session")
    async def get_session(request: Request):
        token, _ = credentials(request)
        return await run_db(store.get_session, token)

    @application.post("/api/logout")
    async def logout(request: Request):
        token, csrf = credentials(request)
        session_id = await run_db(store.logout, token, csrf)
        await hub.close_session(session_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(
            cookie_name, path="/", httponly=True, secure=settings.secure_cookie, samesite="strict"
        )
        return response

    @application.get("/api/catalog")
    async def catalog(request: Request):
        if not legacy_guests:
            return store.catalog
        token, _ = credentials(request)
        return await run_db(store.get_catalog, token)

    @application.get("/api/rooms")
    async def rooms(request: Request, limit: int = Query(24, ge=1, le=100), cursor: str | None = Query(None, max_length=1500), filter: Literal["all", "active", "ended", "mine"] = "all", sort: Literal["time", "rating", "dialogue"] = "time", preferred: bool = False):
        token, _ = credentials(request)
        if not legacy_guests:
            try:
                return await run_db(store.public_rooms, token, limit, cursor, filter, sort, preferred)
            except DomainError as exc:
                if not token or filter == 'mine' or exc.status_code not in {401, 403}:
                    raise
                return await run_db(store.public_rooms, None, limit, cursor, filter, sort, preferred)
        return await run_db(store.list_rooms, token, hub.online_all(), limit, cursor, filter)

    @application.post("/api/rooms")
    async def create_room(body: RoomBody, request: Request):
        token, csrf = credentials(request)
        room_id = await run_db(
            store.create_room, token, csrf, body.scenario_id, body.title, body.background, body.announcement,
            request.client.host if request.client else "unknown",
            access=body.access, password=body.password,
        )
        return await room_response(token, room_id)

    @application.get("/api/rooms/{room_id}")
    async def room(room_id: str, request: Request):
        token, _ = credentials(request)
        return await room_response(token, room_id)

    @application.post("/api/rooms/{room_id}/join")
    async def join(room_id: str, request: Request):
        token, csrf = credentials(request)
        await run_db(store.join, token, csrf, room_id)
        await hub.notify(room_id)
        return await room_response(token, room_id)

    @application.post("/api/rooms/{room_id}/leave")
    async def leave(room_id: str, request: Request):
        token, csrf = credentials(request)
        member_id = await run_db(store.leave, token, csrf, room_id)
        if member_id is not None:
            await hub.close_member(room_id, member_id)
        await hub.notify(room_id)
        return {"ok": True}

    @application.get("/api/rooms/{room_id}/sync")
    async def sync(
        room_id: str, request: Request,
        after_stage: int = Query(default=0, ge=0, le=9223372036854775807),
        after_backstage: int = Query(default=0, ge=0, le=9223372036854775807),
    ):
        token, _ = credentials(request)
        return await run_db(store.sync, token, room_id, after_stage, after_backstage, hub.online(room_id))

    @application.post("/api/rooms/{room_id}/characters/claim")
    async def claim_character(room_id: str, body: CharacterBody, request: Request):
        token, csrf = credentials(request)
        await run_db(store.claim_character, token, csrf, room_id, body.character_id)
        await hub.notify(room_id)
        return await room_response(token, room_id)

    @application.post("/api/rooms/{room_id}/settings")
    async def update_settings(room_id: str, body: RoomSettingsBody, request: Request):
        token, csrf = credentials(request)
        await run_db(store.update_settings, token, csrf, room_id, body.background, body.announcement)
        await hub.notify(room_id)
        return await room_response(token, room_id)

    @application.post("/api/rooms/{room_id}/members/{member_id}/remove")
    async def remove_member(room_id: str, member_id: str, request: Request):
        token, csrf = credentials(request)
        removed = await run_db(store.remove_member, token, csrf, room_id, member_id)
        await hub.close_member(room_id, removed)
        await hub.notify(room_id)
        return await room_response(token, room_id)

    @application.post("/api/rooms/{room_id}/reports")
    async def create_report(room_id: str, body: ReportBody, request: Request):
        token, csrf = credentials(request)
        result = await run_db(store.create_report, token, csrf, room_id, body.message_id, body.reason, body.details)
        await hub.notify(room_id)
        return result

    @application.get("/api/rooms/{room_id}/reports")
    async def room_reports(
        room_id: str, request: Request,
        after: int = Query(default=0, ge=0, le=9223372036854775807),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        token, _ = credentials(request)
        return await run_db(store.list_reports, token, room_id, after, limit)

    @application.get("/api/reports")
    async def own_reports(
        request: Request,
        after: int = Query(default=0, ge=0, le=9223372036854775807),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        token, _ = credentials(request)
        return await run_db(store.list_reports, token, None, after, limit)

    @application.post("/api/rooms/{room_id}/reports/{report_id}/resolve")
    async def resolve_report(room_id: str, report_id: str, body: ResolutionBody, request: Request):
        token, csrf = credentials(request)
        result = await run_db(store.resolve_report, token, csrf, room_id, report_id, body.status, body.note)
        await hub.notify(room_id)
        return result

    @application.post("/api/rooms/{room_id}/control")
    async def control(room_id: str, body: ControlBody, request: Request):
        token, csrf = credentials(request)
        await run_db(store.control, token, csrf, room_id, body.action)
        await hub.notify(room_id)
        return await room_response(token, room_id)

    @application.post("/api/rooms/{room_id}/messages")
    async def send_message(room_id: str, body: MessageBody, request: Request):
        token, csrf = credentials(request)
        result = await run_db(
            store.send_message, token, csrf, room_id,
            body.channel, body.text, body.client_message_id, body.content_type, body.sticker_id,
        )
        await hub.notify(room_id)
        return result

    @application.get("/api/rooms/{room_id}/messages")
    async def get_messages(
        room_id: str, request: Request,
        channel: Literal["stage", "backstage"] = "stage",
        after: int = Query(default=0, ge=0, le=9223372036854775807),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        token, _ = credentials(request)
        return await run_db(store.get_messages, token, room_id, channel, after, limit)

    @application.websocket("/api/rooms/{room_id}/stream")
    async def stream(websocket: WebSocket, room_id: str):
        connection = None
        token = websocket.cookies.get(cookie_name)
        try:
            origins.check(websocket.headers.get("origin"), f"/api/rooms/{room_id}/stream", "POST", required=True)
            if not legacy_guests:
                await run_db(store.account_identity, token)
            identity = await run_db(store.websocket_identity, token, room_id)
            connection = Connection(
                websocket=websocket, room_id=room_id,
                session_id=identity["session_id"], member_id=identity["member_id"],
            )
            await websocket.accept()
            hub.register(connection)
            # Close the gap between the first DB read and registration (logout/leave may race it).
            await run_db(store.websocket_identity, token, room_id)
            await hub.notify(room_id)
            last_check = time.monotonic()
            last_revision = identity["revision"]
            last_lease = 0.0
            incoming = deque()
            while not connection.closed:
                if time.monotonic() - last_check >= 1:
                    current_identity = await run_db(store.websocket_identity, token, room_id)
                    if current_identity["revision"] != last_revision:
                        await hub.send(connection, {"type": "sync"})
                        last_revision = current_identity["revision"]
                    last_check = time.monotonic()
                try:
                    payload = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                now = time.monotonic()
                while incoming and incoming[0] < now - 60:
                    incoming.popleft()
                incoming.append(now)
                if payload != "ping" or len(incoming) > 120:
                    await hub.close(connection, 1008)
                    break
                if not legacy_guests and now - last_lease >= 20:
                    current_session = await run_db(store.get_session, token)
                    await run_db(store.heartbeat, token, current_session["csrf_token"], room_id, connection.id)
                    last_lease = now
                await hub.send(connection, {"type": "pong"})
        except DomainError:
            if connection is not None:
                await hub.close(connection, 1008)
            else:
                await websocket.close(code=1008)
        except (WebSocketDisconnect, RuntimeError, KeyError):
            pass
        finally:
            if connection is not None:
                await hub.close(connection)
                await hub.notify(room_id)

    mount_community(application, store, settings, run_db, credentials, hub, cookie_name)
    mount_push(application, store, run_db, credentials, push_config)
    return application


app = create_app()
