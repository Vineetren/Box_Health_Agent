"""Entry point: python main.py [--config config.yaml]"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from box_health_agent.agent import HealthAgent
from box_health_agent.api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Box Health Agent")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    args = parser.parse_args()
    config = Path(args.config)
    agent = HealthAgent(config)
    agent.start()
    app = create_app(agent)
    host = agent.cfg.get("server", {}).get("host", "0.0.0.0")
    port = int(agent.cfg.get("server", {}).get("port", 8080))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()