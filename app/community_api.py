"""HTTP transport only; all authorization and writes live in store transactions."""
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .security import COOKIE_NAME


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Body):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    remember: bool = False


class Register(Body):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class Characters(Body):
    character_ids: list[str] = Field(max_length=10)


class Access(Body):
    access: Literal["public", "password", "invite"]
    password: str | None = Field(default=None, max_length=128)
    share_enabled: bool | None = None


class Unlock(Body):
    password: str | None = Field(default=None, max_length=128)
    invite: str | None = Field(default=None, max_length=256)


class Invite(Body):
    expires_hours: int = Field(default=24, ge=1, le=168)
    max_uses: int = Field(default=1, ge=1, le=100)


class Visibility(Body):
    hidden: bool


class Scene(Body):
    scenario_id: str = Field(min_length=1, max_length=80)
    action_id: str = Field(min_length=8, max_length=80)


class Heartbeat(Body):
    connection_id: str = Field(default="http", min_length=1, max_length=80)


class Rating(Body):
    rating: int = Field(ge=1, le=5)


class Vote(Body):
    value: Literal[-1, 0, 1]


class Report(Body):
    reason: Literal["harassment", "spam", "unsafe", "other"]
    details: str = Field(default="", max_length=1000)


class Permission(Body):
    allowed: bool


class Share(Body):
    message_ids: list[str] = Field(min_length=1, max_length=100)


class Feedback(Body):
    text: str = Field(min_length=1, max_length=2000)


class Ban(Body):
    banned: bool
    note: str = Field(min_length=1, max_length=500)


class Resolution(Body):
    status: Literal["resolved", "dismissed"]
    note: str = Field(min_length=1, max_length=500)


