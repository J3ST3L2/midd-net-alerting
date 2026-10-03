"""Turn one aggregated Mist alarm into per-device Keep events.

Findings from live discovery (api.ac2.mist.com):
  * Alarms are aggregated: aps/switches/gateways/hostnames are arrays and
    `count` is often > 1, so one alarm fans out to one event per device.
  * Only Marvis alarms carry status open|resolved. Everything else is an event;
    recovery arrives as a different type (device_down -> device_reconnected).
  * Severity is critical|warn|info.
"""
import re
from dataclasses import dataclass

SEVERITY = {"critical": "critical", "warn": "warning", "warning": "warning",
            "info": "low", "normal": "low"}

# type -> (canonical lifecycle name, phase). Both halves of a pair map to the same
# canonical name so firing and recovery share one Keep fingerprint,
# mist:alarms:<canonical>:<device>. device_state keeps the pre-existing scheme.
# Pairs come from /const/alarm_defs. A type not listed here is treated as a
# one-shot event (auto-resolved), never as an unpaired "firing forever" alert.
PAIRS = {
    "device_down": ("device_state", "fire"),
    "device_reconnected": ("device_state", "resolve"),
    "switch_down": ("switch_state", "fire"),
    "switch_reconnected": ("switch_state", "resolve"),
    "gateway_down": ("gateway_state", "fire"),
    "gateway_reconnected": ("gateway_state", "resolve"),
}


def _pair(fire, resolve, canon):
    PAIRS[fire] = (canon, "fire")
    PAIRS[resolve] = (canon, "resolve")


for _b in ("gw_bgp_neighbor", "gw_critical_port", "gw_vpn_path", "ha_control_link",
           "sw_bgp_neighbor", "sw_critical_port", "sw_ospf_neighbor", "sw_vc_port",
           "switch_lacp_member", "tunnel", "vpn_peer"):
    _pair(_b + "_down", _b + "_up", _b)
for _b in ("fan", "hot", "humidity", "mgmt_link_down", "partition", "pem", "poe", "psu", "warm"):
    _pair("gw_alarm_chassis_" + _b, "gw_alarm_chassis_%s_clear" % _b, "gw_alarm_chassis_" + _b)
for _b in ("cpu_board_sensor_failed", "fan", "hot", "humidity", "mgmt_link_down",
           "partition", "pem", "poe", "psu"):
    _pair("sw_alarm_chassis_" + _b, "sw_alarm_chassis_%s_clear" % _b, "sw_alarm_chassis_" + _b)
for _f, _r, _c in (
    ("sw_ddos_protocol_violation_set", "sw_ddos_protocol_violation_clear", "sw_ddos_protocol_violation"),
    ("gw_fib_count_threshold_exceeded", "gw_fib_count_returned_to_normal", "gw_fib_count"),
    ("gw_flow_count_threshold_exceeded", "gw_flow_count_returned_to_normal", "gw_flow_count"),
    ("esl_hung", "esl_recovered", "esl"),
    ("tt_monitored_resource_failed", "tt_monitored_resource_recovered", "tt_monitored_resource"),
    ("tt_tunnels_lost", "tt_tunnels_up", "tt_tunnels"),
    ("cellular_edge_disconnected_from_ncm", "cellular_edge_connected_to_ncm", "cellular_edge_ncm"),
    ("cellular_edge_ethernet_wan_disconnected", "cellular_edge_ethernet_wan_connected", "cellular_edge_eth_wan"),
    ("cellular_edge_ethernet_wan_unplugged", "cellular_edge_ethernet_wan_plugged", "cellular_edge_eth_plug"),
    ("cellular_edge_modem_wan_disconnected", "cellular_edge_modem_wan_connected", "cellular_edge_modem_wan"),
    ("mist_edge_disconnected", "mist_edge_connected", "mist_edge_conn"),
    ("mist_edge_cpu_usage_high", "mist_edge_cpu_usage_normal", "mist_edge_cpu"),
    ("mist_edge_disk_usage_high", "mist_edge_disk_usage_normal", "mist_edge_disk"),
    ("mist_edge_memory_usage_high", "mist_edge_memory_usage_normal", "mist_edge_memory"),
    ("mist_edge_fan_unplugged", "mist_edge_fan_plugged", "mist_edge_fan"),
    ("mist_edge_psu_unplugged", "mist_edge_psu_plugged", "mist_edge_psu"),
    ("mist_edge_powerinput_disconnected", "mist_edge_powerinput_connected", "mist_edge_power"),
    ("infra_arp_failure", "infra_arp_success", "infra_arp"),
    ("infra_dhcp_failure", "infra_dhcp_success", "infra_dhcp"),
    ("infra_dns_failure", "infra_dns_success", "infra_dns"),
):
    _pair(_f, _r, _c)

# Explicit routing overrides; everything else uses the device kind / keywords.
WIFI_TYPES = {"rogue_ap"}
INFRA_TYPES = {"loop_detected_by_ap"}   # an AP reports it, but it is a switch/L2 problem
_WIFI_WORDS = re.compile(r"(^|_)(wlan|ssid|radio|client|roam|auth|wifi|wireless)(_|$)")


@dataclass
class Event:
    alarm_id: str
    alarm_ts: int         # the alarm's own timestamp (what Mist start/end filter on)
    device_key: str
    phase: str            # "fire" | "resolve"
    fingerprint: str
    ts: int
    oneshot: bool         # no recovery signal from Mist; poller auto-resolves
    payload: dict


