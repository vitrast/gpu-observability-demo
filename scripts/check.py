#!/usr/bin/env python3
"""Check the running stack end to end. Standard library only.

    python scripts/check.py              # is every component up and wired together? (~10 s)
    python scripts/check.py cooling      # inject a scenario, wait for the expected pages, reset
    python scripts/check.py all          # stack check, then every scenario in turn (~20 min)

A scenario passes when every alert the README says should page is paging and every alert that
should be inhibited is inhibited. Extra pages are reported as warnings. Exit code 0 = all passed.
"""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SIM = "http://localhost:9100"
VMAGENT = "http://localhost:8429"
VM = "http://localhost:8428"
VMALERT = "http://localhost:8880"
AM = "http://localhost:9093"
VLOGS = "http://localhost:9428"
GRAFANA = "http://localhost:3000"

DASHBOARDS = pathlib.Path(__file__).resolve().parent.parent / "grafana" / "dashboards"

# name: (injection path, same as scripts/scenario.sh; alerts that must page; alerts that must be inhibited)
SCENARIOS = {
    "power": ("/scenario/rpp_trip?rpp=rpp-r1-b",
              {"RPPDown", "RackPowerLost", "TenantJobStalled", "TenantSLOBurnFast"}, set()),
    "ups": ("/scenario/ups_on_battery?ups=ups-b",
            {"UPSOnBattery", "UPSOutputLost", "RackPowerLost", "TenantJobStalled"}, set()),
    "cooling": ("/scenario/cooling_failure?row=r2",
                {"CRAHDegraded", "RackInletTemperatureHigh"}, {"GPUThermalThrottling"}),
    "fabric": ("/scenario/fabric_link?host=gpu-r1-03-n01&gpu=3",
               {"FabricLinkFlapping"}, {"FabricLinkErrorsRising"}),
}
SCENARIO_TIMEOUT = 600   # seconds; SPEED=3 in docker-compose.yml makes incidents unfold in minutes
POLL = 10

failures = []


