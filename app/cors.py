"""Path-specific trust boundaries; public reads never consume browser identity."""
import os
from urllib.parse import urlsplit
import re

from .security import DomainError


def valid_origin(value):
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.query and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535))
    except (ValueError, TypeError):
        return False


class OriginPolicy:
    def __init__(self, settings):
        self.users = {settings.public_origin} if settings.public_origin else set()
        self.users.update(filter(None, (x.strip() for x in os.environ.get("ONSTAGE_FRONTEND_ORIGINS", "").split(","))))
        # Admin origin is independently configured, never inherits all user origins.
        self.admin = set(filter(None, (x.strip() for x in os.environ.get("ONSTAGE_ADMIN_ORIGINS", "").split(","))))
        if any(not valid_origin(value) for value in self.users | self.admin):
            raise RuntimeError("Frontend/admin Origins must be exact HTTP(S) Origins.")

    @staticmethod
    def public_path(path, method):
        if method not in {"GET", "HEAD"}:
            return False
        return path in {"/api/catalog", "/api/scenarios", "/api/rooms", "/api/about"} or bool(
            re.fullmatch(r"/api/shares/[A-Za-z0-9_-]{16,100}", path))

    @staticmethod
    def public_subdomain(origin):
        if not valid_origin(origin):
            return False
        parsed = urlsplit(origin)
        return parsed.scheme == "https" and parsed.port in {None, 443} and bool(
            re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+neboer\.site", parsed.hostname or ""))

    def check(self, origin, path, method, *, required=False):
        if not origin:
            if required:
                raise DomainError(403, "缺少可信来源。")
            return False
        allowed = self.admin if path.startswith("/api/admin/") else self.users | self.admin
        if origin in allowed:
            return True
        if self.public_path(path, method) and self.public_subdomain(origin):
            return False
        raise DomainError(403, "请求来源不被允许。")

    def headers(self, origin, path, method):
        credentialed = self.check(origin, path, method)
        if not origin:
            return {}
        result = {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        if credentialed:
            result["Access-Control-Allow-Credentials"] = "true"
        return result
