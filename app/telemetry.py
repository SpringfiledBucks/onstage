"""Bounded-cardinality Prometheus metrics for the Onstage service.

The HTTP middleware owns request observations; a small background sampler
refreshes database-backed gauges at most once every 20 seconds. This module
does not start tasks or install middleware so application lifecycle remains
owned by ``main``.
"""
from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


SAMPLE_INTERVAL_SECONDS = 20

# Only route templates that belong to the application may become label values.
# An unrecognized or unmatched request collapses to one bounded label.
KNOWN_ROUTE_TEMPLATES = frozenset({
    "/", "/sw.js", "/healthz", "/readyz", "/metrics",
    "/api/session", "/api/logout", "/api/catalog", "/api/rooms",
    "/api/rooms/{room_id}", "/api/rooms/{room_id}/join",
    "/api/rooms/{room_id}/leave", "/api/rooms/{room_id}/sync",
    "/api/rooms/{room_id}/characters/claim", "/api/rooms/{room_id}/settings",
    "/api/rooms/{room_id}/members/{member_id}/remove",
    "/api/rooms/{room_id}/reports", "/api/reports",
    "/api/rooms/{room_id}/reports/{report_id}/resolve",
    "/api/rooms/{room_id}/control", "/api/rooms/{room_id}/messages",
    "/api/rooms/{room_id}/stream", "/api/auth/register", "/api/auth/login",
    "/api/auth/me", "/api/auth/logout", "/api/auth/preferences",
    "/api/auth/heartbeat", "/api/rooms/{room_id}/participants", "/api/scenarios",
    "/api/about", "/api/feedback", "/api/rooms/{room_id}/access",
    "/api/rooms/{room_id}/unlock", "/api/rooms/{room_id}/invites",
    "/api/rooms/{room_id}/visibility", "/api/rooms/{room_id}/recruitment",
    "/api/rooms/{room_id}/scene", "/api/rooms/{room_id}/heartbeat",
    "/api/rooms/{room_id}/rating", "/api/rooms/{room_id}/members/{member_id}/vote",
    "/api/rooms/{room_id}/members/{member_id}/report",
    "/api/rooms/{room_id}/share-permission", "/api/rooms/{room_id}/shares",
    "/api/shares/{slug}", "/api/shares/{slug}/revoke", "/api/admin/users",
    "/api/admin/users/{user_id}/ban", "/api/admin/rooms", "/api/admin/shares",
    "/api/admin/reports", "/api/admin/feedback", "/api/admin/member-feedback",
    "/api/admin/shares/{slug}/revoke", "/api/admin/reports/{report_id}/resolve",
    "/api/admin/feedback/{feedback_id}/resolve", "/static/{path}",
    "/api/admin/audit", "/api/notifications/config", "/api/notifications/rooms",
    "/api/notifications/subscriptions", "/api/notifications/subscriptions/{subscription_id}",
    "/api/rooms/{room_id}/notifications",
})

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "WS", "WEBSOCKET"})
_STATUS_CLASS = re.compile(r"^[1-5]xx$")
_ROOM_STATUSES = ("preparing", "live", "paused", "ended")


def bounded_method(method: str) -> str:
    value = str(method).upper()
    if value == "WEBSOCKET":
        return "WS"
    return value if value in _METHODS else "OTHER"


def bounded_route_template(route_template: str | None) -> str:
    """Return a known application route template, never an arbitrary URL path."""
    if not isinstance(route_template, str):
        return "unmatched"
    if route_template.startswith("/static/"):
        return "/static/{path}"
    return route_template if route_template in KNOWN_ROUTE_TEMPLATES else "unmatched"


def bounded_status(status: int | str) -> str:
    try:
        code = int(status)
    except (TypeError, ValueError):
        return "other"
    if 100 <= code < 600:
        return f"{code // 100}xx"
    return "other"


