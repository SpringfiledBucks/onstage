# Onstage monitoring

The application exposes `GET /metrics` in Prometheus text format. It requires
`Authorization: Bearer <ONSTAGE_METRICS_TOKEN>`; keep that value in the
application secret manager and provide the same value to Prometheus through a
mounted secret file. Do not put it in this directory, a dashboard, or a public
ConfigMap. The sample `prometheus.yml` expects the file at
`/etc/prometheus/secrets/onstage_metrics_token` and the API service at
`onstage:8000`; adjust the scrape target to the private service DNS name used
by the deployment.

The database gauges are refreshed by the application's owned maintenance
worker every 20 seconds. They are read-only aggregate queries. Online users are
distinct registered accounts with a recent authenticated heartbeat or a valid
room presence lease; banned, revoked, expired, inactive-member, and room-banned
identities are excluded. WebSocket connections are process-local, so retain
the one-replica/one-worker deployment constraint until presence and notification
state are shared across instances.

Import `onstage-dashboard.json` into Grafana and select the Prometheus
datasource from its variable. `onstage-alerts.yml` contains baseline service
error and latency alerts; connect those rules to the deployment's existing
Alertmanager receiver. Tune thresholds from observed traffic before treating
them as service-level objectives.

Metrics use bounded route templates and status classes. They never label by
account, room, share token, or request body. The Python process counters reset
when the application restarts; database totals are resampled from PostgreSQL.
