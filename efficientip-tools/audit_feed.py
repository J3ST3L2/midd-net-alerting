#!/usr/bin/env python3
"""SOLIDserver change log -> OpenObserve (everything) and Keep (the changes worth a card).

Who changed what in SOLIDserver (users, API accounts, automation) comes from its own event log,
`/rest/syslog_full_list`. Each run (a systemd timer, once a minute) reads the events newer than the
last one it saw and:

  1. sends every event to an OpenObserve stream, so the full change log is searchable;
  2. drops routine noise (ACME challenge records, reverse zones, accounts you list);
  3. collapses a burst of the same change by the same account into one summary card;
  4. posts the rest to Keep, which posts them to Slack.

Nothing is stored locally except one small state file holding the last event id. At-least-once:
if Keep is unreachable the state is not advanced past the failed card, so the next run retries.

Environment (see eip-tools.env.example): EIP_* (see eip_api.py), KEEP_URL, OO_URL / OO_USER /
OO_PASS / OO_STREAM (optional), EIP_AUDIT_STATE, EIP_AUDIT_BURST, EIP_AUDIT_IGNORE_USERS.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode

import eip_api

SOURCE = "solidserver-audit"
KEEP_URL = os.environ.get("KEEP_URL", "https://keep.middlebury.edu/backend/alerts/event")

SENSITIVE_FRAGMENTS = ("password", "passwd", "passphrase", "secret", "token", "private_key",
                       "community", "auth_pass", "priv_pass", "key_value")
SENSITIVE_KEYS = {"key", "keyvalue", "dnskey_value", "old_dns_key_value"}


def normalize_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def is_sensitive_key(key):
    k = normalize_key(key)
    return k in SENSITIVE_KEYS or any(f in k for f in SENSITIVE_FRAGMENTS)


def sanitize_params(raw):
    """Parse the URL-encoded service_param string, redacting secrets. Returns (clean_string, dict)."""
    pairs, params = [], {}
    for key, value in parse_qsl(str(raw or "").strip(), keep_blank_values=True):
        safe = "[REDACTED]" if is_sensitive_key(key) else value
        pairs.append((key, safe))
        if safe != "":
            params.setdefault(key, safe)
    return urlencode(pairs), params


def derive_action(service):
    value = str(service or "").strip().lower()
    prefix = value.split(":", 1)[0].strip()
    for phrase, action in (("remove", "delete"), ("delete", "delete"), ("revoke", "delete"),
                           ("add", "add"), ("create", "add"), ("grant", "add"),
                           ("edit", "update"), ("modify", "update"), ("update", "update"),
                           ("change", "update"), ("activate", "activate"),
                           ("deactivate", "deactivate"), ("refresh", "refresh"),
                           ("import", "import"), ("export", "export")):
        if prefix.startswith(phrase) or value.startswith(phrase):
            return action
    return normalize_key(prefix)


def derive_object_type(service):
    service = str(service or "").strip()
    if ":" in service:
        service = service.split(":", 1)[1]
    return normalize_key(service) or "system_event"


OBJECT_KEYS = ("rr_full_name", "dnszone_name", "dns_name", "dhcpscope_name", "dhcpsn_name",
               "dhcphost_name", "member_name", "usr_login", "user_login", "acl_name", "name",
               "hostname", "hostaddr", "ipv4_address")


def derive_object_name(params):
    for key in OBJECT_KEYS:
        value = str(params.get(key, "")).strip()
        if value and value != "[REDACTED]":
            return value
    return ""


def normalize_event(row):
    """One syslog_full_list row -> a flat event dict. Raises ValueError for unusable rows."""
    event_id = str(row.get("syslog_id") or "").strip()
    stime = int(str(row.get("syslog_stime") or "0"))
    if not event_id or stime <= 0:
        raise ValueError("row without syslog_id / syslog_stime")
    service = str(row.get("translated_service_name") or row.get("service_name") or "").strip()
    clean, params = sanitize_params(row.get("service_param"))
    return {
        "id": int(event_id),
        "time": datetime.fromtimestamp(stime, tz=timezone.utc).isoformat(),
        "user": str(row.get("usr_login") or "").strip() or "unknown",
        "service": service,
        "operation": str(row.get("service_name") or "").strip(),
        "action": derive_action(service),
        "object_type": derive_object_type(service),
        "object_name": derive_object_name(params),
        "params": params,
        "param_string": clean,
    }


def is_noise(event, ignore_users=()):
    """Routine churn nobody wants a card for."""
    if event["user"].lower() in {u.lower() for u in ignore_users}:
        return True
    p = event["params"]
    name = (p.get("rr_full_name") or "").lower()
    zone = (p.get("dnszone_name") or "").lower()
    if name.startswith("_acme-challenge."):
        return True
    if name.endswith(("in-addr.arpa", "ip6.arpa")) or zone.endswith(("in-addr.arpa", "ip6.arpa")):
        return True
    return False


def collapse(events, burst_min=10):
    """Group by (user, service). A group larger than burst_min becomes one summary card."""
    groups = {}
    for ev in events:
        groups.setdefault((ev["user"], ev["service"]), []).append(ev)
    cards = []
    for (user, service), evs in groups.items():
        if len(evs) > burst_min:
            first, last = evs[0], evs[-1]
            cards.append({"user": user, "service": service, "count": len(evs),
                          "ids": [e["id"] for e in evs], "first_id": first["id"],
                          "time": last["time"],
                          "object": "%d objects (first: %s)" % (len(evs), first["object_name"] or first["object_type"]),
                          "details": "%d x %s between %s and %s" % (len(evs), service, first["time"][11:19], last["time"][11:19])})
        else:
            for e in evs:
                cards.append({"user": user, "service": service, "count": 1, "ids": [e["id"]],
                              "first_id": e["id"], "time": e["time"],
                              "object": e["object_name"] or e["object_type"],
                              "details": describe(e)})
    cards.sort(key=lambda c: c["first_id"])
    return cards


def describe(event):
    p = event["params"]
    bits = []
    for key, label in (("rr_type", "type"), ("dnsview_name", "view"), ("dnszone_name", "zone"),
                       ("rr_ttl", "ttl"), ("value1", "value")):
        if p.get(key):
            bits.append("%s=%s" % (label, p[key][:80]))
    return ", ".join(bits) or event["operation"] or event["service"]


def build_keep_event(card, base_url):
    summary = "%s: %s" % (card["service"], card["object"])
    return {
        "name": "%s - %s" % (card["user"], card["service"]),
        "status": "firing",
        "severity": "info",
        "source": [SOURCE],
        "fingerprint": "%s:%s:%s" % (SOURCE, card["first_id"], card["count"]),
        "message": summary[:300],
        "description": card["details"][:500],
        "user": card["user"],
        "event": card["service"],
        "object": card["object"][:200],
        "details": card["details"][:500],
        "when": card["time"],
        "count": card["count"],
        "url": base_url,
    }


def post_json(url, payload, headers=None, user=None, password=None, timeout=15):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=h)
    if user:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(("%s:%s" % (user, password or "")).encode()).decode())
    return urllib.request.urlopen(req, timeout=timeout).status


def ship_to_openobserve(events):
    url = os.environ.get("OO_URL", "").rstrip("/")
    if not url or not events:
        return
    stream = os.environ.get("OO_STREAM", "eip_audit")
    docs = [{"_timestamp": int(datetime.fromisoformat(e["time"]).timestamp() * 1_000_000),
             "syslog_id": e["id"], "user": e["user"], "service": e["service"],
             "operation": e["operation"], "action": e["action"], "object_type": e["object_type"],
             "object_name": e["object_name"], "params": e["param_string"]} for e in events]
    try:
        post_json("%s/api/default/%s/_json" % (url, stream), docs,
                  user=os.environ.get("OO_USER"), password=os.environ.get("OO_PASS"))
    except Exception as exc:  # the change log in Slack matters more than the archive
        sys.stderr.write("audit_feed: OpenObserve ship failed: %s\n" % exc)


def load_state(path):
    try:
        with open(path) as f:
            return int(json.load(f)["last_id"])
    except (OSError, ValueError, KeyError):
        return None


def save_state(path, last_id):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"last_id": last_id, "saved": datetime.now(timezone.utc).isoformat()}, f)
    os.replace(tmp, path)


def run(args, cfg=None):
    state_path = os.environ.get("EIP_AUDIT_STATE", "/var/lib/eip-tools/audit.state.json")
    burst = int(os.environ.get("EIP_AUDIT_BURST", "10"))
    ignore = [u for u in os.environ.get("EIP_AUDIT_IGNORE_USERS", "").split(",") if u.strip()]
    cfg = cfg or eip_api.config()
    last = load_state(state_path)

    if last is None:
        newest = eip_api.get_rows("/rest/syslog_full_list", where="syslog_id>0",
                                  orderby="syslog_id DESC", max_rows=1, cfg=cfg)
        last = int(newest[0]["syslog_id"]) if newest else 0
        print("first run: starting after syslog_id=%d (no backlog posted)" % last)
        if not args.dry_run:
            save_state(state_path, last)
        return 0

    rows = eip_api.get_rows("/rest/syslog_full_list", where="syslog_id>%d" % last,
                            orderby="syslog_id ASC", max_rows=args.max_events, cfg=cfg)
    events, bad = [], 0
    for row in rows:
        try:
            events.append(normalize_event(row))
        except (ValueError, TypeError):
            bad += 1
    events.sort(key=lambda e: e["id"])
    newest_id = max([last] + [int(str(r.get("syslog_id") or 0)) for r in rows])

    important = [e for e in events if not is_noise(e, ignore)]
    cards = collapse(important, burst)
    print("fetched=%d bad=%d noise=%d cards=%d" % (len(rows), bad, len(events) - len(important), len(cards)))

    if args.dry_run:
        for c in cards:
            print(json.dumps(build_keep_event(c, cfg["base_url"]), indent=1))
        return 0

    ship_to_openobserve(events)
    posted_through = newest_id
    for card in cards:
        try:
            post_json(KEEP_URL, build_keep_event(card, cfg["base_url"]),
                      headers={"X-Service-Name": "eip-audit"})
        except Exception as exc:
            sys.stderr.write("audit_feed: Keep post failed: %s\n" % exc)
            posted_through = card["first_id"] - 1
            break
    save_state(state_path, max(last, posted_through))
    return 0 if posted_through == newest_id else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the cards, post and save nothing")
    ap.add_argument("--max-events", type=int, default=5000)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