class Telemetry:
    """Per-process metric registry and periodic database sampler.

    ``sample`` is intentionally synchronous, matching the store API. Call it
    from one owned periodic worker, not from every request handler.
    """

    def __init__(self, store: Any, hub: Any, *, registry: CollectorRegistry | None = None):
        self.store = store
        self.hub = hub
        self.registry = registry or CollectorRegistry()
        self.http_requests = Counter(
            "onstage_http_requests_total", "Completed HTTP requests",
            ("method", "route", "status_class"), registry=self.registry,
        )
        self.http_duration = Histogram(
            "onstage_http_request_duration_seconds", "HTTP request duration",
            ("method", "route", "status_class"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.db_duration = Histogram(
            "onstage_database_operation_duration_seconds", "Database time within HTTP requests",
            ("method", "route"),
            buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=self.registry,
        )
        self.websocket_connections = Gauge(
            "onstage_websocket_connections", "Current WebSocket connections in this process",
            registry=self.registry,
        )
        self.registered_users = Gauge(
            "onstage_registered_users", "Registered user accounts, including banned accounts",
            registry=self.registry,
        )
        self.online_users = Gauge(
            "onstage_online_users", "Distinct unbanned registered accounts with a valid online lease",
            registry=self.registry,
        )
        self.rooms = Gauge(
            "onstage_rooms", "Retained rooms by lifecycle status", ("status",), registry=self.registry,
        )
        self.total_rooms = Gauge(
            "onstage_total_rooms", "All retained rooms", registry=self.registry,
        )
        self.stage_speech_messages = Gauge(
            "onstage_stage_speech_messages", "Total persisted stage speech messages, including stickers",
            registry=self.registry,
        )
        self.open_reports = Gauge(
            "onstage_open_reports", "Open room, message, and member reports", registry=self.registry,
        )
        self.open_feedback = Gauge(
            "onstage_open_feedback", "Open developer feedback reports", registry=self.registry,
        )
        self.last_sample_monotonic: float | None = None

    def observe(self, method: str, route_template: str | None, status: int | str,
                seconds: float, db_seconds: float = 0.0) -> None:
        """Record one completed request with constrained labels."""
        method_label = bounded_method(method)
        route_label = bounded_route_template(route_template)
        status_label = bounded_status(status)
        self.http_requests.labels(method_label, route_label, status_label).inc()
        self.http_duration.labels(method_label, route_label, status_label).observe(
            max(0.0, float(seconds)),
        )
        db_value = max(0.0, float(db_seconds))
        if db_value > 0:
            self.db_duration.labels(method_label, route_label).observe(db_value)

    @staticmethod
    def _scalar(connection, sql: str, params: dict | None = None) -> int:
        row = connection.execute(sql, params or {}).fetchone()
        return int(row[0] if row is not None else 0)

    def sample(self, *, now: str | None = None, cutoff: str | None = None) -> dict[str, int]:
        """Refresh low-cardinality gauges from one consistent database snapshot.

        An authenticated account is online when it has a recent authenticated
        heartbeat or a still-valid room lease. Account bans, expired/revoked
        authentication sessions, revoked principal sessions, inactive room
        membership, and room bans invalidate leases. ``COUNT(DISTINCT ...)``
        avoids counting multiple devices or simultaneous rooms as users.
        """
        if now is None or cutoff is None:
            from .security import iso_time, utc_now
            current = utc_now()
            now = now or iso_time(current)
            cutoff = cutoff or iso_time(current - timedelta(seconds=90))

        with self.store._transaction() as connection:
            registered = self._scalar(connection, "SELECT COUNT(*) FROM users")
            online = self._scalar(connection,
                'SELECT COUNT(DISTINCT u.id) FROM users u WHERE ' +
                self.store._account_online_clause().replace('users.', 'u.'),
                {"now": now, "cutoff": cutoff})
            room_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM rooms GROUP BY status"
            ).fetchall()
            status_counts = {status: 0 for status in _ROOM_STATUSES}
            for row in room_rows:
                if row[0] in status_counts:
                    status_counts[row[0]] = int(row[1])
            messages = self._scalar(connection,
                "SELECT COUNT(*) FROM messages WHERE channel='stage' AND kind='speech'")
            reports = self._scalar(connection,
                "SELECT (SELECT COUNT(*) FROM reports WHERE status='open') + "
                "(SELECT COUNT(*) FROM member_reports WHERE status='open')")
            feedback = self._scalar(connection,
                "SELECT COUNT(*) FROM developer_feedback WHERE status='open'")

        self.registered_users.set(registered)
        self.online_users.set(online)
        for status, count in status_counts.items():
            self.rooms.labels(status).set(count)
        total_rooms = sum(status_counts.values())
        self.total_rooms.set(total_rooms)
        self.stage_speech_messages.set(messages)
        self.open_reports.set(reports)
        self.open_feedback.set(feedback)
        connections = getattr(self.hub, "connections", {})
        self.websocket_connections.set(len(connections))
        self.last_sample_monotonic = time.monotonic()
        return {
            "registered_users": registered, "online_users": online,
            "total_rooms": total_rooms, "stage_speech_messages": messages,
            "open_reports": reports, "open_feedback": feedback,
            **{f"rooms_{key}": value for key, value in status_counts.items()},
        }

    def render(self) -> bytes:
        """Render the private registry in Prometheus text exposition format."""
        return generate_latest(self.registry)
