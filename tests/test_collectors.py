import time

from box_health_agent.collectors.network import collect_network_metrics
from box_health_agent.collectors.processes import collect_process_metrics
from box_health_agent.collectors.system import collect_system_metrics


def test_collect_system_returns_core_metrics():
    t = time.time()
    rows = collect_system_metrics(t)
    names = {n for _, n, _, _ in rows}
    assert "cpu.total_percent" in names
    assert "mem.ram_percent" in names
    assert "uptime_sec" in names


def test_collect_network_second_sample_has_rates():
    t = time.time()
    collect_network_metrics(t)
    rows = collect_network_metrics(t + 1)
    names = {n for _, n, _, _ in rows}
    assert "tcp.connections" in names or any("net." in n for n in names)


def test_collect_processes_non_empty():
    procs = collect_process_metrics()
    assert len(procs) > 0
    assert "pid" in procs[0]