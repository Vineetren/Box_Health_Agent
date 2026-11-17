import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from box_health_agent.agent import HealthAgent
from box_health_agent.api.app import create_app


@pytest.fixture
def client(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        """
server:
  host: 127.0.0.1
  port: 0
database:
  path: data/test.db
  retention_days: 7
sampling:
  metrics_interval_sec: 5
  probe_interval_sec: 15
alerts:
  cpu_high_percent: 85
  cpu_high_duration_sec: 60
process_watch:
  watch_list: []
network_probes:
  dns_host: localhost
  tcp_host: 127.0.0.1
  tcp_port: 65530
  api_endpoint:
    url: http://127.0.0.1:65530/
    timeout_sec: 1
""",
        encoding="utf-8",
    )
    agent = HealthAgent(cfg)
    agent.start()
    yield TestClient(create_app(agent))
    agent.stop()


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "warn", "critical")
    assert "open_alerts" in body


def test_metrics_and_processes(client):
    import time

    time.sleep(6)
    r = client.get("/metrics", params={"name": "cpu.total_percent"})
    assert r.status_code == 200
    assert "metrics" in r.json()
    r2 = client.get("/processes", params={"sort": "cpu"})
    assert r2.status_code == 200
    assert "processes" in r2.json()


def test_alerts_query(client):
    r = client.get("/alerts", params={"open": "true"})
    assert r.status_code == 200
    assert "alerts" in r.json()


def test_dashboard_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Box Health Agent" in r.text