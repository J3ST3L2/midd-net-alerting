#!/usr/bin/env python3
"""Generate the Grafana dashboards from one definition so panels stay consistent.

    python3 mist-dashboard/dashboards/build.py          # rewrite the JSON files
    python3 mist-dashboard/dashboards/build.py --check  # fail if committed JSON is out of date

Output goes to deploy/mist-dashboard/grafana/dashboards/ and is loaded by Grafana's file
provisioning, so Git stays the source of truth (UI edits are not persisted).
"""
import json
import os
import sys

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "mist-dashboard",
                   "grafana", "dashboards")
DS = {"type": "prometheus", "uid": "prometheus"}

GREEN, RED, AMBER, BLUE = "green", "red", "orange", "blue"
SITE = '{site=~"$site"}'


def target(expr, legend="", fmt="time_series", instant=False, ref="A"):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "format": fmt}
    if instant:
        t["instant"] = True
    return t


def steps(*pairs):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in pairs]}


def panel(kind, title, x, y, w, h, targets, desc="", unit=None, thresholds=None, options=None,
          overrides=None, custom=None, transformations=None, decimals=None, mappings=None, min_=None, max_=None, links=None):
    defaults = {"color": {"mode": "palette-classic"} if kind == "timeseries" else {"mode": "thresholds"}}
    if unit:
        defaults["unit"] = unit
    if thresholds:
        defaults["thresholds"] = thresholds
    if custom:
        defaults["custom"] = custom
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    if links:
        defaults["links"] = links
    p = {"type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
         "options": options or {}}
    if transformations:
        p["transformations"] = transformations
    return p


