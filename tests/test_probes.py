import time

from box_health_agent.probes.network_probes import run_network_probes


def test_probes_all_timeout_bounded():
    cfg = {
        "network_probes": {
            "dns_host": "127.0.0.1",
            "tcp_host": "127.0.0.1",
            "tcp_port": 1,
            "internet_ping_host": "127.0.0.1",
            "api_endpoint": {"url": "http://127.0.0.1:1/", "timeout_sec": 1},
            "timeouts": {"dns_sec": 0.2, "tcp_sec": 0.2, "ping_sec": 0.2},
        }
    }
    start = time.perf_counter()
    results = run_network_probes(cfg)
    elapsed = time.perf_counter() - start
    assert elapsed < 10
    assert {r["probe"] for r in results} >= {"dns", "tcp_connect", "api_endpoint"}