def _mac(value):
    return re.sub(r"[^0-9a-f]", "", str(value).lower())


def _iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _devices(alarm):
    """[(mac|None, hostname|None, kind|None)] for each device in the alarm."""
    macs = []
    for key, kind in (("aps", "ap"), ("switches", "switch"), ("gateways", "gateway")):
        for m in alarm.get(key) or []:
            macs.append((_mac(m), kind))
    names = [str(n) for n in (alarm.get("hostnames") or [])]
    if macs:
        aligned = len(names) == len(macs)
        return [(m, names[i] if aligned else None, k) for i, (m, k) in enumerate(macs)]
    if names:
        return [(None, n, None) for n in names]
    ents = [e for e in (alarm.get("impacted_entities") or []) if isinstance(e, dict)]
    if ents:   # Marvis alarms describe the device here instead of aps/switches/hostnames
        kinds = {"ap": "ap", "switch": "switch", "gateway": "gateway"}
        return [(_mac(e.get("entity_mac")) or None, e.get("entity_name") or None,
                 kinds.get(str(e.get("entity_type", "")).lower())) for e in ents]
    emacs = [_mac(m) for m in (alarm.get("entity_macs") or [])]
    if emacs:
        return [(m, None, None) for m in emacs]
    return [(None, None, None)]


def category(event_type, alarm):
    if event_type in INFRA_TYPES:
        return "infra"
    if event_type in WIFI_TYPES or _WIFI_WORDS.search(event_type):
        return "wifi"
    if event_type.startswith(("sw_", "gw_", "switch_", "gateway_", "vc_", "mist_edge", "ha_", "tt_")):
        return "infra"
    if alarm.get("group") == "security":
        return "wifi"           # rogue/attack detection comes from the wireless side
    if alarm.get("aps") and not (alarm.get("switches") or alarm.get("gateways")):
        return "wifi"
    return "infra"


def extract(alarm, cfg, sites, devices=None):
    """Events for one alarm. [] if suppressed; ValueError if malformed.
    `devices` is the MAC -> {ip,name,model} cache; alarms themselves carry no IPs."""
    devices = devices or {}
    aid, typ = alarm.get("id"), alarm.get("type")
    if not aid or not typ:
        raise ValueError("alarm missing id/type")
    typ = str(typ).lower()
    if typ in cfg.suppress_types:
        return []
    ts = alarm.get("timestamp")
    if not isinstance(ts, int):
        raise ValueError("alarm %s has no integer timestamp" % aid)
    last_seen = alarm.get("last_seen") if isinstance(alarm.get("last_seen"), int) else ts

    status = alarm.get("status")
    pair = PAIRS.get(typ)
    canon = None
    if pair:
        canon, phase = pair
    elif status:
        phase = "resolve" if status == "resolved" else "fire"
    else:
        phase = "fire"
    oneshot = canon is None and not status

    if phase == "resolve" and isinstance(alarm.get("resolved_time"), int):
        ev_ts = alarm["resolved_time"]
    else:
        ev_ts = last_seen

    severity = SEVERITY.get(str(alarm.get("severity", "")).lower(), "warning")
    if oneshot and severity == "low" and cfg.suppress_info_events:
        return []      # e.g. AP/switch restarts: informational, nothing to act on
    site_id = alarm.get("site_id") or ""
    cat = category(typ, alarm)

    events, seen = [], set()
    for mac, name, kind in _devices(alarm):
        ident = mac or (name or "").lower() or "org"
        if ident in seen:
            continue
        seen.add(ident)
        info = devices.get(mac, {}) if mac else {}
        device = name or info.get("name") or mac or "unknown-device"
        fp = ("mist:alarms:%s:%s" % (canon, ident)) if canon else ("mist:alarm:%s:%s" % (aid, ident))
        labels = {
            "mist_topic": "alarms",
            "mist_event_type": typ,
            "mist_state": "resolved" if phase == "resolve" else "open",
            "mist_category": cat,
            "mist_group": str(alarm.get("group", "")),
            "mist_org_id": str(alarm.get("org_id", "")),
            "mist_site_id": site_id,
            "mist_site_name": sites.get(site_id, ""),
            "mist_device": device,
            "mist_mac": mac or "",
            "mist_ip": info.get("ip") or "n/a",
            "mist_model": info.get("model") or "n/a",
            "mist_firmware": info.get("version") or "n/a",
            "mist_last_seen": _iso(info["last_seen"]) if info.get("last_seen") else "n/a",
            "mist_device_kind": kind or "",
            "mist_alarm_id": str(aid),
            "mist_alarm_count": str(alarm.get("count", 1)),
        }
        msg = "Mist %s on %s" % (typ, device)
        payload = {
            "name": "%s: %s" % (device, typ),
            "status": "resolved" if phase == "resolve" else "firing",
            "severity": severity,
            "lastReceived": _iso(ev_ts),
            "service": "network",
            "source": ["mist"],
            "message": msg,
            "description": msg,
            "fingerprint": fp,
            "labels": labels,
        }
        events.append(Event(str(aid), ts, ident, phase, fp, ev_ts, oneshot, payload))
    return events


def resolved_payload(firing_payload, ts, state="resolved"):
    """Resolution of a previously posted firing payload (same fingerprint)."""
    p = dict(firing_payload)
    p["status"] = "resolved"
    p["lastReceived"] = _iso(ts)
    p["labels"] = dict(firing_payload["labels"], mist_state=state)
    return p