def stat(title, x, y, w, expr, desc="", unit="none", thresholds=None, color_mode="value", mappings=None):
    return panel("stat", title, x, y, w, 4, [target(expr, instant=True)], desc, unit,
                 thresholds or steps((BLUE, None)), mappings=mappings, decimals=0,
                 options={"colorMode": color_mode, "graphMode": "none", "textMode": "value",
                          "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}})


def timeseries(title, x, y, w, h, targets, desc="", unit="none", stack=False, max_=None, min_=0):
    custom = {"lineWidth": 2, "fillOpacity": 18 if stack else 8, "showPoints": "never",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    return panel("timeseries", title, x, y, w, h, targets, desc, unit, steps((GREEN, None)),
                 custom=custom, min_=min_, max_=max_,
                 options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                          "tooltip": {"mode": "multi", "sort": "desc"}})


def bargauge(title, x, y, w, h, expr, legend, desc="", unit="none", thresholds=None, max_=None, links=None):
    return panel("bargauge", title, x, y, w, h, [target(expr, legend, instant=True)], desc, unit,
                 thresholds or steps((BLUE, None)), min_=0, max_=max_, links=links,
                 options={"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                          "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}})


def table(title, x, y, w, h, targets, desc="", hide=(), rename=None, order=None, overrides=None, sort=None,
          merge=False):
    excl = {k: True for k in ("Time", "__name__", "job", "instance") + tuple(hide)}
    transform = ([{"id": "merge", "options": {}}] if merge else []) + [{"id": "organize", "options": {"excludeByName": excl, "renameByName": rename or {},
                                                "indexByName": {n: i for i, n in enumerate(order or [])}}}]
    return panel("table", title, x, y, w, h, targets, desc, thresholds=steps((GREEN, None)),
                 transformations=transform, overrides=overrides or [],
                 options={"showHeader": True, "cellHeight": "sm",
                          "sortBy": [{"displayName": sort, "desc": True}] if sort else []})


def _link(title, url):
    return {"title": title, "url": url, "targetBlank": False}


# Grafana prefixes these with the configured sub-path, so they work behind /mist-dashboard/.
SITE_FROM_SERIES = _link("Open site", "/d/mist-site?var-site=${__field.labels.site:percentencode}")
DEVICE_FROM_SERIES = _link("Open device", "/d/mist-device?var-device=${__field.labels.name:percentencode}")


def site_from_column(col):
    return _link("Open site", '/d/mist-site?var-site=${__data.fields["%s"]:percentencode}' % col)


def device_from_column(col):
    return _link("Open device", '/d/mist-device?var-device=${__data.fields["%s"]:percentencode}' % col)


def link_series(panels, title, link):
    """Click a bar or series in the named panel to open another dashboard."""
    next(p for p in panels if p["title"] == title)["fieldConfig"]["defaults"]["links"] = [link]


def link_columns(panels, title, links):
    """Click a cell in the named table to open another dashboard. links: {column: link}."""
    p = next(p for p in panels if p["title"] == title)
    p["fieldConfig"]["overrides"] += [
        {"matcher": {"id": "byName", "options": col}, "properties": [{"id": "links", "value": [lk]}]}
        for col, lk in links.items()]


def variable(name, label, query, multi=True, include_all=True):
    return {"name": name, "label": label, "type": "query", "datasource": DS, "refresh": 2,
            "query": {"query": query, "refId": "v"}, "includeAll": include_all,
            "multi": multi and include_all, "sort": 5,
            **({"allValue": ".*", "current": {"selected": True, "text": "All", "value": "$__all"}}
               if include_all else {})}


def dashboard(uid, title, panels, variables, desc):
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {"uid": uid, "title": title, "description": desc, "tags": ["mist"], "timezone": "browser",
            "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1,
            "refresh": "1m", "time": {"from": "now-6h", "to": "now"},
            "templating": {"list": variables}, "annotations": {"list": []}, "panels": panels,
            "links": [{"title": "Mist dashboards", "type": "dashboards", "tags": ["mist"],
                       "asDropdown": False, "includeVars": False, "keepTime": True}]}


SITE_VAR = variable("site", "Site", "label_values(mist_device_up, site)")
TYPE_VAR = variable("type", "Device type", "label_values(mist_device_up, type)")

FRESH = ('time() - min(mist_exporter_last_success_timestamp_seconds{source="devices"})')


def overview():
    down = 'mist_device_up{site=~"$site"} == 0'
    p = [
        stat("Devices", 0, 0, 4, 'count(mist_device_up%s)' % SITE, "APs, switches and gateways in the org."),
        stat("Connected", 4, 0, 4, 'sum(mist_device_up%s)' % SITE, thresholds=steps((GREEN, None))),
        stat("Disconnected", 8, 0, 4, 'count(%s) or vector(0)' % down,
             "Devices Mist reports as not connected.", thresholds=steps((GREEN, None), (RED, 1)),
             color_mode="background"),
        stat("Critical alarms (24 h)", 12, 0, 4,
             'sum(mist_alarms{site=~"$site",state="open",severity="critical"}) or vector(0)',
             "Unresolved critical alarm records in the last 24 hours. Device down/up pairs are not "
             "closed by Mist, so use Disconnected for current device state.",
             thresholds=steps((GREEN, None), (RED, 1))),
        stat("Wireless clients", 16, 0, 4, 'sum(mist_site_clients%s)' % SITE, thresholds=steps((BLUE, None))),
        stat("Data age", 20, 0, 4, FRESH, "Seconds since the exporter last refreshed device data from Mist.",
             unit="s", thresholds=steps((GREEN, None), (AMBER, 180), (RED, 600)), color_mode="background"),

        timeseries("Disconnected devices", 0, 4, 12, 8,
                   [target('count by (type) (%s) or vector(0)' % down, "{{type}}")],
                   "Count of devices Mist reports as not connected, by type.", stack=True),
        table("Disconnected now", 12, 4, 12, 8,
              [target('(time() - mist_device_last_seen_timestamp_seconds) and on(mac) (%s)' % down,
                      fmt="table", instant=True)],
              "Devices Mist currently reports as not connected, and how long since Mist last heard from them.",
              hide=("mac",), rename={"name": "Device", "site": "Site", "type": "Type", "model": "Model",
                                     "Value": "Down for"},
              order=["Device", "Site", "Type", "Model", "Down for"], sort="Down for",
              overrides=[{"matcher": {"id": "byName", "options": "Down for"},
                          "properties": [{"id": "unit", "value": "s"}]}]),

        bargauge("Devices by site", 0, 12, 8, 9,
                 'sort_desc(count by (site) (mist_device_up%s))' % SITE, "{{site}}",
                 "Total devices per site."),
        bargauge("Disconnected by site", 8, 12, 8, 9,
                 'sort_desc(count by (site) (%s))' % down, "{{site}}",
                 "Devices not connected, per site.", thresholds=steps((RED, None))),
        table("Open alarms (24 h)", 16, 12, 8, 9,
              [target('sort_desc(sum by (site, severity, type) (mist_alarms{site=~"$site",state="open"}))',
                      fmt="table", instant=True)],
              "Unresolved alarm records in the last 24 hours, by site, severity and type.",
              rename={"site": "Site", "severity": "Severity", "type": "Type", "Value": "Alarms"},
              order=["Site", "Severity", "Type", "Alarms"], hide=("group", "state"), sort="Alarms"),

        table("Firmware", 0, 21, 12, 8,
              [target('count by (type, model, version) (mist_device_info%s)' % SITE, fmt="table", instant=True)],
              "Device count per model and firmware version, to spot stragglers after an upgrade.",
              hide=("site", "name", "mac", "ip"),
              rename={"type": "Type", "model": "Model", "version": "Firmware", "Value": "Devices"},
              order=["Type", "Model", "Firmware", "Devices"], sort="Devices"),
        timeseries("Alarms by severity (24 h window)", 12, 21, 12, 8,
                   [target('sum by (severity) (mist_alarms{site=~"$site"})', "{{severity}}")],
                   "Alarm records in the trailing 24 hour window, open and resolved.", stack=True),
    ]
    link_series(p, "Devices by site", SITE_FROM_SERIES)
    link_series(p, "Disconnected by site", SITE_FROM_SERIES)
    link_columns(p, "Disconnected now", {"Device": device_from_column("Device"), "Site": site_from_column("Site")})
    link_columns(p, "Open alarms (24 h)", {"Site": site_from_column("Site")})
    return dashboard("mist-overview", "Mist Overview", p, [SITE_VAR],
                     "Fleet health and alarms across the Juniper Mist org.")


BREAKDOWN_NOTE = ("Clients seen in the last 30 minutes, so totals run a little above the Clients tile "
                  "(which counts clients associated right now).")


def wireless():
    clients = 'mist_ap_clients%s' % SITE
    aps = 'mist_device_up{type="ap",site=~"$site"}'
    p = [
        stat("Clients", 0, 0, 6, 'sum(mist_site_clients%s)' % SITE, "Wireless clients associated now, from Mist site stats."),
        stat("Access points", 6, 0, 6, 'count(%s)' % aps),
        stat("APs offline", 12, 0, 6, 'count(%s == 0) or vector(0)' % aps,
             thresholds=steps((GREEN, None), (RED, 1)), color_mode="background"),
        stat("Busiest AP", 18, 0, 6, 'max(%s)' % clients,
             "Highest client count on a single AP. Per-AP counts cover the busiest ~1000 APs."),

        timeseries("Clients over time", 0, 4, 16, 9,
                   [target('mist_site_clients%s' % SITE, "{{site}}")], "Wireless clients per site.", stack=True),
        timeseries("Clients by band", 16, 4, 8, 9,
                   [target('sum by (band) (mist_site_clients_by_band%s)' % SITE, "{{band}}")],
                   BREAKDOWN_NOTE, stack=True),

        bargauge("Busiest APs", 0, 13, 12, 10, 'topk(15, %s)' % clients, "{{name}} ({{site}})",
                 "The 15 APs with the most clients right now.",
                 thresholds=steps((BLUE, None), (AMBER, 40), (RED, 60))),
        bargauge("Clients by site", 12, 13, 12, 10, 'sort_desc(mist_site_clients%s)' % SITE, "{{site}}",
                 "Current clients per site."),

        bargauge("Clients by SSID", 0, 23, 12, 8,
                 'sort_desc(sum by (ssid) (mist_site_clients_by_ssid%s))' % SITE, "{{ssid}}", BREAKDOWN_NOTE),
        table("Offline access points", 12, 23, 12, 8,
              [target('(time() - mist_device_last_seen_timestamp_seconds) and on(mac) (%s == 0)' % aps,
                      fmt="table", instant=True)],
              "Access points Mist reports as not connected, and how long since Mist last heard from them.",
              hide=("mac", "type"), rename={"name": "AP", "site": "Site", "model": "Model", "Value": "Down for"},
              order=["AP", "Site", "Model", "Down for"], sort="Down for",
              overrides=[{"matcher": {"id": "byName", "options": "Down for"},
                          "properties": [{"id": "unit", "value": "s"}]}]),
    ]
    link_series(p, "Busiest APs", DEVICE_FROM_SERIES)
    link_series(p, "Clients by site", SITE_FROM_SERIES)
    link_columns(p, "Offline access points", {"AP": device_from_column("AP"), "Site": site_from_column("Site")})
    return dashboard("mist-wireless", "Mist Wireless", p, [SITE_VAR], "Wireless client load across the Mist org.")


def load():
    sel = '{site=~"$site",type=~"$type"}'
    p = [
        timeseries("CPU by site (average)", 0, 0, 12, 8,
                   [target('avg by (site) (mist_device_cpu_percent%s)' % sel, "{{site}}")],
                   "Mean CPU utilization of the selected device types, per site.", unit="percent", max_=100),
        timeseries("Memory by site (average)", 12, 0, 12, 8,
                   [target('avg by (site) (mist_device_memory_percent%s)' % sel, "{{site}}")],
                   "Mean memory utilization of the selected device types, per site.", unit="percent", max_=100),
        bargauge("Highest CPU", 0, 8, 12, 9, 'topk(10, mist_device_cpu_percent%s)' % sel, "{{name}} ({{site}})",
                 "Ten busiest devices by CPU.", unit="percent", max_=100,
                 thresholds=steps((GREEN, None), (AMBER, 70), (RED, 90))),
        bargauge("Highest memory", 12, 8, 12, 9, 'topk(10, mist_device_memory_percent%s)' % sel,
                 "{{name}} ({{site}})", "Ten devices with the most memory in use.", unit="percent", max_=100,
                 thresholds=steps((GREEN, None), (AMBER, 80), (RED, 92))),
        bargauge("Recently restarted", 0, 17, 12, 9,
                 'bottomk(10, mist_device_uptime_seconds%s)' % sel, "{{name}} ({{site}})",
                 "Devices with the shortest uptime, i.e. the most recent reboots.", unit="s",
                 thresholds=steps((AMBER, None), (GREEN, 3600))),
        bargauge("PoE load", 12, 17, 12, 9,
                 '100 * mist_switch_poe_draw_watts%s / mist_switch_poe_budget_watts%s' % (sel, sel),
                 "{{name}} ({{site}})",
                 "PoE power drawn as a share of the switch budget. Only switches that report PoE appear.",
                 unit="percent", max_=100, thresholds=steps((GREEN, None), (AMBER, 75), (RED, 90))),
    ]
    for title in ("Highest CPU", "Highest memory", "Recently restarted", "PoE load"):
        link_series(p, title, DEVICE_FROM_SERIES)
    return dashboard("mist-load", "Mist Device Load", p, [SITE_VAR, TYPE_VAR],
                     "CPU, memory, uptime and PoE load of Mist-managed devices.")


UP_MAP = [{"type": "value", "options": {"1": {"text": "Up", "color": GREEN},
                                          "0": {"text": "Down", "color": RED}}}]


def site_view():
    s = '{site="$site"}'
    by = "name, type, model, mac"
    # One instant query per column; `merge` joins them into one row per device.
    cols = [("A", 'max by (%s) (mist_device_up%s)' % (by, s)),
            ("B", 'max by (%s) (mist_device_uptime_seconds%s)' % (by, s)),
            ("C", 'max by (%s) (mist_ap_clients%s)' % (by, s)),
            ("D", 'max by (%s) (mist_device_cpu_percent%s)' % (by, s)),
            ("E", 'max by (%s) (mist_device_memory_percent%s)' % (by, s)),
            ("F", 'max by (%s, version) (mist_device_info%s)' % (by, s))]
    pct = [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 0}]
    devices = table(
        "Devices", 0, 4, 24, 12,
        [target(e, fmt="table", instant=True, ref=r) for r, e in cols],
        "Every device at this site. Click a name to open it.", merge=True, sort="Status",
        hide=("mac",),
        rename={"name": "Device", "type": "Type", "model": "Model", "version": "Firmware",
                "Value #A": "Status", "Value #B": "Uptime", "Value #C": "Clients",
                "Value #D": "CPU", "Value #E": "Memory"},
        order=["Device", "Type", "Model", "Status", "Clients", "CPU", "Memory", "Uptime", "Firmware"],
        overrides=[
            {"matcher": {"id": "byName", "options": "Status"},
             "properties": [{"id": "mappings", "value": UP_MAP},
                            {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
            {"matcher": {"id": "byName", "options": "Uptime"}, "properties": [{"id": "unit", "value": "s"}]},
            {"matcher": {"id": "byName", "options": "CPU"}, "properties": pct},
            {"matcher": {"id": "byName", "options": "Memory"}, "properties": pct}])
    p = [
        stat("Devices", 0, 0, 5, 'count(mist_device_up%s)' % s),
        stat("Disconnected", 5, 0, 5, 'count(mist_device_up%s == 0) or vector(0)' % s,
             thresholds=steps((GREEN, None), (RED, 1)), color_mode="background"),
        stat("Wireless clients", 10, 0, 5, 'sum(mist_site_clients%s)' % s),
        stat("Critical alarms (24 h)", 15, 0, 5,
             'sum(mist_alarms{site="$site",state="open",severity="critical"}) or vector(0)',
             thresholds=steps((GREEN, None), (RED, 1))),
        stat("Access points", 20, 0, 4, 'count(mist_device_up{site="$site",type="ap"})'),
        devices,
        timeseries("Clients per AP", 0, 16, 12, 8,
                   [target('mist_ap_clients%s' % s, "{{name}}")], "Wireless clients on each AP at this site (covers the busiest ~1000 APs org-wide).",
                   stack=True),
        timeseries("CPU", 12, 16, 12, 8, [target('mist_device_cpu_percent%s' % s, "{{name}}")],
                   "CPU utilization of every device at this site.", unit="percent", max_=100),
        bargauge("Clients by SSID", 0, 24, 12, 8,
                 'sort_desc(mist_site_clients_by_ssid%s)' % s, "{{ssid}}", BREAKDOWN_NOTE),
        timeseries("Clients by band", 12, 24, 12, 8,
                   [target('mist_site_clients_by_band%s' % s, "{{band}}")], BREAKDOWN_NOTE, stack=True),
        table("Open alarms (24 h)", 0, 32, 24, 8,
              [target('sort_desc(sum by (severity, type) (mist_alarms{site="$site",state="open"}))',
                      fmt="table", instant=True)],
              "Unresolved alarm records for this site in the last 24 hours.",
              rename={"severity": "Severity", "type": "Type", "Value": "Alarms"},
              hide=("site", "group", "state"), order=["Severity", "Type", "Alarms"], sort="Alarms"),
    ]
    link_columns(p, "Devices", {"Device": device_from_column("Device")})
    return dashboard("mist-site", "Mist Site", p,
                     [variable("site", "Site", "label_values(mist_device_up, site)", include_all=False)],
                     "One site: its devices, clients, load and alarms.")


def device_view():
    d = '{name="$device"}'
    p = [
        stat("Status", 0, 0, 4, 'max(mist_device_up%s)' % d, mappings=UP_MAP, color_mode="background",
             thresholds=steps((RED, None), (GREEN, 1))),
        stat("Uptime", 4, 0, 4, 'max(mist_device_uptime_seconds%s)' % d, unit="s"),
        stat("Clients", 8, 0, 4, 'sum(mist_ap_clients%s)' % d, "Access points only."),
        stat("CPU", 12, 0, 4, 'max(mist_device_cpu_percent%s)' % d, unit="percent",
             thresholds=steps((GREEN, None), (AMBER, 70), (RED, 90))),
        stat("Memory", 16, 0, 4, 'max(mist_device_memory_percent%s)' % d, unit="percent",
             thresholds=steps((GREEN, None), (AMBER, 80), (RED, 92))),
        stat("Last seen", 20, 0, 4, '(time() - max(mist_device_last_seen_timestamp_seconds%s)) * 1000' % d,
             "Time since Mist last heard from the device.", unit="dtdurations"),

        table("Identity", 0, 4, 24, 3,
              [target('mist_device_info%s' % d, fmt="table", instant=True)],
              hide=("Value",), rename={"name": "Device", "site": "Site", "type": "Type", "model": "Model",
                                       "version": "Firmware", "ip": "IP", "mac": "MAC"},
              order=["Device", "Site", "Type", "Model", "Firmware", "IP", "MAC"]),

        panel("state-timeline", "Connection state", 0, 7, 24, 4, [target('max(mist_device_up%s)' % d, "state")],
              "Up/down history for this device.", thresholds=steps((RED, None), (GREEN, 1)), mappings=UP_MAP,
              options={"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                       "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}),
        timeseries("CPU", 0, 11, 12, 8, [target('mist_device_cpu_percent%s' % d, "CPU")],
                   unit="percent", max_=100),
        timeseries("Memory", 12, 11, 12, 8, [target('mist_device_memory_percent%s' % d, "Memory")],
                   unit="percent", max_=100),
        timeseries("Clients", 0, 19, 12, 8, [target('mist_ap_clients%s' % d, "clients")],
                   "Access points only. Missing when the AP is not among the busiest ~1000."),
        timeseries("Uptime", 12, 19, 12, 8, [target('mist_device_uptime_seconds%s' % d, "uptime")],
                   "A drop to zero is a reboot.", unit="s"),
        timeseries("PoE draw", 0, 27, 24, 8,
                   [target('mist_switch_poe_draw_watts%s' % d, "drawn"),
                    target('mist_switch_poe_budget_watts%s' % d, "budget", ref="B")],
                   "Switches that report PoE only.", unit="watt"),
    ]
    link_columns(p, "Identity", {"Site": _link("Open site",
                                                '/d/mist-site?var-site=${__data.fields["Site"]:percentencode}')})
    return dashboard("mist-device", "Mist Device", p,
                     [variable("device", "Device", "label_values(mist_device_up, name)", include_all=False)],
                     "One device: state, load, clients and history.")


def build():
    return {"mist-overview.json": overview(), "mist-wireless.json": wireless(), "mist-load.json": load(),
            "mist-site.json": site_view(), "mist-device.json": device_view()}


def render(d):
    return json.dumps(d, indent=2, sort_keys=True) + "\n"


def main(argv):
    out = os.path.abspath(OUT)
    stale = []
    for name, d in build().items():
        path = os.path.join(out, name)
        text = render(d)
        if "--check" in argv:
            if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
                stale.append(name)
        else:
            os.makedirs(out, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
    if stale:
        sys.exit("out of date, run build.py: " + ", ".join(stale))


if __name__ == "__main__":
    main(sys.argv[1:])
