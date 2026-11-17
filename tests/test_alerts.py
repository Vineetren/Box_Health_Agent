import tempfile
import time
from pathlib import Path

import pytest

from box_health_agent.alerts.engine import AlertEngine
from box_health_agent.db.storage import Database


@pytest.fixture
def engine():
    with tempfile.TemporaryDirectory() as d:
        db = Database(Path(d) / "t.db")
        cfg = {
            "alerts": {
                "cpu_high_percent": 85,
                "cpu_high_duration_sec": 60,
                "ram_high_percent": 90,
                "disk_high_percent": 90,
                "network_failures_before_alert": 3,
                "zombie_min_duration_sec": 30,
                "memory_climb_samples": 10,
                "memory_climb_rising_count": 9,
                "memory_climb_min_mb": 50,
            },
            "process_watch": {"watch_list": ["camera_daemon"]},
        }
        yield AlertEngine(db, cfg), db


def _sys_row(name, val, extra=None):
    return [("system", name, val, extra)]


def test_cpu_high_not_before_60s(engine):
    eng, db = engine
    t0 = time.time()
    rows = _sys_row("cpu.total_percent", 95.0)
    for i in range(11):
        eng.evaluate(t0 + i * 5, rows, [], [])
    assert db.get_open_alert("cpu_high", "cpu") is None
    eng.evaluate(t0 + 12 * 5, rows, [], [])
    assert db.get_open_alert("cpu_high", "cpu") is not None


def test_cpu_high_closes(engine):
    eng, db = engine
    t0 = time.time()
    high = _sys_row("cpu.total_percent", 95.0)
    low = _sys_row("cpu.total_percent", 10.0)
    for i in range(13):
        eng.evaluate(t0 + i * 5, high, [], [])
    assert db.get_open_alert("cpu_high", "cpu") is not None
    eng.evaluate(t0 + 70, low, [], [])
    assert db.get_open_alert("cpu_high", "cpu") is None


def test_ram_high(engine):
    eng, db = engine
    t = time.time()
    eng.evaluate(t, _sys_row("mem.ram_percent", 91.0), [], [])
    assert db.get_open_alert("ram_high", "ram") is not None


def test_disk_high(engine):
    eng, db = engine
    t = time.time()
    rows = [("system", "disk.usage_percent./test", 92.0, {"mount": "/test"})]
    eng.evaluate(t, rows, [], [])
    assert db.get_open_alert("disk_high", "/test") is not None


def test_network_lost_three_strikes(engine):
    eng, db = engine
    t = time.time()
    fail_probe = [
        {"probe": "gateway_reachable", "success": False},
        {"probe": "internet_ping", "success": True},
    ]
    for i in range(2):
        eng.evaluate(t + i, [], [], fail_probe)
        assert db.get_open_alert("network_lost", "uplink") is None
    eng.evaluate(t + 2, [], [], fail_probe)
    assert db.get_open_alert("network_lost", "uplink") is not None
    ok_probe = [
        {"probe": "gateway_reachable", "success": True},
        {"probe": "internet_ping", "success": True},
    ]
    eng.evaluate(t + 3, [], [], ok_probe)
    assert db.get_open_alert("network_lost", "uplink") is None


def test_process_died(engine):
    eng, db = engine
    t = time.time()
    procs = [{"pid": 4242, "name": "camera_daemon", "status": "R", "mem_rss_mb": 10, "cpu_percent": 0}]
    eng.evaluate(t, [], procs, [])
    eng.evaluate(t + 5, [], [], [])
    assert db.get_open_alert("process_died", "camera_daemon:pid4242") is not None


def test_process_died_closes_when_process_returns(engine):
    eng, db = engine
    t = time.time()
    first = [{"pid": 4242, "name": "camera_daemon", "status": "R", "mem_rss_mb": 10, "cpu_percent": 0}]
    eng.evaluate(t, [], first, [])
    eng.evaluate(t + 5, [], [], [])
    assert db.get_open_alert("process_died", "camera_daemon:pid4242") is not None
    again = [{"pid": 5151, "name": "camera_daemon", "status": "R", "mem_rss_mb": 10, "cpu_percent": 0}]
    eng.evaluate(t + 10, [], again, [])
    assert db.list_alerts(open_only=True) == []


def test_disk_high_closes_below_threshold(engine):
    eng, db = engine
    t = time.time()
    high = [("system", "disk.usage_percent./test", 92.0, {"mount": "/test"})]
    low = [("system", "disk.usage_percent./test", 80.0, {"mount": "/test"})]
    eng.evaluate(t, high, [], [])
    assert db.get_open_alert("disk_high", "/test") is not None
    eng.evaluate(t + 5, low, [], [])
    assert db.get_open_alert("disk_high", "/test") is None


def test_memory_climbing(engine):
    eng, db = engine
    t0 = 1700000000.0
    pid = 9999
    for m in range(10):
        mb = 100 + m * 6
        procs = [{"pid": pid, "name": "leaker", "status": "R", "mem_rss_mb": mb, "cpu_percent": 0}]
        eng.evaluate(t0 + m * 60, [], procs, [])
    open_a = db.get_open_alert("memory_climbing", f"pid:{pid}")
    assert open_a is not None
    assert open_a.details.get("pid") == pid


def test_zombie_30s(engine):
    eng, db = engine
    t0 = time.time()
    z = [{"pid": 77, "name": "bad", "status": "Z", "mem_rss_mb": 1, "cpu_percent": 0}]
    eng.evaluate(t0, [], z, [])
    eng.evaluate(t0 + 31, [], z, [])
    assert db.get_open_alert("zombie", "pid:77") is not None


def test_no_duplicate_alerts(engine):
    eng, db = engine
    t = time.time()
    rows = _sys_row("mem.ram_percent", 95.0)
    for _ in range(5):
        eng.evaluate(t, rows, [], [])
    open_alerts = db.list_alerts(open_only=True)
    assert len([a for a in open_alerts if a.rule == "ram_high"]) == 1