def get(url, raw=False):
    """GET and decode JSON; HTTP errors come back as {"error": ...} instead of raising."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        return {"status": "error", "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    return body if raw or body.lstrip()[:1] not in "[{" else json.loads(body)


def report(ok, what, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(what)


def check_services():
    for name, url in {
        "fleet-sim": f"{SIM}/status",
        "vmagent": f"{VMAGENT}/health",
        "victoriametrics": f"{VM}/health",
        "vmalert": f"{VMALERT}/health",
        "alertmanager": f"{AM}/-/healthy",
        "victorialogs": f"{VLOGS}/health",
        "grafana": f"{GRAFANA}/api/health",
    }.items():
        try:
            get(url)
            report(True, f"{name} is up")
        except OSError as e:
            report(False, f"{name} is up", str(e))
    return not failures


def check_collection():
    targets = get(f"{VMAGENT}/api/v1/targets")["data"]["activeTargets"]
    down = [f"{t['labels']['job']}/{t['labels']['instance']}: {t.get('lastError', '')[:80]}"
            for t in targets if t["health"] != "up"]
    report(not down, f"vmagent scrapes {len(targets)} targets", "; ".join(down))

    tsdb = get(f"{VM}/api/v1/status/tsdb")["data"]
    print(f"INFO  VictoriaMetrics holds {tsdb['totalSeries']} series")

    rows = get(f"{VLOGS}/select/logsql/query?query=" + urllib.parse.quote("_time:5m | stats count() as n"),
               raw=True)
    n = int(json.loads(rows.splitlines()[0])["n"]) if rows.strip() else 0
    report(n > 0, "simulator events reach VictoriaLogs", f"{n} events in the last 5 min")


def check_rules():
    rules = [r for g in get(f"{VMALERT}/api/v1/rules")["data"]["groups"] for r in g["rules"]]
    broken = [f"{r['name']}: {r['lastError'][:120]}" for r in rules if r.get("lastError")]
    alerting = sum(r["type"] == "alerting" for r in rules)
    report(not broken, f"vmalert evaluates {alerting} alerting + {len(rules) - alerting} recording rules",
           "; ".join(broken))


def check_grafana():
    for uid in ("vm", "vlogs"):
        health = get(f"{GRAFANA}/api/datasources/uid/{uid}/health")
        report(health.get("status") == "OK", f"Grafana datasource '{uid}' is healthy",
               health.get("message", ""))
    found = {d["uid"] for d in get(f"{GRAFANA}/api/search?tag=neocloud")}
    expected = {p.stem for p in DASHBOARDS.glob("*.json")}
    report(found == expected, f"Grafana loaded {len(found)}/{len(expected)} dashboards",
           ", ".join(sorted(expected - found)))


def check_panels():
    """Run every VictoriaMetrics panel query once, the way Grafana would with default variables."""
    errors, empty, total = [], [], 0
    for path in sorted(DASHBOARDS.glob("*.json")):
        board = json.loads(path.read_text(encoding="utf-8"))
        for panel in board["panels"]:
            for target in panel.get("targets", []):
                if target.get("datasource", {}).get("uid") != "vm":
                    continue
                total += 1
                expr = (target["expr"].replace("${rack}", ".*").replace("$rack", ".*")
                        .replace("$__rate_interval", "1m").replace("$__interval", "15s")
                        .replace("$__range", "30m"))
                res = get(f"{VM}/api/v1/query?query=" + urllib.parse.quote(expr))
                where = f"{path.stem} / {panel.get('title', '?')}"
                if res.get("status") != "success":
                    errors.append(f"{where}: {res.get('error', '')[:150]}")
                elif not res["data"]["result"]:
                    empty.append(where)
    report(not errors, f"{total} panel queries run without errors", "; ".join(errors))
    if empty:
        print(f"INFO  {len(empty)} queries return nothing right now (normal at rest for incident panels):")
        for where in empty:
            print(f"        {where}")


def alert_state():
    """Firing alerts in vmalert, then what Alertmanager turned into pages and what it inhibited."""
    firing = [a for a in get(f"{VMALERT}/api/v1/alerts")["data"]["alerts"]
              if a["state"] == "firing" and a["labels"]["alertname"] != "Watchdog"]
    alerts = get(f"{AM}/api/v2/alerts")
    paged, pages, inhibited = set(), set(), set()
    for a in alerts:
        name, labels = a["labels"]["alertname"], a["labels"]
        receivers = {r["name"] for r in a["receivers"]}
        if a["status"]["inhibitedBy"]:
            inhibited.add(name)
        elif a["status"]["state"] == "active" and any(r.endswith("-pager") for r in receivers):
            paged.add(name)
            pages.add((name, labels.get("site"), labels.get("row")))   # = route group_by
    return len(firing), paged, len(pages), inhibited


def check_alerting_at_rest():
    names = {a["labels"]["alertname"] for a in get(f"{AM}/api/v2/alerts")}
    report("Watchdog" in names, "alerting path alive (Watchdog reaches Alertmanager)")
    _, paged, _, _ = alert_state()
    report(not paged, "nobody is paged at rest", ", ".join(sorted(paged)))


def run_scenario(name):
    path, must_page, must_inhibit = SCENARIOS[name]
    print(f"\n== scenario {name}: inject {path}")
    get(f"{SIM}/scenario/reset")
    deadline = time.time() + 180
    while alert_state()[1] and time.time() < deadline:   # let pages from a previous run resolve
        time.sleep(POLL)
    get(SIM + path)
    start, last = time.time(), None
    try:
        while True:
            fires, paged, pages, inhibited = alert_state()
            elapsed = int(time.time() - start)
            state = (fires, frozenset(paged), frozenset(inhibited))
            if state != last:
                print(f"  t+{elapsed:>3}s  firing {fires:>2}  paging: {', '.join(sorted(paged)) or '-'}"
                      f"  |  inhibited: {', '.join(sorted(inhibited)) or '-'}")
                last = state
            if must_page <= paged and must_inhibit <= inhibited or elapsed > SCENARIO_TIMEOUT:
                break
            time.sleep(POLL)
    finally:
        get(f"{SIM}/scenario/reset")
    report(must_page <= paged, f"{name}: expected alerts page", ", ".join(sorted(must_page - paged)))
    if must_inhibit:
        report(must_inhibit <= inhibited, f"{name}: consequences inhibited",
               ", ".join(sorted(must_inhibit - inhibited)))
    if paged - must_page:
        print(f"WARN  {name}: also paging {', '.join(sorted(paged - must_page))}")
    print(f"INFO  {name}: {fires} alerts firing, {pages} pages sent, after {elapsed}s")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    args = sys.argv[1:] or ["stack"]
    if args == ["all"]:
        args = ["stack", *SCENARIOS]
    unknown = [a for a in args if a != "stack" and a not in SCENARIOS]
    if unknown:
        sys.exit(f"unknown: {', '.join(unknown)}; use stack, all, or one of {', '.join(SCENARIOS)}")
    if not check_services():
        sys.exit("stack is not up: docker compose up -d --build")
    for arg in args:
        if arg == "stack":
            check_collection()
            check_rules()
            check_grafana()
            check_panels()
            check_alerting_at_rest()
        else:
            run_scenario(arg)
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
