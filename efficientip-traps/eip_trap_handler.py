#!/usr/bin/env python3
"""snmptrapd traphandle: forward an SNMP trap from SOLIDserver to Keep.

snmptrapd runs this once per trap and writes the trap to stdin:
    line 1: sender hostname (or IP)
    line 2: transport info, e.g.  UDP: [10.1.15.10]:54321->[10.0.0.5]:162
    rest:   one "OID value" line per varbind (snmpTrapOID.0 names the trap)

The raw trap is appended to a log (so real trap names can be learned), then a
Keep event is posted. Stdlib only; never fails the trap (exit 0).
"""
import json
import re
import sys
import urllib.request

KEEP_URL = "https://keep.middlebury.edu/backend/alerts/event"
RAW_LOG = "/var/log/eip-traps.log"
TRAP_OID_KEYS = ("snmpTrapOID.0", "1.3.6.1.6.3.1.1.4.1.0")
CLEARED_WORDS = ("released", "cleared", "normal", "recovered", "closed")


def parse(text):
    lines = [l.rstrip("\n") for l in text.splitlines() if l.strip()]
    host = lines[0].strip() if lines else "unknown"
    m = re.search(r"\[([^\]]+)\]", lines[1]) if len(lines) > 1 else None
    ip = m.group(1) if m else host
    trap = "unknown-trap"
    varbinds = []
    for line in lines[2:]:
        oid, _, value = line.partition(" ")
        value = value.strip()
        if any(k in oid for k in TRAP_OID_KEYS):
            trap = value.split("::")[-1] or trap
        elif not any(k in oid for k in ("sysUpTime", "1.3.6.1.2.1.1.3.0")):
            varbinds.append("%s=%s" % (oid.split("::")[-1], value.strip('"')))
    return host, ip, trap, varbinds


def build_event(host, ip, trap, varbinds):
    message = "; ".join(varbinds)[:300] or "(no details in trap)"
    cleared = any(w in (trap + " " + message).lower() for w in CLEARED_WORDS)
    # Key the card on the first varbind's value (the alert name) so a raise and
    # its release share a fingerprint; fall back to the trap name.
    key = varbinds[0].split("=", 1)[1] if varbinds and "=" in varbinds[0] else trap
    return [{
        "name": "EfficientIP trap: %s" % trap,
        "status": "resolved" if cleared else "firing",
        "severity": "info" if cleared else "warning",
        "source": ["efficientip"],
        "fingerprint": "efficientip:trap:%s:%s" % (ip, key),
        "hostname": host,
        "ip": ip,
        "event": trap,
        "object": varbinds[0].split("=", 1)[0] if varbinds else "trap",
        "message": message,
    }]


def post(event, url=KEEP_URL):
    req = urllib.request.Request(
        url, data=json.dumps(event).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Service-Name": "snmptrapd"})
    return urllib.request.urlopen(req, timeout=10).status


def main():
    text = sys.stdin.read()
    try:
        with open(RAW_LOG, "a") as f:
            f.write(text.replace("\n", " | ") + "\n")
    except OSError:
        pass
    try:
        post(build_event(*parse(text)))
    except Exception as exc:  # never fail the trap
        sys.stderr.write("eip_trap_handler: post failed: %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
