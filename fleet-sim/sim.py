"""
fleet-sim: a physics-lite simulator of one GPU data center.

In-memory model:  UPS -> RPP -> rack PDU (A/B) -> servers -> GPUs -> tenant jobs
                  CRAH -> rack inlet air -> GPU temperature -> throttling
                  leaf/spine switches -> ports -> NICs -> GPUs (rail-optimized)

Exposed in Prometheus text format, as if they were separate exporters:
  /metrics/facilities  UPS, RPP, rack PDUs, rack sensors, CRAHs   (SNMP / Modbus / BMS in prod)
  /metrics/gpu         GPUs with real DCGM exporter names         (dcgm-exporter in prod)
  /metrics/fabric      switch-side and host-side port counters    (UFM / gNMI / node_exporter in prod)
  /metrics/platform    Kubernetes node readiness, BMC PSUs, tenant jobs, GPU allocation
  /metrics/topology    join tables (power chain, cooling, cabling) (NetBox + Kubernetes in prod)
Events are pushed as logs to VictoriaLogs (XID lines, breaker trips, link flaps, UPS alarms).

Scenarios (HTTP GET /scenario/<name>?...):
  cooling_failure?row=r1          CRAH loses 70% of its capacity
  rpp_trip?rpp=rpp-r1-b           a row power panel trips: every rack in the row loses feed B
  ups_on_battery?ups=ups-b        utility loss on one UPS; the battery drains, then the feed drops
  feed_loss?rack=r2-02&feed=A     one rack loses one feed
  fabric_link?host=gpu-r2-01-n01&gpu=3   a cable/optic degrades: errors -> flaps -> runs at half speed
  reset
"""
import json
import os
import queue
import random
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import yaml

TICK_SECONDS = float(os.getenv("TICK_SECONDS", "5"))
SPEED = float(os.getenv("SPEED", "3"))
PORT = int(os.getenv("PORT", "9100"))
TOPOLOGY = os.getenv("TOPOLOGY", "topology.yaml")
LOGS_URL = os.getenv("LOGS_URL", "")  # e.g. http://victorialogs:9428

SW_THERMAL, HW_THERMAL = 0x20, 0x40
NODE_BASE_W, GPU_IDLE_W, GPU_MAX_EXTRA_W = 1200.0, 120.0, 580.0
THROTTLE_ON_C, THROTTLE_OFF_C, THROTTLE_HW_C = 85.0, 80.0, 90.0
MAX_XIDS_PER_SCENARIO = 2
TRIP_AFTER_OVER_TICKS = 6


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LogShipper:
    """Ships events to VictoriaLogs (jsonline API). Never blocks the simulator."""

    def __init__(self, url):
        self.url = url
        self.q = queue.Queue(maxsize=10000)
        if url:
            threading.Thread(target=self._loop, daemon=True).start()

    def emit(self, source, level, msg, **fields):
        rec = {"_time": now_iso(), "_msg": msg, "level": level, "source": source, **fields}
        print(f"[{level}] {source}: {msg}", flush=True)
        if self.url:
            try:
                self.q.put_nowait(rec)
            except queue.Full:
                pass  # backpressure policy: drop rather than block the simulation

    def _loop(self):
        target = (f"{self.url}/insert/jsonline?_stream_fields=site,source"
                  f"&_msg_field=_msg&_time_field=_time")
        while True:
            time.sleep(2)
            batch = []
            while not self.q.empty() and len(batch) < 1000:
                batch.append(self.q.get_nowait())
            if not batch:
                continue
            body = "\n".join(json.dumps(r) for r in batch).encode()
            try:
                urllib.request.urlopen(urllib.request.Request(target, data=body, method="POST",
                                       headers={"Content-Type": "application/stream+json"}), timeout=5)
            except Exception:
                pass


