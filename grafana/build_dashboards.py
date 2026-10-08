"""
Dashboards as code: generates the five Grafana dashboards into grafana/dashboards/.
Run:  python grafana/build_dashboards.py
Dashboards query derived metrics (recording rules) wherever one exists, so renaming a raw
exporter metric or adding a second GPU vendor does not break them. Every panel carries a
description (the (i) next to its title): what it shows and how to read it.
"""
import json
import pathlib

VM = {"type": "prometheus", "uid": "vm"}
VL = {"type": "victoriametrics-logs-datasource", "uid": "vlogs"}
OUT = pathlib.Path(__file__).parent / "dashboards"


class Board:
    def __init__(self, uid, title, desc, rack_var=False):
        self.uid, self.title, self.desc, self.panels, self.next_id, self.rack_var = uid, title, desc, [], 1, rack_var

    def _panel(self, ptype, title, gp, targets, desc, ds=VM, field=None, options=None, extra=None):
        p = {"id": self.next_id, "type": ptype, "title": title, "description": desc, "datasource": ds,
             "gridPos": dict(zip("xywh", gp)), "targets": targets,
             "fieldConfig": {"defaults": field or {}, "overrides": []}, "options": options or {}}
        p.update(extra or {})
        self.next_id += 1
        self.panels.append(p)
        return p

    @staticmethod
    def q(expr, legend="", ref="A", instant=False, fmt="time_series"):
        return {"datasource": VM, "expr": expr, "legendFormat": legend, "refId": ref,
                "instant": instant, "range": not instant, "format": fmt}

    def stat(self, title, gp, expr, desc, legend="", unit=None, steps=None, mappings=None, links=None, novalue="0"):
        field = {"unit": unit or "none", "mappings": mappings or [], "links": links or [],
                 "thresholds": {"mode": "absolute", "steps": steps or [{"color": "green", "value": None}]},
                 "color": {"mode": "thresholds"}, "noValue": novalue}
        # without a legend Grafana would print the query text as the series name
        return self._panel("stat", title, gp, [self.q(expr, legend, instant=True)], desc, field=field,
                           options={"colorMode": "background", "graphMode": "none",
                                    "textMode": "value_and_name" if legend else "value",
                                    "justifyMode": "center", "reduceOptions": {"calcs": ["lastNotNull"]}})

    def ts(self, title, gp, queries, desc, unit=None, steps=None, line=False, minv=None, maxv=None, softmin=None):
        custom = {"lineWidth": 1, "fillOpacity": 8, "showPoints": "never", "spanNulls": False}
        if line:
            custom["thresholdsStyle"] = {"mode": "line"}
        if softmin is not None:
            custom["axisSoftMin"] = softmin
        field = {"unit": unit or "none", "custom": custom, "color": {"mode": "palette-classic"},
                 "thresholds": {"mode": "absolute", "steps": steps or [{"color": "green", "value": None}]}}
        if minv is not None:
            field["min"] = minv
        if maxv is not None:
            field["max"] = maxv
        targets = [self.q(e, l, ref=chr(65 + i)) for i, (e, l) in enumerate(queries)]
        return self._panel("timeseries", title, gp, targets, desc, field=field,
                           options={"legend": {"displayMode": "list", "placement": "bottom"},
                                    "tooltip": {"mode": "multi", "sort": "desc"}})

    def table(self, title, gp, expr, desc, hide=()):
        exclude = {k: True for k in ("Time", "Value", "__name__", "job", "instance", "alertstate", *hide)}
        return self._panel("table", title, gp, [self.q(expr, instant=True, fmt="table")], desc,
                           options={"showHeader": True, "cellHeight": "sm"},
                           extra={"transformations": [{"id": "organize", "options": {"excludeByName": exclude}}]})

    def bargauge(self, title, gp, queries, desc, unit=None, steps=None, minv=None, maxv=None):
        field = {"unit": unit or "none", "color": {"mode": "thresholds"},
                 "thresholds": {"mode": "absolute", "steps": steps or [{"color": "green", "value": None}]}}
        if minv is not None:
            field["min"] = minv
        if maxv is not None:
            field["max"] = maxv
        targets = [self.q(e, l, ref=chr(65 + i), instant=True) for i, (e, l) in enumerate(queries)]
        # "basic" paints the bar in the colour of the current value; "gradient" would paint every
        # threshold colour along the bar and make a healthy 100% look red
        return self._panel("bargauge", title, gp, targets, desc, field=field,
                           options={"orientation": "horizontal", "displayMode": "basic",
                                    "reduceOptions": {"calcs": ["lastNotNull"]}, "showUnfilled": True})

    def timeline(self, title, gp, expr, legend, desc):
        field = {"color": {"mode": "thresholds"},
                 "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]},
                 "mappings": [{"type": "value", "options": {"0": {"text": "OK", "index": 0},
                                                            "1": {"text": "PROBLEM", "index": 1}}}]}
        return self._panel("state-timeline", title, gp, [self.q(expr, legend)], desc, field=field,
                           options={"showValue": "never", "rowHeight": 0.85, "mergeValues": True,
                                    "legend": {"showLegend": False}})

    def logs(self, title, gp, expr, desc):
        return self._panel("logs", title, gp, [{"datasource": VL, "expr": expr, "refId": "A"}], desc, ds=VL,
                           options={"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                                    "enableLogDetails": True})

    def text(self, title, gp, markdown):
        return self._panel("text", title, gp, [], "", ds=None, options={"mode": "markdown", "content": markdown})

    def graph(self, title, gp, nodes, edges, desc):
        """Node graph from two instant queries; refIds 'nodes'/'edges' tell the panel which frame is which.
        Node value = fixedX, label fixedY = row of the diagram, edge value = thickness.
        The panel looks fields up by name. Organize's rename only sets a display name, so the value
        columns are copied into properly named fields with "Add field from calculation"."""
        targets = [self.q(nodes, ref="nodes", instant=True, fmt="table"),
                   self.q(edges, ref="edges", instant=True, fmt="table")]
        copy_as = lambda src, name: {"id": "calculateField", "options": {
            "mode": "reduceRow", "reduce": {"reducer": "sum", "include": [src]}, "alias": name, "replaceFields": False}}
        return self._panel("nodeGraph", title, gp, targets, desc,
                        options={"nodes": {"mainStatUnit": "none"}, "edges": {"mainStatUnit": "none"},
                                 "zoomMode": "cooperative"},
                        extra={"transformations": [
                            {"id": "organize", "options": {"excludeByName": {"Time": True}}},
                            copy_as("Value #nodes", "fixedX"), copy_as("Value #edges", "thickness"),
                            {"id": "convertFieldType", "options": {
                                "conversions": [{"targetField": "fixedY", "destinationType": "number"}], "fields": {}}}]})

    def save(self):
        templating = []
        if self.rack_var:
            templating.append({
                "type": "query", "name": "rack", "label": "Rack", "datasource": VM,
                "query": {"query": "label_values(node_topology_info, rack)", "refId": "rack"},
                "definition": "label_values(node_topology_info, rack)",
                "includeAll": True, "multi": True, "allValue": ".*", "refresh": 2, "sort": 1,
                "current": {"selected": True, "text": ["All"], "value": ["$__all"]}})
        board = {
            "uid": self.uid, "title": self.title, "description": self.desc, "tags": ["gpu-observability"],
            "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True, "refresh": "10s",
            "time": {"from": "now-30m", "to": "now"}, "panels": self.panels,
            "templating": {"list": templating},
            "links": [{"type": "dashboards", "tags": ["gpu-observability"], "asDropdown": False,
                       "title": "Layers", "keepTime": True, "includeVars": False}],
        }
        OUT.mkdir(exist_ok=True)
        (OUT / f"{self.uid}.json").write_text(json.dumps(board, indent=2, ensure_ascii=False),
                                              encoding="utf-8", newline="\n")
        return board


G, Y, O, R = "green", "yellow", "orange", "red"
st = lambda *pairs: [{"color": c, "value": v} for c, v in pairs]
vmap = lambda d: [{"type": "value", "options": {str(k): {"text": t, "color": c, "index": i}
                                                for i, (k, (t, c)) in enumerate(d.items())}}]
RACK_LINK = [{"title": "Rack ${__field.labels.rack}: facilities", "url": "/d/02-facilities?var-rack=${__field.labels.rack}&${__url_time_range}"},
             {"title": "Rack ${__field.labels.rack}: GPUs", "url": "/d/04-compute?var-rack=${__field.labels.rack}&${__url_time_range}"}]


# ---------------------------------------------------------------- dependency graph (node graph panel)
# The graph is computed from the *_info join tables at query time (MetricsQL label functions), so
# it follows the topology and never adds labels to GPU series. Each node kind is a list of
# (condition, status, colour, reason), most severe first: `or on(key)` keeps the first that matches.
# Layout is a fixed one-line diagram, top to bottom: UPS, row power panels and CRAHs, racks, tenants.
# A rack's x comes from its name (r1-03 = row 1, position 3), as rack names encode floor position;
# a tenant sits under the average x of its racks. Fixed positions keep nodes still while colours change.
HEX = {G: "#73BF69", Y: "#FADE2A", O: "#FF9830", R: "#F2495C", "grey": "#6E7480"}
STEP, RACKS_PER_ROW = 120, 4          # px between neighbouring racks; racks per row in topology.yaml
ROW = STEP * RACKS_PER_ROW

BAD_LINKS = ('((rate(fabric_port_symbol_errors_total[1m]) > 5) or (increase(fabric_port_link_downed_total[2m]) > 0)'
             ' or ((fabric_port_speed_gbps > 0) < 400)) * on(switch, port) group_left(rack) fabric_link_info{peer_type="host"}')
# raw, not the rack:power_feeds_live:count rule: power states must be as fresh as the PDU load they are compared with
FEEDS_UP = 'sum by (rack) (facility_pdu_input_ok * (1 - facility_pdu_breaker_tripped))'
FEED_LIVE = '(facility_pdu_input_ok * (1 - facility_pdu_breaker_tripped)) * on(row, feed) group_left(rpp) facility_rpp_info'
FEED_LOAD = 'facility_pdu_breaker_load_ratio * on(row, feed) group_left(rpp) facility_rpp_info'
COOLS = 'count by (crah, rack) (node_topology_info)'
SERVES = 'count by (rack, tenant) (node_tenant_info * on(Hostname) group_left(rack) node_topology_info)'

ROW_NUM = 'label_value(label_replace({}, "rn", "$1", "row", "r([0-9]+)"), "rn")'
RACK_NUM = ('label_replace(label_replace(max by (rack) (node_topology_info), "rn", "$1", "rack", "r([0-9]+)-.*"),'
            ' "kn", "$1", "rack", "r[0-9]+-([0-9]+)")')
X_RACK = f'(label_value({RACK_NUM}, "rn") - 1) * {ROW} + (label_value({RACK_NUM}, "kn") - 1) * {STEP} - {ROW - STEP // 2}'
# a row's panels sit over the middle of the row: feed A left of centre, feed B right of it
RPP_A, RPP_B = ROW_NUM.format('facility_rpp_info{feed="A"}'), ROW_NUM.format('facility_rpp_info{feed="B"}')
X_RPP = f'union({RPP_A} * {ROW} - {ROW + ROW // 2 + 3 * STEP // 4}, {RPP_B} * {ROW} - {ROW + ROW // 2 - 3 * STEP // 4})'
X_CRAH = ROW_NUM.format("max by (crah, row) (facility_crah_cooling_capacity_ratio)") + f" * {2 * ROW} - {3 * ROW}"
X_UPS = ('union(max by (ups) (facility_ups_output_ok{feed="A"}) * 0 - 90,'
         ' max by (ups) (facility_ups_output_ok{feed="B"}) * 0 + 90)')
X_TENANT = f'avg by (tenant) (({X_RACK}) + on(rack) group_left(tenant) ({SERVES} * 0))'


def _first_match(key, branches, labels):
    return f" or on({key}) ".join(
        "label_set(" + expr + "".join(f', "{k}", "{v}"' for k, v in zip(labels, values)) + ")"
        for expr, *values in branches)


def _status(key, branches):
    return _first_match(key, [(e, s, HEX[c], why) for e, s, c, why in branches], ("mainstat", "color", "detail__why"))


def _node_kind(key, subtitle, y, x, branches):
    placed = f"({_status(key, branches)}) * 0 + on({key}) group_left() ({x})"
    return f'label_set(label_copy({placed}, "{key}", "id", "{key}", "title"), "subtitle", "{subtitle}", "fixedY", "{y}")'


def _edge_kind(key, source, target, branches):
    body = _first_match(key, [(e, s, HEX[c]) for e, s, c in branches], ("mainstat", "color"))
    return f'label_copy({body}, "{source}", "source", "{target}", "target")'


GRAPH_NODES = ('label_keep(union({}), "id", "title", "subtitle", "mainstat", "color", "detail__why", "fixedY")'
               ).format(", ".join([
    _node_kind("ups", "UPS", -170, X_UPS, [
        ("max by (ups) (facility_ups_output_ok) == 0", "NO OUTPUT", R, "UPS output is off: every rack on this feed lost it"),
        ("max by (ups) (facility_ups_on_battery) == 1", "ON BATTERY", O, "Utility input lost: the battery is draining"),
        ("max by (ups) (facility_ups_output_ok)", "OK", G, "On utility power")]),
    _node_kind("rpp", "row power panel", -60, X_RPP, [
        ("max by (rpp) (facility_rpp_output_ok) == 0", "NO OUTPUT", R, "Panel has no output: every rack in the row lost this feed"),
        ("max by (rpp) (facility_rpp_output_ok)", "OK", G, "Output OK")]),
    _node_kind("crah", "CRAH (cooling)", -60, X_CRAH, [
        ("max by (crah) (facility_crah_cooling_capacity_ratio) < 0.9", "DEGRADED", R, "Cooling capacity below 90%: the racks of this row heat up"),
        ("max by (crah) (facility_crah_cooling_capacity_ratio)", "OK", G, "Full cooling capacity")]),
    _node_kind("rack", "rack", 50, X_RACK, [
        (f"{FEEDS_UP} == 0", "DARK", R, "No live power feed"),
        ("max by (rack) (rack:nodes_notready:count) > 0", "NODES DOWN", R, "At least one server is NotReady"),
        ("max by (rack) (rack:gpu_throttled:count) > 0", "THROTTLING", O, "GPUs are thermally throttling: jobs run slower"),
        ("max by (rack) (rack:inlet_temperature_celsius:max) > 27", "HOT AISLE", O, "Inlet air above the ASHRAE recommended 27 C"),
        (f"count by (rack) ({BAD_LINKS})", "LINK ERRORS", O, "A fabric link to a server here is erroring, flapping or below rated speed"),
        (f"{FEEDS_UP} == 1", "ONE FEED", O, "Running on one feed: the next failure takes the rack down"),
        ("max by (rack) (facility_pdu_breaker_load_ratio) > 0.5", "N+1 RISK", Y, "Each feed carries more than 50% of its breaker: losing one overloads the other"),
        ("max by (rack) (node_topology_info)", "OK", G, "Both feeds live, cool, all servers Ready")]),
    # tenants: same pattern, plus the SLA tier from the tenant registry in the subtitle
    'label_set(label_join(label_set(label_copy(((' + _status("tenant", [
        ("max by (tenant) (tenant_job_up) == 0", "STALLED", R, "Training job is down: it lost servers or GPUs"),
        ("min by (tenant) (tenant:gpu_availability:ratio) < 0.995", "GPUS LOST", R, "GPU availability is below the 99.5% SLO"),
        ("max by (tenant) (tenant_job_step_duration_seconds) > 1.45", "SLOW", O, "Training step time above 1.45 s (baseline about 1.2 s)"),
        ("max by (tenant) (tenant_info)", "OK", G, "Job running at normal speed")])
    + f') * 0 + on(tenant) group_left() ({X_TENANT})) * on(tenant) group_left(sla) tenant_info, "tenant", "id", "tenant", "title"),'
      ' "sla_prefix", "tenant · SLA "), "subtitle", "", "sla_prefix", "sla"), "fixedY", "160")',
]))

# edge value = thickness: 1 healthy, 2 precursor, 3 degraded, 4 dead
GRAPH_EDGES = ('label_keep(label_join(union({}), "id", "->", "source", "target"), "id", "source", "target", "color", "mainstat")'
               .format(", ".join([
                   _edge_kind("rpp", "ups", "rpp", [
                       ("facility_rpp_info * on(ups) group_left() (max by (ups) (facility_ups_output_ok) == 0) * 0 + 4", "no power", R),
                       ("facility_rpp_info * on(ups) group_left() (max by (ups) (facility_ups_on_battery) == 1) * 0 + 3", "on battery", O),
                       ("facility_rpp_info * 0 + 1", "power OK", "grey")]),
                   _edge_kind("pdu", "rpp", "rack", [
                       (f"({FEED_LIVE} == 0) * 0 + 4", "feed dead", R),
                       (f"({FEED_LOAD} > 0.5) * 0 + 2", "feed above 50%: no N+1", Y),
                       (f"{FEED_LIVE} * 0 + 1", "feed OK", "grey")]),
                   _edge_kind("rack", "crah", "rack", [
                       (f"{COOLS} * on(crah) group_left() (max by (crah) (facility_crah_cooling_capacity_ratio) < 0.9) * 0 + 3",
                        "cooling degraded", R),
                       (f"{COOLS} * 0 + 1", "cooling OK", "grey")]),
                   _edge_kind("rack", "rack", "tenant", [
                       (f"{SERVES} * on(rack) group_left() ((max by (rack) (rack:nodes_notready:count) > 0)"
                        f" or ({FEEDS_UP} == 0)) * 0 + 4", "GPUs lost", R),
                       (f"{SERVES} * on(rack) group_left() ((max by (rack) (rack:gpu_throttled:count) > 0)"
                        f" or count by (rack) ({BAD_LINKS})) * 0 + 3", "slowed down", O),
                       (f"{SERVES} * 0 + 1", "serving", "grey")]),
               ])))

READING_ORDER = """**The first two minutes**

1. **Top row**: is anyone paged, how much was suppressed as a consequence, how many tenant GPUs are gone.
2. **Graph**, top down: the first red or orange node whose parents are all green is the cause; everything coloured below it is the blast radius. All power and cooling green, one rack orange: look at the fabric dashboard. **Yellow** is a precursor: working, but one failure away from red.
3. **Timeline** below: which layer turned red first.
4. **Alerts table**: owner and runbook of everything firing.

Then drill down: click a rack under *Where*."""


def incident():
    b = Board("01-incident", "01 · Incident response — first 2 minutes",
              "What is burning, where it started, which racks and tenants are hit: one screen for the first two minutes.")
    b.stat("Pages firing", (0, 0, 4, 4), 'count(ALERTS{alertstate="firing", severity="page"}) or vector(0)',
           "Page-severity alerts firing in vmalert, before Alertmanager groups and inhibits them. 0 is the normal state.",
           steps=st((G, None), (R, 1)))
    b.stat("Notifications sent", (4, 0, 4, 4), 'sum(alertmanager_alerts{state="active"}) or vector(0)',
           "Alerts Alertmanager is routing to receivers now. At rest this is 1: the Watchdog heartbeat, a dead man's switch "
           "proving the alerting path works.", steps=st((G, None), (O, 2)))
    b.stat("Suppressed as consequences", (8, 0, 4, 4), 'sum(alertmanager_alerts{state="suppressed"}) or vector(0)',
           "Alerts inhibited along the dependency chains (UPS → RPP → rack → node → GPU; CRAH → throttling; flapping link → "
           "its error counters). Many here and few notifications = people get the cause, not the noise.",
           steps=st(("blue", None)))
    b.stat("Tenants burning SLO", (12, 0, 4, 4), "count(tenant:slo_burn_rate:1m > 1) or vector(0)",
           "Tenants spending their GPU-availability error budget faster than it accrues (burn rate > 1 over the last minute). "
           "The page fires at 14.4x on two windows.", steps=st((G, None), (R, 1)))
    b.stat("GPUs unavailable to tenants", (16, 0, 4, 4),
           "sum(tenant:gpus_allocated:count) - sum(tenant:gpus_healthy:count)",
           "GPUs allocated to tenants minus GPUs that are usable (node Ready, no XID 79): the customer-facing blast radius.",
           steps=st((G, None), (R, 1)))
    b.stat("Nodes with no GPU telemetry", (20, 0, 4, 4),
           "count(node_topology_info unless on(Hostname) DCGM_FI_DEV_GPU_TEMP) or vector(0)",
           "'Unknown' is not 'healthy': servers in the inventory whose GPU exporter went silent (unpowered, crashed, "
           "unreachable). A silent server must never look green.", steps=st((G, None), (O, 1)))
    b.graph("Dependency graph — the cause and the blast radius", (0, 4, 18, 15), GRAPH_NODES, GRAPH_EDGES,
            "One dependency graph, live: power (UPS → row power panel → rack feed), cooling (CRAH → rack) and placement "
            "(rack → tenant). Colour is the state now. The first red or orange node whose parents are all green is the cause; "
            "everything coloured below it is the blast radius. Yellow is a precursor. Click a node for the reason. Computed "
            "at query time from the *_info join-table metrics: no topology labels on GPU series.")
    b.text("How to read this dashboard", (18, 4, 6, 15), READING_ORDER)
    b.timeline("Where did it start — status by layer", (0, 19, 24, 7), "layer:status", "{{layer}}",
               "One line per layer, red while any signal of that layer is out of range. The first line to turn red is "
               "usually where the incident started: facilities first means the building, compute alone means the GPU.")
    b.table("What is burning — firing alerts", (0, 26, 24, 7), 'ALERTS{alertstate="firing", alertname!="Watchdog"}',
            "Every firing alert with owner (team), severity and location labels. Inhibited alerts are listed too; "
            "Alertmanager decides who is actually paged.")
    b.stat("Where — racks by worst signal (click for drill-down)", (0, 33, 12, 6), "rack:status",
           "Number of hurting signals per rack: hot inlet, throttling GPUs, one feed, no power, NotReady servers. "
           "Click a rack to open its facilities or GPU view.",
           legend="{{rack}}", steps=st((G, None), (O, 1), (R, 3)), links=RACK_LINK)
    b.bargauge("Who is hit — tenant GPU availability", (12, 33, 12, 6),
               [("tenant:gpu_availability:ratio", "{{tenant}}")],
               "Share of each tenant's allocated GPUs that are usable now. Green at or above the 99.5% SLO target.",
               unit="percentunit", steps=st((R, None), (O, 0.95), (G, 0.995)), minv=0, maxv=1)
    b.table("Walk up the power chain — NotReady nodes and their upstream devices", (0, 39, 12, 8),
            'label_replace(kube_node_status_ready == 0, "Hostname", "$1", "node", "(.*)") '
            '* on(Hostname) group_left(rack, pdu_a, rpp_a, ups_a, pdu_b, rpp_b, ups_b) node_power_info',
            "Each NotReady server with both of its power paths (PDU → RPP → UPS for feeds A and B) from the topology "
            "join. A device shared by every row is the common cause.", hide=("node",))
    b.stat("Upstream power devices with no output", (12, 39, 6, 8),
           "((facility_rpp_output_ok == bool 0) or (facility_ups_output_ok == bool 0)) == 1",
           "UPS units and row power panels whose output is off. Any entry here is a facilities incident for a whole "
           "feed or row.", legend="{{rpp}}{{ups}}", steps=st((G, None), (R, 1)),
           mappings=vmap({1: ("NO OUTPUT", R)}), novalue="All have output")
    b.bargauge("Throttling GPUs by CRAH (cooling chain)", (18, 39, 6, 8),
               [("count by (crah) ((DCGM_FI_DEV_CLOCK_THROTTLE_REASONS > 0) * on(Hostname) group_left(crah) node_topology_info)"
                 " or max by (crah) (node_topology_info) * 0", "{{crah}}")],
               "Throttling GPUs grouped by the CRAH that cools them. Many under one CRAH = a cooling problem, "
               "not a GPU problem.", steps=st((G, None), (O, 1)), minv=0)
    b.ts("Tenant training step time", (0, 47, 12, 7), [("tenant_job_step_duration_seconds", "{{tenant}}")],
         "Seconds per training step per tenant job (baseline about 1.2 s). Throttling or a degraded link shows up here as "
         "a customer-visible slowdown; the ticket fires above 1.45 s.", unit="s", steps=st((G, None), (R, 1.45)), line=True)
    b.ts("Tenant SLO burn rate (fast-burn page at 14.4x)", (12, 47, 12, 7),
         [("tenant:slo_burn_rate:1m", "{{tenant}} 1m"), ("tenant:slo_burn_rate:5m", "{{tenant}} 5m")],
         "How many times faster than allowed each tenant spends its error budget, on two windows (compressed to 1m/5m for "
         "the demo). The page needs both above 14.4x: fast enough to matter, sustained enough not to flap.",
         steps=st((G, None), (R, 14.4)), line=True)
    b.logs("Event log — warning and above, all layers", (0, 54, 24, 10), "level:in(critical, error, warning)",
           "Events from every layer at warning and above (VictoriaLogs): breaker trips, XID lines, link flaps, UPS alarms. "
           "Use it to confirm the order the timeline suggests.")
    return b.save()


def facilities():
    b = Board("02-facilities", "02 · Facilities — power and cooling",
              "Is power and cooling redundant right now, and how much headroom is left?", rack_var=True)
    b.stat("UPS input", (0, 0, 6, 4), "facility_ups_on_battery",
           "Utility or battery. On battery is a page: the runtime clock is running.",
           legend="{{ups}}", mappings=vmap({0: ("Utility", G), 1: ("ON BATTERY", R)}))
    b.stat("UPS output", (6, 0, 6, 4), "facility_ups_output_ok",
           "Whether each UPS delivers power. OFF = every rack lost that feed.",
           legend="{{ups}}", mappings=vmap({1: ("OK", G), 0: ("OFF", R)}))
    b.bargauge("UPS battery charge", (12, 0, 6, 4), [("facility_ups_battery_charge_ratio", "{{ups}}")],
               "Battery state of charge. During 'on battery' the slope tells how long is left.",
               unit="percentunit", steps=st((R, None), (O, 0.3), (G, 0.8)), minv=0, maxv=1)
    b.stat("UPS estimated runtime", (18, 0, 6, 4), "facility_ups_battery_runtime_seconds",
           "Runtime at the current load as reported by the UPS. A few minutes is normal: enough to start a generator. "
           "Under 2 minutes is critical.",
           legend="{{ups}}", unit="s", steps=st((R, None), (O, 120), (G, 300)))
    b.stat("Row power panels (RPP)", (0, 4, 12, 4), "facility_rpp_output_ok",
           "One panel per row and feed. NO OUTPUT = every rack in the row lost that feed.",
           legend="{{rpp}}", mappings=vmap({1: ("OK", G), 0: ("NO OUTPUT", R)}))
    b.stat("Rack power redundancy", (12, 4, 12, 4), 'rack:power_feeds_live:count{rack=~"$rack"}',
           "Live feeds per rack. A+B = redundant. SINGLE FEED works, but the next failure takes the rack down. DARK = no power.",
           legend="{{rack}}", mappings=vmap({2: ("A+B", G), 1: ("SINGLE FEED", O), 0: ("DARK", R)}), links=RACK_LINK)
    b.ts("Rack PDU breaker load (each feed must stay < 50% to survive losing the other)", (0, 8, 12, 8),
         [('facility_pdu_breaker_load_ratio{rack=~"$rack"}', "{{pdu}}")],
         "Load of each PDU feed as a share of its breaker rating. With two feeds each must stay under 50%: when one feed "
         "dies the other carries the whole rack. Above 50% is a hidden single point of failure (precursor signal).",
         unit="percentunit", steps=st((G, None), (Y, 0.5), (R, 0.8)), line=True, minv=0)
    b.ts("RPP load", (12, 8, 12, 8), [("facility_rpp_load_ratio", "{{rpp}}")],
         "Load of each row power panel as a share of its capacity.", unit="percentunit", minv=0)
    b.ts("Rack inlet temperature, max (ASHRAE recommended ≤ 27°C)", (0, 16, 12, 8),
         [('rack:inlet_temperature_celsius:max{rack=~"$rack"}', "{{rack}}")],
         "Hottest inlet sensor per rack. Above 27°C GPUs start to throttle within minutes.",
         unit="celsius", steps=st((G, None), (R, 27)), line=True)
    b.stat("CRAH cooling capacity", (12, 16, 12, 8), "facility_crah_cooling_capacity_ratio",
           "Available cooling capacity of each CRAH. Below 90% its row starts to heat up.",
           legend="{{crah}}", unit="percentunit", steps=st((R, None), (O, 0.7), (G, 0.9)))
    return b.save()


def fabric():
    b = Board("03-fabric", "03 · Network fabric — links, errors, congestion",
              "Which cable is sick, and which GPU and tenant sit behind it?")
    b.stat("Ports down", (0, 0, 6, 4), "count(fabric_port_state == 0) or vector(0)",
           "Fabric ports reported down. Ports of unpowered servers go down too.", steps=st((G, None), (O, 1)))
    b.stat("Flapping links (5m)", (6, 0, 6, 4), "count(increase(fabric_port_link_downed_total[5m]) > 0) or vector(0)",
           "Links that went down and up in the last 5 minutes. Each flap resets the collectives running over it.",
           steps=st((G, None), (R, 1)))
    b.stat("Links below rated speed", (12, 0, 6, 4), "count((fabric_port_speed_gbps > 0) < 400) or vector(0)",
           "Links that renegotiated below 400 Gb/s. The slowest link sets the pace of the whole training job.",
           steps=st((G, None), (O, 1)))
    b.stat("Weakest optical rx power", (18, 0, 6, 4), "min(fabric_port_rx_power_dbm > -30)",
           "Lowest received optical power on any link. Falling rx power is the earliest sign of a dirty or failing optic.",
           unit="dBm", steps=st((R, None), (O, -6), (G, -4)))
    # topk_max / bottomk_min (MetricsQL) pick the five series once for the whole range;
    # plain topk on a range query picks five per step and fills the legend with dozens of ports
    b.ts("Top symbol error rate (cable / optic health)", (0, 4, 12, 8),
         [("topk_max(5, rate(fabric_port_symbol_errors_total[1m]))", "{{switch}}:{{port}}")],
         "Physical-layer errors per second on the five worst ports. Errors rise before a link starts to flap.", unit="cps")
    b.ts("Top congestion (xmit wait rate)", (12, 4, 12, 8),
         [("topk_max(5, rate(fabric_port_xmit_wait_total[1m]))", "{{switch}}:{{port}}")],
         "Time ports wait to transmit because of back-pressure: congestion, not cable errors.", unit="cps")
    b.table("Problem links → GPU → tenant (cable to customer in one join)", (0, 12, 24, 7),
            "(((rate(fabric_port_symbol_errors_total[1m]) > 0) or ((fabric_port_speed_gbps > 0) < 400)) "
            "* on(switch, port) group_left(Hostname, gpu, rack) fabric_link_info) "
            "* on(Hostname, gpu) group_left(tenant) gpu_tenant_info",
            "Every unhealthy link joined through the cabling table to the GPU at its other end and the tenant that owns it.")
    b.ts("Tenant all-reduce time", (0, 19, 12, 8), [("tenant_job_allreduce_seconds", "{{tenant}}")],
         "Time each job spends in the gradient all-reduce. One bad link slows the whole job.", unit="s")
    b.ts("Weakest optics (rx power)", (12, 19, 12, 8),
         [("bottomk_min(5, fabric_port_rx_power_dbm > -30)", "{{switch}}:{{port}}")],
         "The five links with the lowest received optical power over the time range.", unit="dBm")
    return b.save()


def compute():
    b = Board("04-compute", "04 · Compute — GPUs and servers",
              "Are GPUs doing real work, throttling, falling off the bus, or silent?", rack_var=True)
    b.stat("GPUs throttling", (0, 0, 6, 4), "sum(rack:gpu_throttled:count)",
           "GPUs whose clocks are being reduced (thermal or power). They keep running, slower.", steps=st((G, None), (O, 1)))
    b.stat("GPUs off the bus (XID 79)", (6, 0, 6, 4), "count(DCGM_FI_DEV_XID_ERRORS == 79) or vector(0)",
           "GPUs that reported XID 79 (fell off the PCIe bus): drain the node, send the GPU for repair.",
           steps=st((G, None), (R, 1)))
    b.stat("Nodes with no GPU telemetry (unknown)", (12, 0, 6, 4),
           "count(node_topology_info unless on(Hostname) DCGM_FI_DEV_GPU_TEMP) or vector(0)",
           "Servers in the inventory without GPU metrics. Unknown is not healthy.", steps=st((G, None), (O, 1)))
    b.stat("Fleet SM activity (real work, not 'utilization')", (18, 0, 6, 4), "avg(gpu_compute_active_ratio)",
           "Average share of time the streaming multiprocessors are busy (DCGM SM_ACTIVE). 'GPU utilization' only says a "
           "kernel was running; SM activity measures how much of the chip worked.",
           unit="percentunit", steps=st((O, None), (G, 0.6)))
    b.ts("Max GPU temperature per rack", (0, 4, 12, 8), [('rack:gpu_temperature_celsius:max{rack=~"$rack"}', "{{rack}}")],
         "Hottest GPU per rack. Throttling starts at 85°C in this simulation.",
         unit="celsius", steps=st((G, None), (O, 80), (R, 85)), line=True)
    b.ts("Throttling GPUs per rack", (12, 4, 12, 8), [('rack:gpu_throttled:count{rack=~"$rack"}', "{{rack}}")],
         "Number of throttling GPUs per rack. A whole rack throttling together points at air, not at the GPUs.")
    b.ts("SM activity by tenant", (0, 12, 12, 8),
         [("avg by (tenant) (gpu_compute_active_ratio * on(UUID) group_left(tenant) gpu_tenant_info)", "{{tenant}}")],
         "Real GPU work per tenant, joined through the GPU → tenant table by UUID.", unit="percentunit", minv=0, maxv=1)
    b.ts("GPU power by rack", (12, 12, 12, 8),
         [('sum by (rack) (gpu_power_watts * on(Hostname) group_left(rack) node_topology_info{rack=~"$rack"})', "{{rack}}")],
         "GPU power draw per rack. Compare with the PDU load on the facilities dashboard.", unit="watt")
    b.table("XID errors", (0, 20, 12, 7), "(DCGM_FI_DEV_XID_ERRORS > 0) * on(Hostname) group_left(rack) node_topology_info",
            "Last XID error code per GPU with its rack. 79 = fell off the bus, 48 = uncorrectable ECC error.",
            hide=("modelName",))
    b.ts("Corrected ECC errors rate by node", (12, 20, 12, 7),
         [("sum by (Hostname) (rate(DCGM_FI_DEV_ECC_SBE_VOL_TOTAL[5m])) > 0", "{{Hostname}}")],
         "Corrected (single-bit) memory errors per second. A rising rate is a precursor of uncorrectable errors.", unit="cps")
    return b.save()


def platform():
    b = Board("05-platform", "05 · Platform — Kubernetes, tenants, and the observability platform itself",
              "Are tenants getting what they pay for, and is the observability platform itself healthy?")
    b.stat("Nodes Ready", (0, 0, 4, 4), "sum(kube_node_status_ready)",
           "Kubernetes nodes in Ready state (kube-state-metrics in production).", steps=st((G, None)))
    b.stat("Nodes NotReady", (4, 0, 4, 4), "count(kube_node_status_ready == 0) or vector(0)",
           "Nodes the scheduler cannot use.", steps=st((G, None), (R, 1)))
    b.stat("Allocatable GPUs", (8, 0, 4, 4), "sum(kube_node_gpu_allocatable)",
           "GPUs Kubernetes can schedule. Drops when a node goes NotReady or a GPU falls off the bus.", steps=st(("blue", None)))
    b.stat("Active series (VictoriaMetrics)", (12, 0, 4, 4), 'max(vm_cache_entries{type="storage/hour_metric_ids"})',
           "Active time series in storage: the cardinality of this site, live.", steps=st(("blue", None)))
    b.stat("vmagent disk buffer", (16, 0, 4, 4), "sum(vmagent_remotewrite_pending_data_bytes) or vector(0)",
           "Data vmagent holds on disk because storage did not accept it yet. Grows when storage is unreachable: "
           "backpressure in action.", unit="bytes", steps=st((G, None), (O, 1048576)))
    b.stat("Telemetry targets down", (20, 0, 4, 4), "count(up == 0) or vector(0)",
           "Scrape targets that failed their last scrape: blind spots. TelemetryTargetDown pages after 30 s.",
           steps=st((G, None), (R, 1)))
    b.bargauge("GPUs per tenant: allocated vs healthy", (0, 4, 12, 8),
               [("tenant:gpus_allocated:count", "{{tenant}} allocated"), ("tenant:gpus_healthy:count", "{{tenant}} healthy")],
               "Allocated vs usable GPUs per tenant. The gap is what the tenant pays for and cannot use.",
               steps=st(("blue", None)))
    b.ts("Tenant GPU availability (SLO 99.5%)", (12, 4, 12, 8), [("tenant:gpu_availability:ratio", "{{tenant}}")],
         "Share of allocated GPUs that are usable, the SLI behind the tenant SLO. The red line is the 99.5% target.",
         unit="percentunit", steps=st((R, None), (G, 0.995)), line=True, maxv=1, softmin=0.9)
    b.ts("Error-budget burn rate, 5m", (0, 12, 12, 8), [("tenant:slo_burn_rate:5m", "{{tenant}}")],
         "How fast each tenant spends its error budget. 1 = exactly on budget; the fast-burn page needs 14.4.",
         steps=st((G, None), (R, 14.4)), line=True)
    b.ts("Training jobs up", (12, 12, 12, 8), [("tenant_job_up", "{{tenant}}")],
         "1 while the tenant job runs, 0 while it is stalled.", minv=0, maxv=1)
    b.table("Tenants and SLA tiers", (0, 20, 12, 6), "tenant_info", "Tenant registry: contractual SLA tier per tenant.")
    b.ts("Tenant step time", (12, 20, 12, 6), [("tenant_job_step_duration_seconds", "{{tenant}}")],
         "Seconds per training step: the customer-visible speed of the job.", unit="s")
    return b.save()


if __name__ == "__main__":
    for build in (incident, facilities, fabric, compute, platform):
        board = build()
        print(f"{board['uid']}: {len(board['panels'])} panels")
