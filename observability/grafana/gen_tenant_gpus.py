#!/usr/bin/env python3
"""Generates dashboards/tenant-gpus.json, the Tenant Cluster GPUs dashboard. Edit here, rerun:

    python3 gen_tenant_gpus.py

GPUs per tenant cluster, with Project, Tenant Cluster, Node and GPU Model selectors, on the
fleet datasource. Only DCGM series that report real values on both NVIDIA hardware and the
nvml-mock simulated GPUs (private-node-kubevirt-provider/mock-gpu.md) are used. Left out on
purpose, because the mock reports them static or zero: framebuffer used, memory temperature,
SM and memory clocks, total energy, encoder/decoder utilization and NVLink bandwidth.

GPUs are keyed by node (Hostname) and index, never UUID: nvml-mock gives GPU N the same
UUID on every node.
"""
import json
import pathlib

FLEET_DS = "e8b3387a-24f9-4f94-9ffc-555c6cb944f8"
DS = {"type": "prometheus", "uid": "${datasource}"}

# Every tenant-cluster DCGM series carries these. The tenant variable's All value is ".+",
# so control-plane-cluster GPUs (no vcluster_platform_instance) never mix in.
SEL = ('vcluster_platform_project=~"$project",vcluster_platform_instance=~"$tenant",'
       'Hostname=~"$node",modelName=~"$model"')
NODE_SEL = ('vcluster_platform_project=~"$project",vcluster_platform_instance=~"$tenant",'
            'k8s_node_name=~"$node"')
GPU_KEY = "vcluster_platform_project, vcluster_platform_instance, Hostname, gpu"
GPU_LEGEND = "{{vcluster_platform_instance}} / {{Hostname}} / GPU {{gpu}}"


def m(name):
    return f"{name}{{{SEL}}}"


def per_gpu(expr):
    return f"max by ({GPU_KEY}) ({expr})"


FB_TOTAL = f"({m('DCGM_FI_DEV_FB_FREE')} + {m('DCGM_FI_DEV_FB_USED')} + {m('DCGM_FI_DEV_FB_RESERVED')})"
XID_1H = f"increase({m('DCGM_EXP_XID_ERRORS_COUNT_total')}[1h])"

_ids = iter(range(1, 1000))


def target(expr, legend="", ref="A", instant=False, fmt="time_series"):
    t = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend}
    if instant:
        t.update({"instant": True, "range": False, "format": fmt})
    return t