class Fleet:
    def __init__(self, topo, log):
        self.lock = threading.Lock()
        self.log = log
        self.site = topo["site"]
        self.model = topo["gpu_model"]
        self.rpp_cap = float(topo["rpp_capacity_watts"])
        self.ups_cap = float(topo["ups_capacity_watts"])
        self.tenants = topo["tenants"]
        rnd = random.Random(42)

        self.ups = {u["name"]: {"feed": u["feed"], "utility": True, "charge": 1.0, "ok": True}
                    for u in topo["power"]["ups"]}
        ups_by_feed = {u["feed"]: name for name, u in self.ups.items()}

        self.crahs, self.rpps, self.racks, self.nodes, self.jobs = {}, {}, {}, {}, {}
        node_index = 0
        for row in topo["rows"]:
            self.crahs[row["crah"]] = {"row": row["name"], "capacity": 1.0}
            for feed, rpp in row["rpp"].items():
                self.rpps[rpp] = {"row": row["name"], "feed": feed, "ups": ups_by_feed[feed], "tripped": False}
            for rack in row["racks"]:
                rname, tenant = rack["name"], rack.get("tenant", "")
                cap = float(rack.get("pdu_feed_capacity_watts", topo["pdu_feed_capacity_watts"]))
                self.racks[rname] = {
                    "row": row["name"], "crah": row["crah"], "tenant": tenant, "inlet": 22.0, "power": 0.0,
                    "feeds": {f: {"rpp": row["rpp"][f], "cap": cap, "tripped": False, "manual_off": False,
                                  "over": 0, "watts": 0.0} for f in ("A", "B")},
                }
                if tenant:
                    self.jobs.setdefault(tenant, {"job": f"{tenant}-train", "step": 1.2, "allreduce": 0.3, "up": 1})
                for n in range(topo["nodes_per_rack"]):
                    host = f"gpu-{rname}-n{n + 1:02d}"
                    self.nodes[host] = {
                        "rack": rname, "tenant": tenant, "powered": True, "index": node_index,
                        "gpus": [{
                            "idx": g, "uuid": f"GPU-{uuid.UUID(int=rnd.getrandbits(128))}",
                            "target_sm": rnd.uniform(0.74, 0.86) if tenant else 0.0,
                            "sm": 0.0, "temp": 35.0, "throttle": 0, "hot": 0,
                            "sbe": rnd.randint(0, 20), "dbe": 0, "xid": 0, "dead": False,
                        } for g in range(topo["gpus_per_node"])],
                    }
                    node_index += 1

        # fabric: rail-optimized leaves + spines
        fab = topo["fabric"]
        self.links = []
        for host, node in self.nodes.items():
            for g in node["gpus"]:
                leaf = f"leaf-{g['idx'] // 2 + 1}"
                port = node["index"] * 2 + g["idx"] % 2 + 1
                self.links.append(self._link("host", leaf, port, host=host, gpu=g["idx"]))
        for li in range(fab["leaves"]):
            for si in range(fab["spines"]):
                for k in range(fab["uplinks_per_leaf_per_spine"]):
                    lport = 100 + si * fab["uplinks_per_leaf_per_spine"] + k + 1
                    sport = li * fab["uplinks_per_leaf_per_spine"] + k + 1
                    self.links.append(self._link("uplink", f"leaf-{li + 1}", lport,
                                                 peer=f"spine-{si + 1}", peer_port=sport))
        self.xids = 0

    @staticmethod
    def _link(kind, switch, port, **kw):
        return {"kind": kind, "switch": switch, "port": port, "mode": "ok", "mode_ticks": 0,
                "up": 1, "speed": 400, "sym": 0, "rcv_err": 0, "downed": 0, "wait": 0, "bytes": 0,
                "rx_power": round(random.uniform(-2.6, -1.4), 2), **kw}

    # ------------------------------------------------------------------ power chain
    def feed_live(self, rack, f):
        feed = rack["feeds"][f]
        rpp = self.rpps[feed["rpp"]]
        ups = self.ups[rpp["ups"]]
        return ups["ok"] and not rpp["tripped"] and not feed["tripped"] and not feed["manual_off"]

    def feed_input_ok(self, rack, f):
        """Is there voltage at the PDU input (upstream healthy), regardless of the PDU breaker?"""
        rpp = self.rpps[rack["feeds"][f]["rpp"]]
        return self.ups[rpp["ups"]]["ok"] and not rpp["tripped"] and not rack["feeds"][f]["manual_off"]

    # ------------------------------------------------------------------ physics
    def tick(self):
        a = min(1.0, 0.12 * SPEED)
        with self.lock:
            self._tick_ups()
            self._tick_power(a)
            self._tick_cooling(a)
            self._tick_gpus(a)
            self._tick_fabric()
            self._tick_jobs()

    def _tick_ups(self):
        for name, u in self.ups.items():
            if not u["utility"] and u["ok"]:
                u["charge"] = max(0.0, u["charge"] - 0.012 * SPEED)
                if u["charge"] <= 0.0:
                    u["ok"] = False
                    self.log.emit("facilities", "critical", f"UPS {name}: battery exhausted, output OFF (feed {u['feed']} lost)",
                                  site=self.site, device=name, feed=u["feed"])
            elif u["utility"] and u["charge"] < 1.0:
                u["charge"] = min(1.0, u["charge"] + 0.004 * SPEED)

    def _tick_power(self, a):
        for rname, rack in self.racks.items():
            live = [f for f in ("A", "B") if self.feed_live(rack, f)]
            powered = bool(live)
            for host in self._nodes_in(rname):
                node = self.nodes[host]
                if node["powered"] and not powered:
                    self.log.emit("platform", "error", f"Node {host} is NotReady (lost power)",
                                  site=self.site, Hostname=host, rack=rname)
                node["powered"] = powered
            watts = 0.0
            for host in self._nodes_in(rname):
                node = self.nodes[host]
                if not node["powered"]:
                    continue
                watts += NODE_BASE_W
                for g in node["gpus"]:
                    if not g["dead"]:
                        watts += (GPU_IDLE_W + GPU_MAX_EXTRA_W * g["sm"]) * random.uniform(0.96, 1.07)
            rack["power"] = watts
            for f in ("A", "B"):
                feed = rack["feeds"][f]
                feed["watts"] = watts / len(live) if f in live else 0.0
                ratio = feed["watts"] / feed["cap"]
                feed["over"] = feed["over"] + 1 if ratio > 1.0 else max(0, feed["over"] - 1)
                if feed["over"] >= TRIP_AFTER_OVER_TICKS and not feed["tripped"]:
                    feed["tripped"] = True
                    self.log.emit("facilities", "critical",
                                  f"PDU pdu-{rname}-{f.lower()}: breaker TRIPPED on overload ({ratio:.0%} of rating)",
                                  site=self.site, device=f"pdu-{rname}-{f.lower()}", rack=rname, feed=f)

    def _tick_cooling(self, a):
        for rack in self.racks.values():
            cap = self.crahs[rack["crah"]]["capacity"]
            target = 22.0 + (1.0 - cap) * (rack["power"] / 1000.0) * 1.1
            rack["inlet"] += (target - rack["inlet"]) * a + random.gauss(0, 0.05)

    def _tick_gpus(self, a):
        for host, node in self.nodes.items():
            if not node["powered"]:
                continue
            inlet = self.racks[node["rack"]]["inlet"]
            for g in node["gpus"]:
                if g["dead"]:
                    continue
                want = g["target_sm"] * random.uniform(0.97, 1.03)
                if g["temp"] >= THROTTLE_HW_C:
                    g["throttle"], want = SW_THERMAL | HW_THERMAL, want * 0.55
                elif g["temp"] >= THROTTLE_ON_C or (g["throttle"] and g["temp"] > THROTTLE_OFF_C):
                    g["throttle"], want = SW_THERMAL, want * 0.75
                else:
                    g["throttle"] = 0
                g["hot"] = g["hot"] + 1 if g["throttle"] else max(0, g["hot"] - 1)
                g["sm"] += (want - g["sm"]) * a
                g["temp"] += (inlet + 12 + 45 * g["sm"] - g["temp"]) * a + random.gauss(0, 0.2)
                if random.random() < 0.0005:
                    g["sbe"] += 1
                if g["hot"] > 12 and self.xids < MAX_XIDS_PER_SCENARIO and random.random() < 0.002:
                    g["xid"], g["dead"] = 79, True
                    self.xids += 1
                    self.log.emit("compute", "critical",
                                  f"NVRM: Xid (PCI:0000:{0x18 + g['idx']:02x}:00): 79, pid=0, GPU has fallen off the bus.",
                                  site=self.site, Hostname=host, gpu=str(g["idx"]), UUID=g["uuid"], xid="79")
                    self.log.emit("platform", "warning", f"Node {host}: GPU {g['idx']} unhealthy, device plugin marked it unallocatable",
                                  site=self.site, Hostname=host)

    def _tick_fabric(self):
        for l in self.links:
            peer_dead = l["kind"] == "host" and not self.nodes[l["host"]]["powered"]
            if peer_dead:
                l["up"] = 0
                continue
            l["mode_ticks"] += 1
            if l["mode"] == "ok":
                l["up"], l["speed"] = 1, 400
                l["wait"] += random.randint(0, 50)
            elif l["mode"] == "degrading":
                l["sym"] += random.randint(40, 120)
                l["rcv_err"] += random.randint(0, 3)
                l["rx_power"] = round(l["rx_power"] - 0.12, 2)
                l["wait"] += random.randint(200, 800)
                if l["mode_ticks"] > 10:
                    l["mode"], l["mode_ticks"] = "flapping", 0
            elif l["mode"] == "flapping":
                l["sym"] += random.randint(200, 600)
                l["rcv_err"] += random.randint(5, 20)
                if l["mode_ticks"] % 2 == 1:
                    l["up"] = 0
                    l["downed"] += 1
                    self.log.emit("fabric", "error", f"{l['switch']} port {l['port']}: link DOWN (peer {l.get('host')} mlx5_{l.get('gpu')})",
                                  site=self.site, device=l["switch"], port=str(l["port"]), Hostname=l.get("host", ""))
                else:
                    l["up"] = 1
                    self.log.emit("fabric", "warning", f"{l['switch']} port {l['port']}: link UP, renegotiated",
                                  site=self.site, device=l["switch"], port=str(l["port"]), Hostname=l.get("host", ""))
                if l["mode_ticks"] > 12:
                    l["mode"], l["mode_ticks"], l["up"], l["speed"] = "degraded", 0, 1, 200
                    self.log.emit("fabric", "warning", f"{l['switch']} port {l['port']}: link up at 200 Gb/s (expected 400 Gb/s)",
                                  site=self.site, device=l["switch"], port=str(l["port"]), Hostname=l.get("host", ""))
            elif l["mode"] == "degraded":
                l["up"], l["speed"] = 1, 200
                l["wait"] += random.randint(500, 1500)
            if l["up"]:
                l["bytes"] += int(l["speed"] / 8 * 1e9 * TICK_SECONDS * random.uniform(0.3, 0.6))

    def _tick_jobs(self):
        bad_links = {(l["host"], l["gpu"]): l["mode"] for l in self.links if l["kind"] == "host" and l["mode"] != "ok"}
        for tenant, job in self.jobs.items():
            nodes = [(h, n) for h, n in self.nodes.items() if n["tenant"] == tenant]
            gpus = [g for _, n in nodes for g in n["gpus"]]
            was_up = job["up"]
            if any(not n["powered"] for _, n in nodes) or any(g["dead"] for g in gpus):
                job["up"] = 0
            else:
                job["up"] = 1
                fabric = 1.0
                for h, n in nodes:
                    for g in n["gpus"]:
                        mode = bad_links.get((h, g["idx"]))
                        fabric = max(fabric, {"degrading": 1.08, "flapping": 1.55, "degraded": 1.35}.get(mode, 1.0))
                compute = max(g["target_sm"] / max(g["sm"], 0.05) for g in gpus)
                job["step"] = 1.2 * compute * fabric * random.uniform(0.98, 1.02)
                job["allreduce"] = 0.3 * fabric * random.uniform(0.95, 1.05)
            if was_up and not job["up"]:
                self.log.emit("tenant", "error", f"Job {job['job']} ({tenant}): NCCL watchdog timeout, job stalled, restarting from checkpoint",
                              site=self.site, tenant=tenant)

    def _nodes_in(self, rack):
        return [h for h, n in self.nodes.items() if n["rack"] == rack]

    # ------------------------------------------------------------------ scenarios
    def scenario(self, name, q):
        arg = lambda k, d: q.get(k, [d])[0]
        with self.lock:
            if name == "cooling_failure":
                row = arg("row", "r1")
                for cname, c in self.crahs.items():
                    if c["row"] == row:
                        c["capacity"] = 0.3
                        self.log.emit("facilities", "error", f"CRAH {cname}: fan 2 failure, cooling capacity 30%",
                                      site=self.site, device=cname, row=row)
                return f"CRAH of row {row} degraded to 30% capacity"
            if name == "rpp_trip":
                rpp = arg("rpp", "rpp-r1-b")
                self.rpps[rpp]["tripped"] = True
                self.log.emit("facilities", "critical", f"RPP {rpp}: main breaker TRIPPED, row {self.rpps[rpp]['row']} feed {self.rpps[rpp]['feed']} lost",
                              site=self.site, device=rpp, row=self.rpps[rpp]["row"], feed=self.rpps[rpp]["feed"])
                return f"{rpp} tripped"
            if name == "ups_on_battery":
                ups = arg("ups", "ups-b")
                self.ups[ups]["utility"] = False
                self.log.emit("facilities", "critical", f"UPS {ups}: utility input lost, running ON BATTERY",
                              site=self.site, device=ups, feed=self.ups[ups]["feed"])
                return f"{ups} on battery"
            if name == "feed_loss":
                rack, feed = arg("rack", "r2-02"), arg("feed", "A")
                self.racks[rack]["feeds"][feed]["manual_off"] = True
                self.log.emit("facilities", "error", f"PDU pdu-{rack}-{feed.lower()}: input voltage lost",
                              site=self.site, device=f"pdu-{rack}-{feed.lower()}", rack=rack, feed=feed)
                return f"rack {rack}: feed {feed} lost"
            if name == "fabric_link":
                host, gpu = arg("host", "gpu-r2-01-n01"), int(arg("gpu", "3"))
                for l in self.links:
                    if l["kind"] == "host" and l["host"] == host and l["gpu"] == gpu:
                        l["mode"], l["mode_ticks"] = "degrading", 0
                        return f"link {l['switch']}:{l['port']} <-> {host} mlx5_{gpu} degrading"
                return None
            if name == "reset":
                for c in self.crahs.values():
                    c["capacity"] = 1.0
                for r in self.rpps.values():
                    r["tripped"] = False
                for u in self.ups.values():
                    u.update(utility=True, ok=True)
                for rack in self.racks.values():
                    for f in rack["feeds"].values():
                        f.update(tripped=False, manual_off=False, over=0)
                for n in self.nodes.values():
                    for g in n["gpus"]:
                        g.update(dead=False, xid=0, hot=0)
                for l in self.links:
                    l.update(mode="ok", mode_ticks=0, up=1, speed=400, rx_power=round(random.uniform(-2.6, -1.4), 2))
                self.xids = 0
                self.log.emit("ops", "info", "Scenario reset: all faults cleared", site=self.site)
                return "reset to normal"
        return None

    # ------------------------------------------------------------------ exposition
    def render(self, section):
        out = Exposition()
        with self.lock:
            for name, fn in (("facilities", self._facilities), ("gpu", self._gpu), ("fabric", self._fabric),
                             ("platform", self._platform), ("topology", self._topology)):
                if section in (name, "all"):
                    fn(out)
        return out.text()

    def _facilities(self, o):
        s = self.site
        feed_watts = {"A": 0.0, "B": 0.0}
        rpp_watts = {r: 0.0 for r in self.rpps}
        for rname, rack in self.racks.items():
            base = {"site": s, "row": rack["row"], "rack": rname}
            for f, feed in rack["feeds"].items():
                lbl = {**base, "pdu": f"pdu-{rname}-{f.lower()}", "feed": f}
                o.add("facility_pdu_power_watts", "gauge", lbl, round(feed["watts"], 1))
                o.add("facility_pdu_breaker_load_ratio", "gauge", lbl, round(feed["watts"] / feed["cap"], 3))
                o.add("facility_pdu_input_ok", "gauge", lbl, 1 if self.feed_input_ok(rack, f) else 0)
                o.add("facility_pdu_breaker_tripped", "gauge", lbl, 1 if feed["tripped"] else 0)
                rpp_watts[feed["rpp"]] += feed["watts"]
                feed_watts[f] += feed["watts"]
            for pos, off in (("bottom", -1.0), ("middle", 0.0), ("top", 1.5)):
                o.add("facility_rack_inlet_temperature_celsius", "gauge", {**base, "position": pos}, round(rack["inlet"] + off, 2))
        for name, r in self.rpps.items():
            lbl = {"site": s, "row": r["row"], "rpp": name, "feed": r["feed"]}
            ok = self.ups[r["ups"]]["ok"] and not r["tripped"]
            o.add("facility_rpp_output_ok", "gauge", lbl, 1 if ok else 0)
            o.add("facility_rpp_power_watts", "gauge", lbl, round(rpp_watts[name], 1))
            o.add("facility_rpp_load_ratio", "gauge", lbl, round(rpp_watts[name] / self.rpp_cap, 3))
        for name, u in self.ups.items():
            lbl = {"site": s, "ups": name, "feed": u["feed"]}
            o.add("facility_ups_output_ok", "gauge", lbl, 1 if u["ok"] else 0)
            o.add("facility_ups_on_battery", "gauge", lbl, 0 if u["utility"] else 1)
            o.add("facility_ups_battery_charge_ratio", "gauge", lbl, round(u["charge"], 3))
            runtime = u["charge"] * 600 * (self.ups_cap / max(feed_watts[u["feed"]], 1.0)) / 4
            o.add("facility_ups_battery_runtime_seconds", "gauge", lbl, round(min(runtime, 7200), 0))
            o.add("facility_ups_load_ratio", "gauge", lbl, round(feed_watts[u["feed"]] / self.ups_cap, 3))
        for name, c in self.crahs.items():
            lbl = {"site": s, "row": c["row"], "crah": name}
            o.add("facility_crah_status", "gauge", lbl, 1 if c["capacity"] >= 0.99 else 0)
            o.add("facility_crah_cooling_capacity_ratio", "gauge", lbl, round(c["capacity"], 2))

    def _gpu(self, o):
        for host, n in self.nodes.items():
            if not n["powered"]:
                continue  # exporter unreachable: series go stale (the "unknown" state, not "healthy")
            for g in n["gpus"]:
                lbl = {"Hostname": host, "gpu": str(g["idx"]), "UUID": g["uuid"], "modelName": self.model}
                if g["dead"]:
                    o.add("DCGM_FI_DEV_XID_ERRORS", "gauge", lbl, g["xid"])
                    continue
                o.add("DCGM_FI_DEV_GPU_TEMP", "gauge", lbl, round(g["temp"]))
                o.add("DCGM_FI_DEV_POWER_USAGE", "gauge", lbl, round(GPU_IDLE_W + GPU_MAX_EXTRA_W * g["sm"], 1))
                o.add("DCGM_FI_PROF_SM_ACTIVE", "gauge", lbl, round(g["sm"], 3))
                o.add("DCGM_FI_DEV_SM_CLOCK", "gauge", lbl, round(1980 * (0.6 if g["throttle"] else 1.0)))
                o.add("DCGM_FI_DEV_CLOCK_THROTTLE_REASONS", "gauge", lbl, g["throttle"])
                o.add("DCGM_FI_DEV_ECC_SBE_VOL_TOTAL", "counter", lbl, g["sbe"])
                o.add("DCGM_FI_DEV_ECC_DBE_VOL_TOTAL", "counter", lbl, g["dbe"])
                o.add("DCGM_FI_DEV_XID_ERRORS", "gauge", lbl, g["xid"])

    def _fabric(self, o):
        for l in self.links:
            sw = {"switch": l["switch"], "port": str(l["port"])}
            o.add("fabric_port_state", "gauge", sw, l["up"])
            o.add("fabric_port_speed_gbps", "gauge", sw, l["speed"] if l["up"] else 0)
            o.add("fabric_port_symbol_errors_total", "counter", sw, l["sym"])
            o.add("fabric_port_rcv_errors_total", "counter", sw, l["rcv_err"])
            o.add("fabric_port_link_downed_total", "counter", sw, l["downed"])
            o.add("fabric_port_xmit_wait_total", "counter", sw, l["wait"])
            o.add("fabric_port_xmit_bytes_total", "counter", sw, l["bytes"])
            o.add("fabric_port_rx_power_dbm", "gauge", sw, l["rx_power"] if l["up"] else -40.0)
            if l["kind"] == "host" and self.nodes[l["host"]]["powered"]:
                # the same cable seen from the server side (node_exporter infiniband collector in prod)
                hl = {"Hostname": l["host"], "device": f"mlx5_{l['gpu']}"}
                o.add("node_ib_port_state", "gauge", hl, l["up"])
                o.add("node_ib_port_symbol_errors_total", "counter", hl, l["sym"])
                o.add("node_ib_port_link_downed_total", "counter", hl, l["downed"])

    def _platform(self, o):
        for host, n in self.nodes.items():
            o.add("kube_node_status_ready", "gauge", {"node": host}, 1 if n["powered"] else 0)
            o.add("kube_node_gpu_allocatable", "gauge", {"node": host},
                  sum(1 for g in n["gpus"] if not g["dead"]) if n["powered"] else 0)
            if n["powered"]:
                rack = self.racks[n["rack"]]
                for psu, feed in (("psu1", "A"), ("psu2", "B")):
                    o.add("node_bmc_psu_input_ok", "gauge", {"Hostname": host, "psu": psu},
                          1 if self.feed_live(rack, feed) else 0)
                o.add("node_bmc_inlet_temperature_celsius", "gauge", {"Hostname": host}, round(rack["inlet"] + 0.8, 1))
        for tenant, j in self.jobs.items():
            lbl = {"tenant": tenant, "workload": j["job"]}
            o.add("tenant_job_up", "gauge", lbl, j["up"])
            if j["up"]:
                o.add("tenant_job_step_duration_seconds", "gauge", lbl, round(j["step"], 3))
                o.add("tenant_job_allreduce_seconds", "gauge", lbl, round(j["allreduce"], 3))

    def _topology(self, o):
        s = self.site
        for host, n in self.nodes.items():
            r = self.racks[n["rack"]]
            o.add("node_topology_info", "gauge", {"Hostname": host, "site": s, "row": r["row"], "rack": n["rack"], "crah": r["crah"]}, 1)
            fa, fb = r["feeds"]["A"], r["feeds"]["B"]
            o.add("node_power_info", "gauge", {
                "Hostname": host, "rack": n["rack"], "row": r["row"],
                "pdu_a": f"pdu-{n['rack']}-a", "rpp_a": fa["rpp"], "ups_a": self.rpps[fa["rpp"]]["ups"],
                "pdu_b": f"pdu-{n['rack']}-b", "rpp_b": fb["rpp"], "ups_b": self.rpps[fb["rpp"]]["ups"]}, 1)
            if n["tenant"]:
                o.add("node_tenant_info", "gauge", {"Hostname": host, "tenant": n["tenant"]}, 1)
                for g in n["gpus"]:
                    o.add("gpu_tenant_info", "gauge", {"Hostname": host, "gpu": str(g["idx"]), "UUID": g["uuid"],
                                                       "tenant": n["tenant"], "workload": self.jobs[n["tenant"]]["job"]}, 1)
        for name, rp in self.rpps.items():
            o.add("facility_rpp_info", "gauge", {"rpp": name, "row": rp["row"], "feed": rp["feed"], "ups": rp["ups"]}, 1)
        for l in self.links:
            if l["kind"] == "host":
                r = self.racks[self.nodes[l["host"]]["rack"]]
                o.add("fabric_link_info", "gauge", {"switch": l["switch"], "port": str(l["port"]), "peer_type": "host",
                                                    "Hostname": l["host"], "gpu": str(l["gpu"]),
                                                    "rack": self.nodes[l["host"]]["rack"], "row": r["row"]}, 1)
            else:
                o.add("fabric_link_info", "gauge", {"switch": l["switch"], "port": str(l["port"]), "peer_type": "switch",
                                                    "peer_switch": l["peer"], "peer_port": str(l["peer_port"])}, 1)
        for t, meta in self.tenants.items():
            o.add("tenant_info", "gauge", {"tenant": t, "sla": str(meta["sla"])}, 1)

    def status(self):
        with self.lock:
            return {
                "ups": {k: {"utility": v["utility"], "ok": v["ok"], "charge": round(v["charge"], 2)} for k, v in self.ups.items()},
                "rpp_tripped": [k for k, v in self.rpps.items() if v["tripped"]],
                "dark_racks": [k for k, r in self.racks.items() if not any(self.feed_live(r, f) for f in "AB")],
                "tripped_pdus": [f"pdu-{k}-{f.lower()}" for k, r in self.racks.items() for f, fd in r["feeds"].items() if fd["tripped"]],
                "jobs": {k: {"up": v["up"], "step": round(v["step"], 2)} for k, v in self.jobs.items()},
                "dead_gpus": [f"{h}/{g['idx']}" for h, n in self.nodes.items() for g in n["gpus"] if g["dead"]],
                "bad_links": [f"{l['switch']}:{l['port']} {l['mode']}" for l in self.links if l["mode"] != "ok"],
            }


