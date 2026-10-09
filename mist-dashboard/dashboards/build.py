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
          overrides=None, custom=None, transformations=None, decimals=None, mappings=None, min_=None, max_=None, links=None,
          no_value=None):
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
    if no_value:
        defaults["noValue"] = no_value
    p = {"type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
         "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
         "options": options or {}}
    if transformations:
        p["transformations"] = transformations
    return p


def stat(title, x, y, w, expr, desc="", unit="none", thresholds=None, color_mode="value", mappings=None,
         no_value=None):
    return panel("stat", title, x, y, w, 4, [target(expr, instant=True)], desc, unit,
                 thresholds or steps((BLUE, None)), mappings=mappings, decimals=0, no_value=no_value,
                 options={"colorMode": color_mode, "graphMode": "none", "textMode": "value",
                          "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}})


def timeseries(title, x, y, w, h, targets, desc="", unit="none", stack=False, max_=None, min_=0):
    custom = {"lineWidth": 2, "fillOpacity": 18 if stack else 8, "showPoints": "never",
              "stacking": {"mode": stack if isinstance(stack, str) else ("normal" if stack else "none"),
                           "group": "A"}}
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


def dashboard(uid, title, panels, variables, desc, time_from="now-6h", tags=("mist",)):
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {"uid": uid, "title": title, "description": desc, "tags": list(tags), "timezone": "browser",
            "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1,
            "refresh": "1m", "time": {"from": time_from, "to": "now"},
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


PN_FAILURE_DEF = ("Counted failures are PantherNet auth-failure events with reason 23 (802.1X failed, or "
                  "'802.1x Auth Fail' text) or reason 15 (4-way handshake timeout). Other reasons are shown "
                  "separately and not counted.")
PN_FALLBACK_DEF = ("A client falls back when a counted PantherNet failure is followed by a MiddleburyCollege "
                   "connection by the same username within 30 minutes. It is judged only after those 30 minutes "
                   "have passed, so this lags by that long. "
                   "Clients with no username are excluded.")


LOADING = "Loading history"
LOADING_NOTE = (" Blank ('Loading history') until the last 24 hours have been read after a restart, so a restart never shows a false low number.")


PN_TREND_NOTE = ("Each point is the trailing 24-hour value at that moment. The dashboard opens on 7 days; "
                 "widen the time picker to look further back (Prometheus keeps 180 days, building from when the "
                 "exporter first ran). Counts are held back while a restart reloads the history.")


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


PN_REASON_NAMES = (
    ("dot1x_failed", "Couldn't complete 802.1X sign-in"),
    ("handshake_timeout", "Security handshake timed out"),
    ("previous_auth_invalid", "Old session expired (not counted)"),
    ("client_left", "Device walked away (not counted)"),
    ("tx_failure", "Radio transmission problem (not counted)"),
    ("other", "Other (not counted)"),
)


def _plain_reasons(expr):
    """Rename the reason label values to readable text (the metric keeps the stable names)."""
    for raw, text in PN_REASON_NAMES:
        expr = 'label_replace(%s, "reason", "%s", "reason", "%s")' % (expr, text, raw)
    return expr


def pn_adoption(y):
    """PantherNet adoption: how many devices use it, who fails to sign in, who falls back to MiddleburyCollege."""
    top = y + 1
    pn, mc = 'sum(mist_pn_unique_clients{ssid="PantherNet"})', 'sum(mist_pn_unique_clients{ssid="MiddleburyCollege"})'
    share = "100 * %s / (%s + %s)" % (pn, pn, mc)
    p = [
        row("PantherNet adoption", y),

        stat("Devices on PantherNet", 0, top, 5, pn,
             "Unique devices that connected to PantherNet in the last 24 hours."),
        stat("PantherNet share", 5, top, 5, share,
             "Of the devices on PantherNet or MiddleburyCollege in the last 24 hours, the percentage on "
             "PantherNet. This is the adoption number.", unit="percent"),
        stat("Users who failed to sign in", 10, top, 5, "mist_pn_auth_failure_clients",
             "Unique users with at least one failed PantherNet sign-in in the last 24 hours. " + PN_FAILURE_DEF + LOADING_NOTE,
             thresholds=steps((GREEN, None), (AMBER, 1)), no_value=LOADING),
        stat("...who then used MiddleburyCollege", 15, top, 5, 'mist_pn_fallback_rate{match="user"}',
             "Of the failing users old enough to judge, the percentage who connected to MiddleburyCollege "
             "within 30 minutes of failing. " + PN_FALLBACK_DEF + LOADING_NOTE, unit="percentunit",
             thresholds=steps((GREEN, None), (AMBER, 0.25), (RED, 0.5)), no_value=LOADING),
        stat("Users judged", 20, top, 4, 'sum(mist_pn_fallback_eligible_clients{match="user"})',
             "Failing users old enough (30+ minutes) to judge, whose MiddleburyCollege connections were checked: "
             "the base for the percentage beside it." + LOADING_NOTE, no_value=LOADING),

        timeseries("PantherNet share of devices over time", 0, top + 4, 12, 8,
                   [target(share, "PantherNet share")],
                   "How the adoption percentage moves. Read it as: a line going up means more devices are choosing "
                   "PantherNet. " + PN_TREND_NOTE, unit="percent", min_=None),
        timeseries("Users who failed to sign in, and how many fell back", 12, top + 4, 12, 8,
                   [target("mist_pn_auth_failure_clients", "Users who failed to sign in"),
                    target('mist_pn_fallback_clients{match="user"}', "...of those, used MiddleburyCollege", ref="B")],
                   "The upper line is people who could not sign in to PantherNet; the lower line is how many of "
                   "them ended up on MiddleburyCollege instead. The gap is people who failed and did not "
                   "connect, or connected after more than 30 minutes. " + PN_TREND_NOTE),

        bargauge("Why PantherNet sign-ins fail (24 h)", 0, top + 12, 12, 8,
                 "sort_desc(%s)" % _plain_reasons("mist_pn_failure_reason_events"), "{{reason}}",
                 "Rejected PantherNet attempts in the last 24 hours by reason. Only the first two reasons "
                 "count as failures; the rest are shown so you can see what is being ignored. These are "
                 "attempts, not people, and one failing device retries many times."),
        timeseries("Share of failing users who fell back", 12, top + 12, 12, 8,
                   [target('mist_pn_fallback_rate{match="user"}', "Fell back to MiddleburyCollege")],
                   "Of the users who failed to sign in to PantherNet (and are old enough to judge), the percentage "
                   "who then connected to MiddleburyCollege. A line going down means fewer failures end up on the "
                   "old network. " + PN_TREND_NOTE, unit="percentunit", min_=None),

        stat("Data age", 0, top + 20, 6, 'time() - mist_exporter_last_success_timestamp_seconds{source="fallback"}',
             "Seconds since this data last refreshed from Mist.", unit="s",
             thresholds=steps((GREEN, None), (AMBER, 600), (RED, 1800)), color_mode="background"),
        stat("History", 6, top + 20, 6, "mist_pn_backfill_complete",
             "Loading until the last 24 hours and their username lookups are fetched (after a restart this takes "
             "hours). The failure and fallback numbers are blank until it reads Loaded, so a restart never "
             "draws a false dip in the trend.",
             mappings=[{"type": "value", "options": {"1": {"text": "Loaded", "color": GREEN},
                                                      "0": {"text": "Loading", "color": AMBER}}}],
             thresholds=steps((AMBER, None), (GREEN, 1)), color_mode="background"),
        stat("Lookups pending", 12, top + 20, 6, "sum(mist_pn_lookups_pending)",
             "Devices and users still waiting for a Mist lookup. Near zero once History is Loaded."),
        stat("Failed attempts (24 h)", 18, top + 20, 6, "mist_pn_auth_failure_events",
             "Counted failed sign-in attempts. Much larger than the number of users, because a device that "
             "cannot sign in keeps retrying."),
    ]
    # Added after the trend note is applied: this one is progress, not a trailing-24-hour value.
    p.append(timeseries("Lookups pending over time", 0, top + 24, 24, 6,
                        [target("sum by (stage) (mist_pn_lookups_pending)", "{{stage}}")],
                        "Only interesting while History says Loading. 'username' is failing devices whose user is "
                        "not yet known, 'connections' is users whose MiddleburyCollege connections are not yet "
                        "checked. A falling line means the history is loading; a flat line means it is stalled."))
    os_note = ("Mist buildings only: devices seen in the last 30 minutes, by operating system. Versions are folded "
               "into one short list; 'Unknown' is a device Mist could not identify.")
    p += [bargauge("Operating systems on PantherNet", 0, top + 30, 12, 9,
                   'sort_desc(mist_ssid_os_clients{ssid="PantherNet"})', "{{os}}", os_note),
          bargauge("Operating systems on MiddleburyCollege", 12, top + 30, 12, 9,
                   'sort_desc(mist_ssid_os_clients{ssid="MiddleburyCollege"})', "{{os}}",
                   os_note + " The old network: which kinds of device have not moved yet.")]
    return p


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
    p += pn_adoption(max(x["gridPos"]["y"] + x["gridPos"]["h"] for x in p))
    link_series(p, "Busiest APs", DEVICE_FROM_SERIES)
    link_series(p, "Clients by site", SITE_FROM_SERIES)
    link_columns(p, "Offline access points", {"AP": device_from_column("AP"), "Site": site_from_column("Site")})
    return dashboard("mist-wireless", "Mist Wireless", p, [SITE_VAR], "Wireless client load across the Mist org.",
                     time_from="now-7d")


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


SLE_METRICS = (("coverage", "Coverage"), ("capacity", "Capacity"), ("time-to-connect", "Time to connect"),
               ("roaming", "Roaming"), ("throughput", "Throughput"))
SLE_NOTE = ("Share of clients Mist scored as degraded for this metric over the last 24 hours (Mist SLE). "
            "Lower is better.")


def _pct_affected(metric, by=None):
    """Percent of clients affected: impacted / total, optionally grouped by a label."""
    grp = "sum by (%s)" % by if by else "sum"
    sel = 'mist_site_sle_users{metric="%s",site=~"$site",state="%%s"}' % metric
    return "100 * %s(%s) / %s(%s)" % (grp, sel % "impacted", grp, sel % "total")


def wifi_view():
    cell = lambda col: {"matcher": {"id": "byName", "options": col}, "properties": [
        {"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1},
        {"id": "thresholds", "value": steps((GREEN, None), (AMBER, 5), (RED, 15))},
        {"id": "custom.cellOptions", "value": {"type": "color-background"}}]}
    widths = [5, 5, 5, 5, 4]
    p = []
    x = 0
    for (metric, label), w in zip(SLE_METRICS, widths):
        p.append(stat(label, x, 0, w, _pct_affected(metric), SLE_NOTE, unit="percent",
                      thresholds=steps((GREEN, None), (AMBER, 5), (RED, 15)), color_mode="background"))
        x += w
    for pn in p:
        pn["fieldConfig"]["defaults"]["decimals"] = 1
    p += [
        table("Clients affected by site (%)", 0, 4, 24, 8,
              [target(_pct_affected(m, "site"), fmt="table", instant=True, ref=chr(65 + i))
               for i, (m, _) in enumerate(SLE_METRICS)],
              SLE_NOTE + " Click a site for its devices.", merge=True, sort="Coverage",
              rename={"site": "Site", **{"Value #%s" % chr(65 + i): l for i, (_, l) in enumerate(SLE_METRICS)}},
              order=["Site"] + [l for _, l in SLE_METRICS],
              overrides=[cell(l) for _, l in SLE_METRICS]),
        timeseries("Coverage: clients affected", 0, 12, 12, 8,
                   [target(_pct_affected("coverage", "site"), "{{site}}")],
                   "Weak signal and uplink/downlink asymmetry. " + SLE_NOTE, unit="percent"),
        timeseries("Capacity: clients affected", 12, 12, 12, 8,
                   [target(_pct_affected("capacity", "site"), "{{site}}")],
                   "Too many clients or too little airtime. " + SLE_NOTE, unit="percent"),

        table("APs behind coverage problems", 0, 20, 12, 10,
              [target('topk(20, mist_ap_sle_degraded_ratio{metric="coverage",site=~"$site"})',
                      fmt="table", instant=True)],
              "Worst 25 APs per site by share of degraded coverage samples. Click an AP to open it.",
              hide=("mac", "metric"), rename={"name": "AP", "site": "Site", "Value": "Degraded"},
              order=["AP", "Site", "Degraded"], sort="Degraded",
              overrides=[{"matcher": {"id": "byName", "options": "Degraded"},
                          "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 0}]}]),
        table("APs behind capacity problems", 12, 20, 12, 10,
              [target('topk(20, mist_ap_sle_degraded_ratio{metric="capacity",site=~"$site"})',
                      fmt="table", instant=True)],
              "Worst 25 APs per site by share of degraded capacity samples. Click an AP to open it.",
              hide=("mac", "metric"), rename={"name": "AP", "site": "Site", "Value": "Degraded"},
              order=["AP", "Site", "Degraded"], sort="Degraded",
              overrides=[{"matcher": {"id": "byName", "options": "Degraded"},
                          "properties": [{"id": "unit", "value": "percentunit"}, {"id": "decimals", "value": 0}]}]),

        table("APs that dropped offline (24 h)", 0, 30, 12, 9,
              [target('sort_desc(changes(mist_device_up{type="ap",site=~"$site"}[24h]) > 0)',
                      fmt="table", instant=True)],
              "Times each AP's connection state changed in the last 24 hours. Repeated changes mean flapping.",
              hide=("mac", "type", "model"), rename={"name": "AP", "site": "Site", "Value": "Changes"},
              order=["AP", "Site", "Changes"], sort="Changes"),
        bargauge("Recently restarted APs", 12, 30, 12, 9,
                 'bottomk(10, mist_device_uptime_seconds{type="ap",site=~"$site"})', "{{name}} ({{site}})",
                 "APs with the shortest uptime, i.e. the most recent reboots.", unit="s",
                 thresholds=steps((AMBER, None), (GREEN, 3600))),

        timeseries("Client share by band", 0, 39, 24, 8,
                   [target('sum by (band) (mist_site_clients_by_band%s)' % SITE, "{{band}}")],
                   "Each band's share of clients (stacked to 100%). A large 2.4 GHz share usually means "
                   "clients are not reaching 5 GHz. " + BREAKDOWN_NOTE, stack="percent", max_=None),
    ]
    link_columns(p, "Clients affected by site (%)", {"Site": site_from_column("Site")})
    for title in ("APs behind coverage problems", "APs behind capacity problems", "APs that dropped offline (24 h)"):
        link_columns(p, title, {"AP": device_from_column("AP"), "Site": site_from_column("Site")})
    link_series(p, "Recently restarted APs", DEVICE_FROM_SERIES)
    return dashboard("mist-wifi", "Mist Wifi Troubleshooting", p, [SITE_VAR],
                     "Where wifi is degraded: client experience by site, the APs behind it, and AP stability.")


ARUBA_NOTE = ("Counted from the Aruba controllers' own client tables every 2 minutes, one device per MAC. "
              "A controller that stops answering keeps its last count for 6 minutes, then drops out and shows "
              "as unreachable.")
ARUBA_PN, ARUBA_MC = 'sum(aruba_clients{ssid="PantherNet"})', 'sum(aruba_clients{ssid="MiddleburyCollege"})'
ARUBA_SHARE = "100 * %s / (%s + %s)" % (ARUBA_PN, ARUBA_PN, ARUBA_MC)
ARUBA_AGE = 'time() - min(mist_exporter_last_success_timestamp_seconds{source="aruba"})'


def aruba_view():
    both = 'ssid=~"PantherNet|MiddleburyCollege"'
    p = [
        stat("Devices on Aruba Wi-Fi", 0, 0, 5, "sum(aruba_clients)",
             "Wireless devices connected to the Aruba controllers right now, all SSIDs. " + ARUBA_NOTE),
        stat("Devices on PantherNet", 5, 0, 5, ARUBA_PN, "Devices connected to PantherNet on the Aruba side right now."),
        stat("PantherNet share", 10, 0, 5, ARUBA_SHARE,
             "Of the devices on PantherNet or MiddleburyCollege right now, the percentage on PantherNet. "
             "This is the Aruba side of the adoption number.", unit="percent"),
        stat("People on both networks", 15, 0, 5, "aruba_people_on_both_ssids",
             "People (counted by username) with a device on PantherNet and another on MiddleburyCollege at this "
             "moment. Often someone who has moved some devices over and not others."),
        stat("Controllers unreachable", 20, 0, 4, "count(aruba_controller_up == 0) or vector(0)",
             "Controllers that did not answer the last poll. Their last count is kept for 6 minutes.",
             thresholds=steps((GREEN, None), (RED, 1)), color_mode="background"),

        timeseries("Devices by SSID over time", 0, 4, 12, 9, [target("aruba_clients", "{{ssid}}")],
                   "Devices connected, per SSID. " + ARUBA_NOTE, stack=True),
        timeseries("PantherNet share over time", 12, 4, 12, 9, [target(ARUBA_SHARE, "PantherNet share")],
                   "The adoption percentage as it moves through the day: a line going up means more devices are on "
                   "PantherNet. It is a snapshot of who is connected, so it follows the school calendar.",
                   unit="percent", min_=None),

        bargauge("Who is on PantherNet and MiddleburyCollege", 0, 13, 12, 9,
                 "sort_desc(aruba_ssid_role_clients{%s})" % both, "{{ssid}} - {{role}}",
                 "Devices by the role the controller gave them. PantherNet roles say how the device is managed; "
                 "MiddleburyCollege roles say who the person is. AD-FAIL-THRU is a device that signed in but whose "
                 "directory lookup failed."),
        timeseries("People on each network", 12, 13, 12, 9,
                   [target('aruba_ssid_people{%s}' % both, "{{ssid}}"),
                    target("aruba_people_on_both_ssids", "On both", ref="B")],
                   "Distinct people (not devices) on each network, and how many are on both. One person with a "
                   "phone and a laptop counts once."),

        bargauge("Device types on PantherNet", 0, 22, 12, 9,
                 'sort_desc(aruba_ssid_device_clients{ssid="PantherNet"})', "{{device_type}}",
                 "What the controller says the device is. 'unknown' is a device it could not identify."),
        bargauge("Device types on MiddleburyCollege", 12, 22, 12, 9,
                 'sort_desc(aruba_ssid_device_clients{ssid="MiddleburyCollege"})', "{{device_type}}",
                 "The same view for the old network: where the devices that have not moved are coming from."),

        timeseries("Radio band by network", 0, 31, 12, 8,
                   [target('sum by (ssid, band) (aruba_ssid_band_clients{%s})' % both, "{{ssid}} {{band}}")],
                   "Devices on 2.4 GHz and 5 GHz, per network.", stack=True),
        bargauge("Devices per controller", 12, 31, 12, 8,
                 "sort_desc(sum by (controller) (aruba_controller_clients))", "{{controller}}",
                 "How the wireless load is spread across the controllers."),

        table("Controllers", 0, 39, 24, 8,
              [target("aruba_controller_up", fmt="table", instant=True),
               target("aruba_controller_users", fmt="table", instant=True, ref="B"),
               target("aruba_controller_poll_seconds", fmt="table", instant=True, ref="C"),
               target("aruba_controller_data_age_seconds", fmt="table", instant=True, ref="D")],
              "Each controller's last poll. 'Reachable' is whether it answered the last time; 'Data age' is how old "
              "the numbers being counted are.", merge=True,
              rename={"controller": "Controller", "Value #A": "Reachable", "Value #B": "Devices",
                      "Value #C": "Poll time", "Value #D": "Data age"},
              order=["Controller", "Reachable", "Devices", "Poll time", "Data age"], sort="Devices",
              overrides=[
                  {"matcher": {"id": "byName", "options": "Reachable"}, "properties": [
                      {"id": "mappings", "value": [{"type": "value", "options": {
                          "1": {"text": "Yes", "color": GREEN}, "0": {"text": "No", "color": RED}}}]},
                      {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                  {"matcher": {"id": "byName", "options": "Poll time"}, "properties": [{"id": "unit", "value": "s"}]},
                  {"matcher": {"id": "byName", "options": "Data age"}, "properties": [{"id": "unit", "value": "s"}]}]),
        stat("Data age", 0, 47, 6, ARUBA_AGE, "Seconds since the Aruba counts last refreshed.", unit="s",
             thresholds=steps((GREEN, None), (AMBER, 300), (RED, 900)), color_mode="background"),
    ]
    return dashboard("aruba-wireless", "Aruba Wireless", p, [],
                     "Aruba client counts and PantherNet adoption, read from the Aruba controllers.",
                     time_from="now-7d", tags=("mist", "aruba"))


def _z(expr):
    """Treat a side with no data yet as zero, so a combined number never disappears with it."""
    return "(%s or vector(0))" % expr


MIST_PN = 'sum(mist_site_clients_by_ssid{ssid="PantherNet"})'
MIST_MC = 'sum(mist_site_clients_by_ssid{ssid="MiddleburyCollege"})'
CAMPUS_PN = "(%s + %s)" % (_z(MIST_PN), _z(ARUBA_PN))
CAMPUS_MC = "(%s + %s)" % (_z(MIST_MC), _z(ARUBA_MC))
CAMPUS_NOTE = ("Mist and Aruba added together: the devices connected right now, counted on both platforms "
               "(Mist counts a device if it was seen in the last 30 minutes; Aruba is the controllers' live tables).")
OS_METRICS = '__name__=~"aruba_ssid_os_clients|mist_ssid_os_clients"'


def pn_campus_view():
    pn_os = 'sum by (os) ({%s, ssid="PantherNet"})' % OS_METRICS
    all_os = 'sum by (os) ({%s, ssid=~"PantherNet|MiddleburyCollege"})' % OS_METRICS
    p = [
        stat("Devices on PantherNet", 0, 0, 5, CAMPUS_PN, "Campus-wide. " + CAMPUS_NOTE),
        stat("Devices on MiddleburyCollege", 5, 0, 5, CAMPUS_MC,
             "Campus-wide, the old network. " + CAMPUS_NOTE),
        stat("PantherNet share", 10, 0, 5, "100 * %s / (%s + %s)" % (CAMPUS_PN, CAMPUS_PN, CAMPUS_MC),
             "Of the devices on PantherNet or MiddleburyCollege, the percentage on PantherNet, Mist and Aruba "
             "together. This is the adoption number.", unit="percent"),
        stat("Mist side", 15, 0, 4, "100 * %s / (%s + %s)" % (MIST_PN, MIST_PN, MIST_MC),
             "The same percentage for the Mist-managed buildings only.", unit="percent"),
        stat("Aruba side", 19, 0, 5, "100 * %s / (%s + %s)" % (ARUBA_PN, ARUBA_PN, ARUBA_MC),
             "The same percentage for the Aruba-managed buildings only.", unit="percent"),

        timeseries("PantherNet share over time", 0, 4, 12, 9,
                   [target("100 * %s / (%s + %s)" % (CAMPUS_PN, CAMPUS_PN, CAMPUS_MC), "Campus"),
                    target("100 * %s / (%s + %s)" % (MIST_PN, MIST_PN, MIST_MC), "Mist side", ref="B"),
                    target("100 * %s / (%s + %s)" % (ARUBA_PN, ARUBA_PN, ARUBA_MC), "Aruba side", ref="C")],
                   "How the adoption percentage moves, for the campus and for each platform. A line going up "
                   "means more devices are choosing PantherNet. It is a snapshot of who is connected, so it "
                   "follows the school day and the academic calendar.", unit="percent", min_=None),
        timeseries("Devices over time", 12, 4, 12, 9,
                   [target(MIST_PN, "PantherNet - Mist"), target(ARUBA_PN, "PantherNet - Aruba", ref="B"),
                    target(MIST_MC, "MiddleburyCollege - Mist", ref="C"),
                    target(ARUBA_MC, "MiddleburyCollege - Aruba", ref="D")],
                   "Devices connected to each network, on each platform. " + CAMPUS_NOTE),

        bargauge("Operating systems on PantherNet", 0, 13, 12, 9, "sort_desc(%s)" % pn_os, "{{os}}",
                 "Campus-wide, Mist and Aruba together. Operating systems are folded into one short list; "
                 "'Unknown' is a device the network could not identify."),
        bargauge("Operating systems on MiddleburyCollege", 12, 13, 12, 9,
                 'sort_desc(sum by (os) ({%s, ssid="MiddleburyCollege"}))' % OS_METRICS, "{{os}}",
                 "The same view for the old network: which kinds of device have not moved yet."),

        bargauge("PantherNet share by operating system", 0, 22, 24, 9,
                 "sort_desc(100 * %s / %s)" % (pn_os, all_os), "{{os}}",
                 "For each operating system, the percentage of its devices that are on PantherNet rather than "
                 "MiddleburyCollege. A short bar is where adoption is lagging.", unit="percent", max_=100),

        row("Sign-in problems and overlap", 31),
        stat("Users who failed to sign in", 0, 32, 6, "mist_pn_auth_failure_clients",
             "Mist buildings only, last 24 hours: unique users with at least one failed PantherNet sign-in. "
             "Aruba does not report failures. " + PN_FAILURE_DEF + LOADING_NOTE,
             thresholds=steps((GREEN, None), (AMBER, 1)), no_value=LOADING),
        stat("...who then used MiddleburyCollege", 6, 32, 6, 'mist_pn_fallback_rate{match="user"}',
             "Mist buildings only: of the failing users old enough to judge, the percentage who connected to "
             "MiddleburyCollege within 30 minutes. " + PN_FALLBACK_DEF + LOADING_NOTE, unit="percentunit",
             thresholds=steps((GREEN, None), (AMBER, 0.25), (RED, 0.5)), no_value=LOADING),
        stat("People on both networks", 12, 32, 6, "aruba_people_on_both_ssids",
             "Aruba buildings only: people (by username) with a device on PantherNet and another on "
             "MiddleburyCollege right now."),
        stat("Data age", 18, 32, 6,
             'max(time() - mist_exporter_last_success_timestamp_seconds{source=~"clients|aruba"})',
             "Seconds since the older of the Mist and Aruba counts last refreshed.", unit="s",
             thresholds=steps((GREEN, None), (AMBER, 300), (RED, 900)), color_mode="background"),
    ]
    return dashboard("pn-adoption", "PantherNet Adoption", p, [],
                     "Campus-wide PantherNet adoption: Mist and Aruba together.",
                     time_from="now-7d", tags=("mist", "aruba"))


def build():
    return {"mist-overview.json": overview(), "mist-wireless.json": wireless(), "mist-load.json": load(),
            "mist-site.json": site_view(), "mist-device.json": device_view(), "mist-wifi.json": wifi_view(),
            "aruba-wireless.json": aruba_view(), "pn-adoption.json": pn_campus_view()}


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
