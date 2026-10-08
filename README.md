# GPU observability demo

Docker Compose demo with simulated power, cooling, network, GPU and tenant metrics.
The simulated site has 2 rows, 8 racks, 32 nodes and 256 GPUs.

Stack: fleet-sim, vmagent, VictoriaMetrics, vmalert, Alertmanager, VictoriaLogs and Grafana.

## Run

Requires Docker with Compose.

```sh
docker compose up -d --build
```

| Service | URL |
| --- | --- |
| Grafana | http://localhost:3000 |
| VictoriaMetrics | http://localhost:8428/vmui |
| vmalert | http://localhost:8880 |
| Alertmanager | http://localhost:9093 |
| VictoriaLogs | http://localhost:9428/select/vmui |
| Simulator | http://localhost:9100/status |

Topology metrics update every 60 seconds.

## Scenarios

Run in Bash (Git Bash or WSL on Windows):

```sh
bash scripts/scenario.sh power    # power panel trip
bash scripts/scenario.sh ups      # UPS battery discharge
bash scripts/scenario.sh cooling  # cooling failure
bash scripts/scenario.sh fabric   # link degradation
bash scripts/scenario.sh reset
```

## Checks

Requires Python 3.

```sh
python scripts/check.py          # services and dashboard queries
python scripts/check.py all      # all scenarios, about 20 minutes
```

Dashboard source: `grafana/build_dashboards.py`. Rebuild with `python grafana/build_dashboards.py`.

Alertmanager has no external notification integrations. Check alerts in its UI.