class Exposition:
    def __init__(self):
        self.metrics = {}

    def add(self, name, mtype, labels, value):
        self.metrics.setdefault(name, (mtype, []))[1].append((labels, value))

    def text(self):
        lines = []
        for name, (mtype, samples) in self.metrics.items():
            lines.append(f"# TYPE {name} {mtype}")
            for labels, value in samples:
                lab = ",".join(f'{k}="{v}"' for k, v in labels.items())
                lines.append(f"{name}{{{lab}}} {value}")
        return "\n".join(lines) + "\n"


def make_handler(fleet):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if parts[0] == "metrics":
                self._send(200, fleet.render(parts[1] if len(parts) > 1 else "all"), "text/plain; version=0.0.4")
            elif parts[0] == "scenario" and len(parts) > 1:
                msg = fleet.scenario(parts[1], parse_qs(url.query))
                self._send(200 if msg else 404, (msg or "unknown scenario") + "\n", "text/plain")
            elif parts[0] == "status":
                self._send(200, json.dumps(fleet.status(), indent=2) + "\n", "application/json")
            else:
                self._send(404, "try /metrics, /status or /scenario/<name>\n", "text/plain")

        def _send(self, code, body, ctype):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


def main():
    log = LogShipper(LOGS_URL)
    with open(TOPOLOGY, encoding="utf-8") as f:
        fleet = Fleet(yaml.safe_load(f), log)
    for _ in range(60):
        fleet.tick()

    def loop():
        while True:
            time.sleep(TICK_SECONDS)
            fleet.tick()
            if random.random() < 0.1:
                log.emit("platform", "info", "kubelet: node heartbeats OK", site=fleet.site)

    threading.Thread(target=loop, daemon=True).start()
    log.emit("ops", "info", f"fleet-sim started: {len(fleet.nodes)} nodes, "
             f"{sum(len(n['gpus']) for n in fleet.nodes.values())} GPUs, {len(fleet.links)} fabric links", site=fleet.site)
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(fleet)).serve_forever()


if __name__ == "__main__":
    main()
