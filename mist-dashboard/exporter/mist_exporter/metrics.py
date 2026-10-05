"""Turn Mist API payloads into Prometheus metric families (pure functions).

Every field is read defensively: Mist omits stats a device does not report, and a
missing value must produce no sample, never a wrong one.
"""
import collections
import re

Family = collections.namedtuple("Family", "name help type samples")

BANDS = (("band_24", "2.4"), ("band_5", "5"), ("band_6", "6"))


def _num(v):
    """Real numbers only (bool is an int subclass in Python, so exclude it)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _mac(raw):
    return re.sub(r"[^0-9a-f]", "", str(raw or "").lower())


def _site(sites, site_id):
    return sites.get(site_id) or site_id or "unassigned"


def _stat_block(row, key):
    """A cpu_stat/memory_stat block: top level first, else the first module that has one."""
    block = row.get(key)
    if isinstance(block, dict):
        return block
    for mod in row.get("module_stat") or []:
        if isinstance(mod, dict) and isinstance(mod.get(key), dict):
            return mod[key]
    return {}


def cpu_percent(row):
    v = _num(row.get("cpu_util"))                      # APs
    if v is not None:
        return v
    idle = _num(_stat_block(row, "cpu_stat").get("idle"))   # switches, gateways
    return None if idle is None else max(0.0, 100.0 - idle)


def memory_percent(row):
    used, total = _num(row.get("mem_used_kb")), _num(row.get("mem_total_kb"))   # APs
    if used is not None and total:
        return 100.0 * used / total
    return _num(_stat_block(row, "memory_stat").get("usage"))   # switches, gateways


def poe_watts(row):
    """(draw, budget) summed over switch modules, or None if the switch reports no PoE."""
    draw = budget = None
    for mod in row.get("module_stat") or []:
        poe = mod.get("poe") if isinstance(mod, dict) else None
        if not isinstance(poe, dict):
            continue
        d, m = _num(poe.get("power_draw")), _num(poe.get("max_power"))
        if d is not None:
            draw = (draw or 0.0) + d
        if m is not None:
            budget = (budget or 0.0) + m
    return None if draw is None and budget is None else (draw, budget)


def device_families(rows, sites):
    f = {n: Family(n, h, t, []) for n, h, t in (
        ("mist_device_up", "1 if Mist reports the device connected.", "gauge"),
        ("mist_device_info", "Device identity; value is always 1.", "gauge"),
        ("mist_device_uptime_seconds", "Device uptime.", "gauge"),
        ("mist_device_last_seen_timestamp_seconds", "Unix time Mist last heard from the device.", "gauge"),
        ("mist_device_cpu_percent", "CPU utilization.", "gauge"),
        ("mist_device_memory_percent", "Memory utilization.", "gauge"),
        ("mist_ap_clients", "Wireless clients on the AP.", "gauge"),
        ("mist_ap_band_clients", "Wireless clients on the AP, per radio band.", "gauge"),
        ("mist_ap_power_constrained", "1 if the AP is running power-constrained.", "gauge"),
        ("mist_switch_poe_draw_watts", "PoE power drawn by the switch.", "gauge"),
        ("mist_switch_poe_budget_watts", "PoE power budget of the switch.", "gauge"))}

    for row in rows:
        mac = _mac(row.get("mac"))
        if not mac:
            continue
        base = {"site": _site(sites, row.get("site_id")), "type": str(row.get("type") or "unknown"),
                "name": str(row.get("name") or mac), "mac": mac, "model": str(row.get("model") or "")}

        f["mist_device_up"].samples.append((base, 1.0 if row.get("status") == "connected" else 0.0))
        f["mist_device_info"].samples.append(
            (dict(base, version=str(row.get("version") or ""), ip=str(row.get("ip") or "")), 1.0))

        for fam, value in (("mist_device_uptime_seconds", _num(row.get("uptime"))),
                           ("mist_device_last_seen_timestamp_seconds", _num(row.get("last_seen"))),
                           ("mist_device_cpu_percent", cpu_percent(row)),
                           ("mist_device_memory_percent", memory_percent(row))):
            if value is not None:
                f[fam].samples.append((base, value))

        if base["type"] == "ap":
            clients = _num(row.get("num_clients"))
            if clients is not None:
                f["mist_ap_clients"].samples.append((base, clients))
            radios = row.get("radio_stat") if isinstance(row.get("radio_stat"), dict) else {}
            for key, band in BANDS:
                n = _num((radios.get(key) or {}).get("num_clients")) if isinstance(radios.get(key), dict) else None
                if n is not None:
                    f["mist_ap_band_clients"].samples.append((dict(base, band=band), n))
            if isinstance(row.get("power_constrained"), bool):
                f["mist_ap_power_constrained"].samples.append((base, 1.0 if row["power_constrained"] else 0.0))
        elif base["type"] == "switch":
            poe = poe_watts(row)
            if poe:
                if poe[0] is not None:
                    f["mist_switch_poe_draw_watts"].samples.append((base, poe[0]))
                if poe[1] is not None:
                    f["mist_switch_poe_budget_watts"].samples.append((base, poe[1]))
    return list(f.values())


def alarm_families(alarms, sites):
    """Alarm records in the exporter's window, counted by site/severity/type/group/state."""
    counts = collections.Counter()
    for a in alarms:
        resolved = bool(a.get("resolved_time")) or a.get("status") == "resolved"
        counts[(_site(sites, a.get("site_id")), str(a.get("severity") or "unknown"),
                str(a.get("type") or "unknown"), str(a.get("group") or "unknown"),
                "resolved" if resolved else "open")] += 1
    samples = [({"site": s, "severity": sev, "type": t, "group": g, "state": st}, float(n))
               for (s, sev, t, g, st), n in sorted(counts.items())]
    return [Family("mist_alarms", "Mist alarm records in the exporter window, by state.", "gauge", samples)]


def _escape(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(v):
    return repr(int(v)) if float(v).is_integer() and abs(v) < 1e15 else repr(float(v))


def render(families):
    """Prometheus text exposition format."""
    out = []
    for fam in families:
        out.append("# HELP %s %s" % (fam.name, fam.help))
        out.append("# TYPE %s %s" % (fam.name, fam.type))
        for labels, value in fam.samples:
            lab = ",".join('%s="%s"' % (k, _escape(v)) for k, v in sorted(labels.items()))
            out.append("%s%s %s" % (fam.name, "{%s}" % lab if lab else "", _fmt(value)))
    return "\n".join(out) + "\n"
