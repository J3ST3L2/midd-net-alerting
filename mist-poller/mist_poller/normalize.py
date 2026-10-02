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

SEVERITY = {"critical": "critical", "warn": "warning", "warning": "warning", "info": "low"}

# type -> (canonical lifecycle name, phase). Keeps the pre-existing Keep
# fingerprint scheme mist:alarms:device_state:<mac> for these pairs.
PAIRS = {
    "device_down": ("device_state", "fire"),
    "device_reconnected": ("device_state", "resolve"),
}

# Explicit routing overrides; everything else uses the device kind / keywords.
WIFI_TYPES = {"rogue_ap"}
INFRA_TYPES = {"loop_detected_by_ap"}   # an AP reports it, but it is a switch/L2 problem
_WIFI_WORDS = re.compile(r"(^|_)(wlan|ssid|radio|client|roam|auth|wifi|wireless)(_|$)")


@dataclass
class Event:
    alarm_id: str
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
    return [(None, None, None)]


def category(event_type, alarm):
    if event_type in INFRA_TYPES:
        return "infra"
    if event_type in WIFI_TYPES or _WIFI_WORDS.search(event_type):
        return "wifi"
    if alarm.get("aps") and not (alarm.get("switches") or alarm.get("gateways")):
        return "wifi"
    return "infra"


def extract(alarm, cfg, sites):
    """Events for one alarm. [] if suppressed; ValueError if malformed."""
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
    site_id = alarm.get("site_id") or ""
    cat = category(typ, alarm)

    events, seen = [], set()
    for mac, name, kind in _devices(alarm):
        ident = mac or (name or "").lower() or "org"
        if ident in seen:
            continue
        seen.add(ident)
        device = name or mac or "unknown-device"
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
        events.append(Event(str(aid), ident, phase, fp, ev_ts, oneshot, payload))
    return events


def resolved_payload(firing_payload, ts, state="resolved"):
    """Resolution of a previously posted firing payload (same fingerprint)."""
    p = dict(firing_payload)
    p["status"] = "resolved"
    p["lastReceived"] = _iso(ts)
    p["labels"] = dict(firing_payload["labels"], mist_state=state)
    return p