def stat(title, expr, x, y, w=3, unit="none", desc="", decimals=None, thresholds=None):
    steps = thresholds or [{"color": "blue", "value": None}]
    d = {"unit": unit, "color": {"mode": "thresholds"},
         "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        d["decimals"] = decimals
    return {"id": next(_ids), "type": "stat", "title": title, "description": desc,
            "datasource": DS, "gridPos": {"h": 4, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "textMode": "auto"},
            "targets": [target(expr, instant=True)]}


def ts(title, targets, x, y, w=12, h=8, unit="none", desc="", maxv=None):
    d = {"unit": unit, "custom": {"fillOpacity": 10, "lineWidth": 1, "showPoints": "never"}}
    if maxv is not None:
        d.update({"min": 0, "max": maxv})
    return {"id": next(_ids), "type": "timeseries", "title": title, "description": desc,
            "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"legend": {"displayMode": "table", "placement": "right",
                                   "calcs": ["mean", "lastNotNull", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
            "targets": targets}


def bar(title, expr, legend, x, y, w=8, h=7, unit="none", desc=""):
    return {"id": next(_ids), "type": "bargauge", "title": title, "description": desc,
            "datasource": DS, "gridPos": {"h": h, "w": w, "x": x, "y": y},
            "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                         "color": {"mode": "palette-classic"}}, "overrides": []},
            "options": {"displayMode": "basic", "orientation": "horizontal", "showUnfilled": True,
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "targets": [target(expr, legend, instant=True)]}


def row(title, y):
    return {"id": next(_ids), "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


def inventory(y):
    q = [
        ("A", f'count by ({GPU_KEY}, modelName, DCGM_FI_DRIVER_VERSION) ({m("DCGM_FI_DEV_GPU_UTIL")})'),
        ("B", per_gpu(m("DCGM_FI_DEV_GPU_UTIL"))),
        ("C", per_gpu(f"100 * {m('DCGM_FI_PROF_PIPE_TENSOR_ACTIVE')}")),
        ("D", per_gpu(m("DCGM_FI_DEV_GPU_TEMP"))),
        ("E", per_gpu(m("DCGM_FI_DEV_POWER_USAGE"))),
        ("F", per_gpu(FB_TOTAL)),
        ("G", per_gpu(XID_1H)),
    ]
    rename = {"vcluster_platform_project": "Project", "vcluster_platform_instance": "Tenant cluster",
              "Hostname": "Node", "gpu": "GPU", "modelName": "Model",
              "DCGM_FI_DRIVER_VERSION": "Driver", "Value #B": "Utilization",
              "Value #C": "Tensor active", "Value #D": "Temperature", "Value #E": "Power",
              "Value #F": "Memory", "Value #G": "XID errors (1h)"}
    units = {"Utilization": "percent", "Tensor active": "percent", "Temperature": "celsius",
             "Power": "watt", "Memory": "decmbytes", "XID errors (1h)": "none"}
    overrides = [{"matcher": {"id": "byName", "options": n},
                  "properties": [{"id": "unit", "value": u}, {"id": "decimals", "value": 0 if u in ("decmbytes", "none") else 1}]}
                 for n, u in units.items()]
    for n in ("Utilization", "Tensor active"):
        overrides.append({"matcher": {"id": "byName", "options": n},
                          "properties": [{"id": "custom.cellOptions",
                                          "value": {"type": "gauge", "mode": "basic"}},
                                         {"id": "min", "value": 0}, {"id": "max", "value": 100}]})
    return {"id": next(_ids), "type": "table", "title": "GPU inventory",
            "description": "One row per GPU. Memory is the GPU's total framebuffer.",
            "datasource": DS, "gridPos": {"h": 9, "w": 24, "x": 0, "y": y},
            "targets": [target(e, ref=r, instant=True, fmt="table") for r, e in q],
            "transformations": [
                {"id": "merge", "options": {}},
                {"id": "organize", "options": {
                    "excludeByName": {"Time": True, "Value #A": True},
                    "indexByName": {"vcluster_platform_project": 0, "vcluster_platform_instance": 1,
                                    "Hostname": 2, "gpu": 3, "modelName": 4,
                                    "DCGM_FI_DRIVER_VERSION": 5},
                    "renameByName": rename}}],
            "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": overrides},
            "options": {"showHeader": True,
                        "sortBy": [{"displayName": "Tenant cluster", "desc": False}]}}


def var(name, label, query, all_value=".*", multi=True, hide=0):
    return {"name": name, "label": label, "type": "query", "datasource": DS, "hide": hide,
            "query": {"query": query, "refId": "StandardVariableQuery"}, "definition": query,
            "refresh": 2, "sort": 1, "multi": multi, "includeAll": True, "allValue": all_value,
            "current": {"selected": True, "text": ["All"], "value": ["$__all"]}}


def build():
    p, y = [], 0
    hot = [{"color": "green", "value": None}, {"color": "orange", "value": 80},
           {"color": "red", "value": 90}]
    p += [
        stat("Tenant clusters", f"count(count by (vcluster_platform_instance) ({m('DCGM_FI_DEV_GPU_UTIL')})) or vector(0)", 0, y,
             desc="Tenant clusters with at least one GPU in the selection."),
        stat("GPUs", f"count({m('DCGM_FI_DEV_GPU_UTIL')}) or vector(0)", 3, y),
        stat("Avg. GPU util.", f"avg({m('DCGM_FI_DEV_GPU_UTIL')})", 6, y, unit="percent", decimals=1),
        stat("Avg. tensor active", f"100 * avg({m('DCGM_FI_PROF_PIPE_TENSOR_ACTIVE')})", 9, y, unit="percent", decimals=1),
        stat("Total power", f"sum({m('DCGM_FI_DEV_POWER_USAGE')})", 12, y, unit="watt", decimals=0),
        stat("Max temperature", f"max({m('DCGM_FI_DEV_GPU_TEMP')})", 15, y, unit="celsius", decimals=0,
             thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 87}]),
        stat("GPU memory", f"sum({FB_TOTAL})", 18, y, unit="decmbytes", decimals=0,
             desc="Total framebuffer across the selected GPUs."),
        stat("XID errors (1h)", f"sum({XID_1H}) or vector(0)", 21, y, decimals=0,
             thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    ]
    y += 4
    p.append(inventory(y)); y += 9

    p.append(row("Utilization", y)); y += 1
    p += [
        ts("GPU utilization", [target(per_gpu(m("DCGM_FI_DEV_GPU_UTIL")), GPU_LEGEND)], 0, y,
           unit="percent", maxv=100),
        ts("Tensor core activity", [target(per_gpu(f"100 * {m('DCGM_FI_PROF_PIPE_TENSOR_ACTIVE')}"), GPU_LEGEND)],
           12, y, unit="percent", maxv=100, desc="Share of cycles the tensor pipe is active."),
    ]
    y += 8
    p += [
        ts("Graphics engine and memory activity", [
            target(f"100 * avg by (vcluster_platform_instance) ({m('DCGM_FI_PROF_GR_ENGINE_ACTIVE')})",
                   "{{vcluster_platform_instance}} graphics engine", "A"),
            target(f"100 * avg by (vcluster_platform_instance) ({m('DCGM_FI_PROF_DRAM_ACTIVE')})",
                   "{{vcluster_platform_instance}} memory interface", "B"),
            target(f"avg by (vcluster_platform_instance) ({m('DCGM_FI_DEV_MEM_COPY_UTIL')})",
                   "{{vcluster_platform_instance}} memory copy", "C"),
        ], 0, y, unit="percent", maxv=100, desc="Averages per tenant cluster."),
        ts("PCIe throughput", [
            target(per_gpu(m("DCGM_FI_PROF_PCIE_RX_BYTES")), GPU_LEGEND + " rx", "A"),
            target(f"-1 * {per_gpu(m('DCGM_FI_PROF_PCIE_TX_BYTES'))}", GPU_LEGEND + " tx", "B"),
        ], 12, y, unit="Bps", desc="Received above the axis, transmitted below."),
    ]
    y += 8

    p.append(row("Power and thermals", y)); y += 1
    p += [
        ts("Power draw", [target(per_gpu(m("DCGM_FI_DEV_POWER_USAGE")), GPU_LEGEND)], 0, y, unit="watt"),
        ts("GPU temperature", [target(per_gpu(m("DCGM_FI_DEV_GPU_TEMP")), GPU_LEGEND)], 12, y, unit="celsius"),
    ]
    y += 8

    p.append(row("By tenant cluster", y)); y += 1
    p += [
        bar("GPUs per tenant cluster",
            f"count by (vcluster_platform_instance) ({m('DCGM_FI_DEV_GPU_UTIL')})",
            "{{vcluster_platform_instance}}", 0, y),
        bar("Avg. GPU utilization per tenant cluster",
            f"avg by (vcluster_platform_instance) ({m('DCGM_FI_DEV_GPU_UTIL')})",
            "{{vcluster_platform_instance}}", 8, y, unit="percent"),
        bar("Power per tenant cluster",
            f"sum by (vcluster_platform_instance) ({m('DCGM_FI_DEV_POWER_USAGE')})",
            "{{vcluster_platform_instance}}", 16, y, unit="watt"),
    ]
    y += 7

    p.append(row("GPU health", y)); y += 1
    p += [
        ts("XID errors (1h)", [target(per_gpu(XID_1H), GPU_LEGEND)], 0, y, w=8, unit="none",
           desc="A rise here is what NVSentinel acts on."),
        ts("Remapped rows", [
            target(per_gpu(f"increase({m('DCGM_FI_DEV_UNCORRECTABLE_REMAPPED_ROWS_total')}[1h])"), GPU_LEGEND + " uncorrectable", "A"),
            target(per_gpu(f"increase({m('DCGM_FI_DEV_CORRECTABLE_REMAPPED_ROWS_total')}[1h])"), GPU_LEGEND + " correctable", "B"),
        ], 8, y, w=8, unit="none", desc="Memory rows remapped in the last hour."),
        ts("PCIe replays (1h)", [target(per_gpu(f"increase({m('DCGM_FI_DEV_PCIE_REPLAY_COUNTER_total')}[1h])"), GPU_LEGEND)],
           16, y, w=8, unit="none"),
    ]
    y += 8

    p.append(row("GPU nodes", y)); y += 1
    cpu_busy = (f"100 * (1 - sum by (vcluster_platform_instance, k8s_node_name) "
                f"(rate(system_cpu_time_seconds_total{{{NODE_SEL},state=\"idle\"}}[5m])) / "
                f"sum by (vcluster_platform_instance, k8s_node_name) "
                f"(rate(system_cpu_time_seconds_total{{{NODE_SEL}}}[5m])))")
    mem_used = (f"100 * sum by (vcluster_platform_instance, k8s_node_name) "
                f"(system_memory_usage_bytes{{{NODE_SEL},state=~\"used|buffered\"}}) / "
                f"sum by (vcluster_platform_instance, k8s_node_name) (system_memory_usage_bytes{{{NODE_SEL}}})")
    node_legend = "{{vcluster_platform_instance}} / {{k8s_node_name}}"
    p += [
        ts("Node CPU busy", [target(cpu_busy, node_legend)], 0, y, unit="percent", maxv=100,
           desc="Host CPU of each GPU node (for VM nodes, the guest)."),
        ts("Node memory used", [target(mem_used, node_legend)], 12, y, unit="percent", maxv=100),
    ]

    return {
        "uid": "vcluster-tenant-gpus",
        "title": "Tenant Cluster GPUs",
        "description": "GPUs per tenant cluster from DCGM, with project and tenant cluster selectors. "
                       "Uses only series that report real values on hardware and on nvml-mock.",
        "tags": ["vcluster", "fleet-observability", "gpu", "tenant-cluster", "environment-owned"],
        "editable": True, "graphTooltip": 1, "refresh": "30s", "schemaVersion": 41,
        "time": {"from": "now-1h", "to": "now"}, "timezone": "browser",
        "links": [
            {"title": "Fleet Overview", "type": "link", "url": "/d/vcluster-fleet-observability-operations",
             "keepTime": True, "targetBlank": False, "icon": "dashboard", "tags": [], "asDropdown": False,
             "includeVars": False, "tooltip": ""},
            {"title": "GPU Overview", "type": "link", "url": "/d/vcluster-gpu-overview",
             "keepTime": True, "targetBlank": False, "icon": "dashboard", "tags": [], "asDropdown": False,
             "includeVars": False, "tooltip": ""},
            {"title": "Cluster Operations", "type": "link", "url": "/d/vcluster-cluster-operations",
             "keepTime": True, "targetBlank": False, "icon": "dashboard", "tags": [], "asDropdown": False,
             "includeVars": False, "tooltip": ""},
        ],
        "templating": {"list": [
            # A datasource variable rather than a constant, defaulting to the fleet datasource:
            # a tenant-scoped datasource pins the project/instance headers, so the same
            # dashboard also serves a tenant looking at only its own clusters.
            {"name": "datasource", "label": "Datasource", "type": "datasource", "query": "prometheus",
             "hide": 2, "current": {"text": FLEET_DS, "value": FLEET_DS}, "regex": "", "refresh": 1},
            var("project", "Project",
                'label_values(DCGM_FI_DEV_GPU_UTIL{vcluster_platform_instance!=""}, vcluster_platform_project)'),
            var("tenant", "Tenant cluster",
                'label_values(DCGM_FI_DEV_GPU_UTIL{vcluster_platform_project=~"$project",vcluster_platform_instance!=""}, vcluster_platform_instance)',
                all_value=".+"),
            var("node", "Node",
                'label_values(DCGM_FI_DEV_GPU_UTIL{vcluster_platform_project=~"$project",vcluster_platform_instance=~"$tenant"}, Hostname)'),
            var("model", "GPU model",
                'label_values(DCGM_FI_DEV_GPU_UTIL{vcluster_platform_project=~"$project",vcluster_platform_instance=~"$tenant"}, modelName)'),
        ]},
        "panels": p,
    }


if __name__ == "__main__":
    out = pathlib.Path(__file__).parent / "dashboards" / "tenant-gpus.json"
    out.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {out}")