def mount_community(application, store, settings, run_db, credentials, hub, cookie_name=COOKIE_NAME):
    router = APIRouter(prefix="/api")

    def auth_response(result):
        user = dict(result["user"])
        user.update(csrf_token=result["csrf_token"], storage_scope=result["storage_scope"])
        response = JSONResponse(user)
        response.set_cookie(cookie_name, result["token"],
                            max_age=30 * 86400 if result["remember"] else None,
                            httponly=True, secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    @router.post("/auth/register")
    async def register(body: Register, request: Request):
        token, csrf = credentials(request)
        result = await run_db(store.account_register, body.username, body.password,
                              request.client.host if request.client else "unknown", token, csrf)
        return auth_response(result)

    @router.post("/auth/login")
    async def login(body: Login, request: Request):
        token, csrf = credentials(request)
        result = await run_db(store.account_login, body.username, body.password, body.remember,
                              request.client.host if request.client else "unknown", token, csrf)
        return auth_response(result)

    @router.get("/auth/me")
    async def me(request: Request):
        return await run_db(store.account_identity, credentials(request)[0])

    @router.post("/auth/logout")
    async def logout(request: Request):
        token, csrf = credentials(request)
        await run_db(store.account_logout, token, csrf)
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name, httponly=True, secure=settings.secure_cookie, samesite="strict", path="/")
        return response

    @router.post("/auth/preferences")
    async def preferences(body: Characters, request: Request):
        return await run_db(store.set_preferences, *credentials(request), body.character_ids)

    @router.post("/auth/heartbeat")
    async def account_heartbeat(request: Request):
        return await run_db(store.account_heartbeat, *credentials(request))

    @router.get("/rooms/{room_id}/participants")
    async def participants(room_id: str, request: Request):
        def read():
            with store._transaction() as connection:
                session = store._session(connection, credentials(request)[0])
                store._account(connection, session)
                own = store._historical_member(connection, room_id, session["id"])
                rows = connection.execute('SELECT id,COALESCE(character_id,first_character_id) AS character_id,display_alias,status FROM room_members WHERE room_id=:room ORDER BY joined_at,id LIMIT 151', {"room": room_id}).fetchall()
                permission = connection.execute('SELECT allowed FROM room_share_permissions WHERE room_id=:r AND member_id=:m', {"r": room_id, "m": own["id"]}).fetchone()
                return {"me": {"member_id": own["id"], "share_allowed": bool(permission and permission["allowed"])},
                        "members": [{"id": row["id"], "character_id": row["character_id"],
                                     "display_name": store.characters[row["character_id"]]["name"] if row["character_id"] else row["display_alias"],
                                     "status": row["status"]} for row in rows]}
        return await run_db(read)

    @router.get("/scenarios")
    async def scenarios(limit: int = Query(24, ge=1, le=100), cursor: str | None = Query(None, max_length=500)):
        return await run_db(store.public_scenes, limit, cursor)

    @router.get("/about")
    async def about():
        return {"name": "Onstage", "repository": "https://github.com/SpringfiledBucks/onstage",
                "authors": ["SpringfiledBucks"], "contributors": "https://github.com/SpringfiledBucks/onstage/graphs/contributors"}

    @router.post("/feedback")
    async def feedback(body: Feedback, request: Request):
        return await run_db(store.developer_feedback, *credentials(request), body.text)

    @router.post("/rooms/{room_id}/access")
    async def access(room_id: str, body: Access, request: Request):
        token, csrf = credentials(request)
        result = await run_db(store.configure_room_access, token, csrf, room_id, body.access, body.password)
        if body.share_enabled is not None:
            await run_db(store.configure_sharing, token, csrf, room_id, body.share_enabled)
        await hub.notify(room_id)
        return result

    @router.post("/rooms/{room_id}/unlock")
    async def unlock(room_id: str, body: Unlock, request: Request):
        return await run_db(store.room_unlock, *credentials(request), room_id, body.password, body.invite)

    @router.post("/rooms/{room_id}/invites")
    async def invite(room_id: str, body: Invite, request: Request):
        return await run_db(store.create_invite, *credentials(request), room_id, body.expires_hours, body.max_uses)

    @router.post("/rooms/{room_id}/visibility")
    async def visibility(room_id: str, body: Visibility, request: Request):
        result = await run_db(store.hide_room, *credentials(request), room_id, body.hidden)
        await hub.notify(room_id)
        return result

    @router.post("/rooms/{room_id}/recruitment")
    async def recruitment(room_id: str, body: Characters, request: Request):
        result = await run_db(store.recruit_roles, *credentials(request), room_id, body.character_ids)
        await hub.notify(room_id)
        return result

    @router.post("/rooms/{room_id}/scene")
    async def scene(room_id: str, body: Scene, request: Request):
        result = await run_db(store.switch_scene, *credentials(request), room_id, body.scenario_id, body.action_id)
        await hub.notify(room_id)
        return result

    @router.post("/rooms/{room_id}/heartbeat")
    async def heartbeat(room_id: str, body: Heartbeat, request: Request):
        return await run_db(store.heartbeat, *credentials(request), room_id, body.connection_id)

    @router.post("/rooms/{room_id}/rating")
    async def rating(room_id: str, body: Rating, request: Request):
        return await run_db(store.rate_room, *credentials(request), room_id, body.rating)

    @router.post("/rooms/{room_id}/members/{member_id}/vote")
    async def vote(room_id: str, member_id: str, body: Vote, request: Request):
        return await run_db(store.member_vote, *credentials(request), room_id, member_id, body.value)

    @router.post("/rooms/{room_id}/members/{member_id}/report")
    async def report(room_id: str, member_id: str, body: Report, request: Request):
        return await run_db(store.report_member, *credentials(request), room_id, member_id, body.reason, body.details)

    @router.post("/rooms/{room_id}/share-permission")
    async def share_permission(room_id: str, body: Permission, request: Request):
        return await run_db(store.share_permission, *credentials(request), room_id, body.allowed)

    @router.post("/rooms/{room_id}/shares")
    async def share(room_id: str, body: Share, request: Request):
        return await run_db(store.create_share, *credentials(request), room_id, body.message_ids)

    @router.get("/shares/{slug}")
    async def share_detail(slug: str):
        return await run_db(store.share_detail, slug)

    @router.post("/shares/{slug}/revoke")
    async def revoke_share(slug: str, request: Request):
        return await run_db(store.revoke_share, *credentials(request), slug)

    @router.get("/admin/users")
    async def users(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                    search: str | None = Query(None, max_length=64), banned: bool | None = None, online: bool | None = None):
        return await run_db(store.admin_users, credentials(request)[0], limit, cursor, search, banned, online)

    @router.post("/admin/users/{user_id}/ban")
    async def ban(user_id: str, body: Ban, request: Request):
        result = await run_db(store.admin_ban, *credentials(request), user_id, body.banned, body.note)
        # Per-connection DB revalidation also enforces revocation within one second.
        return result

    @router.get("/admin/rooms")
    async def admin_rooms(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None,
                          user_id: str | None = None, status: str | None = None):
        return await run_db(store.admin_rooms, credentials(request)[0], limit=limit, cursor=cursor, user_id=user_id, status=status)

    @router.get("/admin/shares")
    async def admin_shares(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
        return await run_db(store.admin_shares, credentials(request)[0], limit=limit, cursor=cursor)

    @router.get("/admin/reports")
    async def admin_reports(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None, status: str | None = None):
        return await run_db(store.admin_reports, credentials(request)[0], limit=limit, cursor=cursor, status=status)

    @router.get("/admin/feedback")
    async def admin_feedback(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None, status: str | None = None):
        return await run_db(store.admin_feedback, credentials(request)[0], limit=limit, cursor=cursor, status=status)

    @router.get("/admin/member-feedback")
    async def admin_votes(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None, room_id: str | None = None):
        return await run_db(store.admin_member_feedback, credentials(request)[0], limit=limit, cursor=cursor, room_id=room_id)

    @router.get("/admin/audit")
    async def admin_audit(request: Request, limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
        return await run_db(store.admin_audit, credentials(request)[0], limit=limit, cursor=cursor)

    @router.post("/admin/shares/{slug}/revoke")
    async def admin_revoke(slug: str, request: Request):
        return await run_db(store.admin_revoke_share, *credentials(request), slug)

    @router.post("/admin/reports/{report_id}/resolve")
    async def admin_report_resolve(report_id: str, body: Resolution, request: Request):
        return await run_db(store.admin_resolve_report, *credentials(request), report_id, body.status, body.note)

    @router.post("/admin/feedback/{feedback_id}/resolve")
    async def admin_feedback_resolve(feedback_id: str, body: Resolution, request: Request):
        return await run_db(store.admin_resolve_feedback, *credentials(request), feedback_id, body.status, body.note)

    application.include_router(router)
