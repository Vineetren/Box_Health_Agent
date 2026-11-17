import sys
from unittest.mock import patch

from box_health_agent.probes.gateway import detect_default_gateway, resolve_gateway


def test_resolve_gateway_explicit_config():
    gw, src = resolve_gateway({"network_probes": {"gateway_host": "10.0.0.1"}})
    assert gw == "10.0.0.1"
    assert src == "config"


def test_resolve_gateway_empty_uses_detect():
    with patch("box_health_agent.probes.gateway.detect_default_gateway", return_value=("192.168.88.1", "windows_route")):
        gw, src = resolve_gateway({"network_probes": {"gateway_host": ""}})
        assert gw == "192.168.88.1"
        assert src == "windows_route"


def test_windows_ipconfig_parse():
    sample = """
Ethernet adapter Ethernet:
   Default Gateway . . . . . . . . . : 192.168.1.254
"""
    with patch.object(sys, "platform", "win32"):
        with patch("box_health_agent.probes.gateway.subprocess.run") as run:
            run.return_value.stdout = sample
            run.return_value.returncode = 0
            gw, src = detect_default_gateway()
    assert gw == "192.168.1.254"
    assert src == "windows_route"