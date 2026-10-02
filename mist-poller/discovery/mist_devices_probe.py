#!/usr/bin/env python3
"""Read-only device status probe (GET only), same env vars as mist_discover.py.

Prints device counts per type/status. Set MIST_PROBE_NAME_CONTAINS=<text> to also list
the names (and status) of matching devices, e.g. to confirm whether alarmed devices are up.
"""
import collections
import json
import os
import sys
import urllib.error
import urllib.request

HOST = os.environ.get("MIST_API_HOST", "api.mist.com").strip()
ORG = os.environ.get("MIST_ORG_ID", "").strip()
OUT = os.environ.get("MIST_DISCOVERY_OUT", "./mist-discovery-out")

NAME_FILTER = os.environ.get("MIST_PROBE_NAME_CONTAINS", "").lower()   # optional, e.g. "atwater"


def token():
    p = os.environ.get("MIST_API_TOKEN_FILE")
    t = open(p, encoding="utf-8").read().strip() if p else os.environ.get("MIST_API_TOKEN", "").strip()
    if not t:
        sys.exit("Set MIST_API_TOKEN_FILE")
    return t


def get(path, tok):
    req = urllib.request.Request("https://%s/api/v1%s" % (HOST, path), headers={
        "Authorization": "Token " + tok, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null"), r.headers.get("Link")
    except urllib.error.HTTPError as e:
        return e.code, None, None
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit("Network error: %s" % type(e).__name__)


def summarize(rows):
    types = collections.defaultdict(collections.Counter)
    for r in rows:
        for k, v in r.items():
            types[k][type(v).__name__] += 1
    ip_like = [k for k in types if "ip" in k.lower()]
    kinds = collections.Counter(str(r.get("type")) for r in rows)
    sample = {k: ("<%s>" % type(v).__name__ if not isinstance(v, (int, float, bool)) else v)
              for k, v in (rows[0].items() if rows else [])}
    return {"rows": len(rows), "device_types": dict(kinds),
            "ip_like_fields": {k: dict(types[k]) for k in ip_like},
            "ip_present_count": sum(1 for r in rows if r.get("ip")),
            "mac_present_count": sum(1 for r in rows if r.get("mac")),
            "name_present_count": sum(1 for r in rows if r.get("name")),
            "field_types": {k: dict(v) for k, v in sorted(types.items())},
            "sample_shape": sample}


def main():
    if not ORG:
        sys.exit("Set MIST_ORG_ID")
    tok = token()
    rows = []
    for page in range(1, 51):
        status, body, _ = get("/orgs/%s/stats/devices?type=all&limit=1000&page=%d" % (ORG, page), tok)
        if status != 200 or not isinstance(body, list):
            sys.exit("device stats failed: HTTP %s" % status)
        rows += [r for r in body if isinstance(r, dict)]
        if len(body) < 1000:
            break
    report = {"devices": len(rows), "pages": page,
              "status_values": dict(collections.Counter(str(r.get("status")) for r in rows)),
              "by_type_and_status": dict(collections.Counter(
                  "%s/%s" % (r.get("type"), r.get("status")) for r in rows)),
              "with_ip": sum(1 for r in rows if r.get("ip"))}
    if NAME_FILTER:   # names you asked for plus status; no IPs or MACs
        report["matches"] = sorted("%s: %s" % (r.get("name"), r.get("status"))
                                   for r in rows if NAME_FILTER in str(r.get("name", "")).lower())
    os.makedirs(OUT, mode=0o700, exist_ok=True)
    with open(os.path.join(OUT, "devices_probe.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
