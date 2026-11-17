# Box Health Agent

Python health agent for Zero Forg edge boxes: samples the host every 5s, network probes every 15s, SQLite history, deterministic alerts, HTTP API, and HTML dashboard.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py --config config.yaml
```

Open `http://localhost:8080/` for the dashboard.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | `ok` / `warn` / `critical` plus open alerts |
| `GET /metrics?name=&since=` | Metric time series |
| `GET /alerts?open=true` | Open or closed alerts |
| `GET /processes?sort=cpu` | Latest process snapshot |

## Alert rules

| Rule | Threshold (configurable) |
|------|--------------------------|
| `cpu_high` | Total CPU > 85% for **60s continuous** (12×5s samples) |
| `ram_high` | RAM > 90% |
| `disk_high` | Any mount > 90% |
| `network_lost` | Gateway or internet probe fails **3 times in a row** |
| `memory_climbing` | **9 of last 10** one-minute RAM samples rise; **≥ 50 MB** total delta |
| `process_died` | Watch-list process (see `config.yaml`) no longer running |
| `zombie` | Process in `Z` state > 30s |

Alerts open once per condition, stay open while active, close when cleared. History retained **7 days** then purged.

## Tests

```bash
pytest -q
```