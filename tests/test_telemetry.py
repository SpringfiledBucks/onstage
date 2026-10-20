"""Telemetry label bounds, scrape output, and sampled operational gauges."""
from __future__ import annotations

from pathlib import Path

import json

from prometheus_client import CollectorRegistry

from app.security import Settings
from app.store import Store
from app.telemetry import Telemetry, bounded_method, bounded_route_template, bounded_status


class DummyHub:
    def __init__(self):
        self.connections = {"one": object(), "two": object()}


def test_observations_constrain_all_labels_and_render_prometheus_text(tmp_path):
    store = Store(Settings.from_values(data_dir=tmp_path, public_origin="http://localhost"))
    store.initialize()
    telemetry = Telemetry(store, DummyHub(), registry=CollectorRegistry())

    telemetry.observe("get", "/api/rooms/{room_id}/messages", 200, 0.2, 0.03)
    telemetry.observe("SOME-ARBITRARY-VERB", "/api/rooms/private-user-room/messages", 599, 1.0, 0.4)
    telemetry.observe("GET", "/static/assets/optimized/private-hash.webp", 404, 0.1)
    body = telemetry.render().decode("utf-8")

    assert 'method="GET",route="/api/rooms/{room_id}/messages",status_class="2xx"' in body
    assert 'method="OTHER",route="unmatched",status_class="5xx"' in body
    assert 'method="GET",route="/static/{path}",status_class="4xx"' in body
    assert "onstage_http_request_duration_seconds_bucket" in body
    assert "onstage_database_operation_duration_seconds_bucket" in body
    assert "private-user-room" not in body
    assert "private-hash" not in body
    store.close()


def test_periodic_sample_counts_distinct_live_accounts_and_operational_totals(tmp_path):
    store = Store(Settings.from_values(data_dir=tmp_path, public_origin="http://localhost"))
    store.initialize()
    guest = store.create_session(source="metrics-test", nickname="test")
    account = store.account_register("MetricUser", "metrics-password-123", "metrics-test",
                                     guest["token"], guest["csrf_token"])
    room_id = store.create_room(account["token"], account["csrf_token"], "mygo_rehearsal", None)

    with store._transaction(write=True) as conn:
        room = conn.execute("SELECT stage_seq FROM rooms WHERE id=:id", {"id": room_id}).fetchone()
        seq = room["stage_seq"] + 1
        conn.execute("UPDATE rooms SET stage_seq=:seq WHERE id=:id", {"seq": seq, "id": room_id})
        conn.execute("INSERT INTO messages(id,room_id,seq,channel,member_id,character_id,author,text,created_at,kind,client_message_id,content_type) "
                     "VALUES('speech-metrics',:room,:seq,'stage',NULL,NULL,'测试角色','发言',:now,'speech','client-metrics','text')",
                     {"room": room_id, "seq": seq, "now": "2026-10-20T12:00:00.000000Z"})
        conn.execute("UPDATE auth_sessions SET last_seen_at=:now WHERE user_id=(SELECT id FROM users WHERE username='MetricUser')",
                     {"now": "2026-10-20T12:00:00.000000Z"})
        conn.execute("INSERT INTO developer_feedback(id,user_id,text,created_at) "
                     "SELECT 'feedback-metrics',id,'feedback','2026-10-20T12:00:00.000000Z' FROM users WHERE username='MetricUser'")

    telemetry = Telemetry(store, DummyHub(), registry=CollectorRegistry())
    values = telemetry.sample(now="2026-10-20T12:00:30.000000Z", cutoff="2026-10-20T11:59:00.000000Z")
    output = telemetry.render().decode("utf-8")

    assert values["registered_users"] == 1
    assert values["online_users"] == 1
    assert values["total_rooms"] == 1
    assert values["rooms_preparing"] == 1
    assert values["stage_speech_messages"] == 1
    assert values["open_feedback"] == 1
    assert 'onstage_websocket_connections 2.0' in output
    assert 'onstage_rooms{status="preparing"} 1.0' in output
    assert room_id
    store.close()


def test_route_normalization_never_turns_user_path_segments_into_labels():
    assert bounded_route_template("/api/rooms/{room_id}/sync") == "/api/rooms/{room_id}/sync"
    assert bounded_route_template("/api/rooms/unguessable-token/sync") == "unmatched"
    assert bounded_route_template(None) == "unmatched"
    assert bounded_method("websocket") == "WS"
    assert bounded_method("TRACE") == "OTHER"
    assert bounded_status(204) == "2xx"
    assert bounded_status(700) == "other"


def test_monitoring_assets_are_valid_json_and_do_not_contain_credentials():
    root = Path(__file__).parents[1]
    dashboard = json.loads((root / "deploy/monitoring/onstage-dashboard.json").read_text(encoding="utf-8"))
    prometheus = (root / "deploy/monitoring/prometheus.yml").read_text(encoding="utf-8")
    alerts = (root / "deploy/monitoring/onstage-alerts.yml").read_text(encoding="utf-8")
    assert dashboard["templating"]["list"][0]["type"] == "datasource"
    assert {panel["title"] for panel in dashboard["panels"]} >= {
        "Registered users", "Online users", "HTTP p50 / p95 latency", "Database time p50 / p95",
    }
    assert "credentials_file:" in prometheus
    assert "Bearer " not in prometheus
    assert "OnstageHighServerErrorRate" in alerts
