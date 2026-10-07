#!/usr/bin/env python3
"""Read-only discovery of the SOLIDserver REST API (GET only, nothing is written).

Tries likely service names for alerts and DHCP objects and reports which exist and what
fields they carry. Never prints the password. Review the output before sharing: alert rows
are shown (names/state are what we need) and may mention server names.

Config (environment variables):
  SOLID_HOST            SOLIDserver hostname (required)
  SOLID_USER            read-only SOLIDserver user (required)
  SOLID_PASSWORD_FILE   file containing that user's password (required)
  SOLID_CA_FILE         CA bundle if the appliance uses a private CA (optional)
  SOLID_INSECURE=1      skip TLS verification (testing only; noted in the report)
  SOLID_AUTH            "token" (recommended), "ipm" (default: X-IPM-Username/X-IPM-Password
                        headers) or "basic"
  SOLID_TOKEN_ID_FILE      file with the API token's Access Key (for SOLID_AUTH=token)
  SOLID_TOKEN_SECRET_FILE  file with the API token's Secret (for SOLID_AUTH=token)
  SOLID_DISCOVERY_OUT   output dir, default ./solid-discovery-out
"""
import base64
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("SOLID_HOST", "").strip()
USER = os.environ.get("SOLID_USER", "").strip()
PW_FILE = os.environ.get("SOLID_PASSWORD_FILE", "").strip()
TOKEN_ID_FILE = os.environ.get("SOLID_TOKEN_ID_FILE", "").strip()
TOKEN_SECRET_FILE = os.environ.get("SOLID_TOKEN_SECRET_FILE", "").strip()
CA_FILE = os.environ.get("SOLID_CA_FILE", "").strip()
INSECURE = os.environ.get("SOLID_INSECURE", "") == "1"
AUTH = os.environ.get("SOLID_AUTH", "ipm").strip().lower()
OUT = os.environ.get("SOLID_DISCOVERY_OUT", "./solid-discovery-out")

# Service names are guesses from the REST naming pattern; the probe tells us which are real.
CANDIDATES = [
    "member_list",   # always exists: doubles as the login check
    "alert_list", "alerts_list", "monitoring_alert_list", "alert_definition_list",
    "alert_def_list", "alert_group_list",
    "dhcp_server_list", "dhcp_scope_list", "dhcp_sharednetwork_list",
    "dhcp_shared_network_list", "dhcp_range_list", "dhcp_group_list",
    "dns_server_list",
]
ENUMISH = {"state", "status", "severity", "priority", "type", "condition", "enabled", "level"}


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def password():
    return read_file(PW_FILE)


def token_headers(method, url, key_id, secret):
    """SOLIDserver API token auth (as implemented in EfficientIP's own SOLIDserverRest client):
    Authorization: SDS <key id>:<sha3-256 of secret, timestamp, method and URL>. The secret itself
    is never sent."""
    ts = int(time.time())
    to_sign = "\n".join([secret, str(ts), method, url])
    sig = hashlib.sha3_256(to_sign.encode()).hexdigest()
    return {"Authorization": "SDS %s:%s" % (key_id, sig), "X-SDS-TS": str(ts)}


def b64(s):
    return base64.b64encode(s.encode()).decode()


def ctx():
    if INSECURE:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    return ssl.create_default_context(cafile=CA_FILE or None)


def get(service, pw, limit=25):
    url = "https://%s/rest/%s?%s" % (HOST, service, urllib.parse.urlencode({"LIMIT": limit}))
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if AUTH == "token":
        headers.update(token_headers("GET", url, pw[0], pw[1]))
    elif AUTH == "basic":
        headers["Authorization"] = "Basic " + b64("%s:%s" % (USER, pw))
    else:
        headers["X-IPM-Username"] = b64(USER)
        headers["X-IPM-Password"] = b64(pw)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx()) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as e:
        return None, "network error: %s" % type(e).__name__
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, "non-JSON response"


def shape(rows):
    types = {}
    for r in rows:
        for k, v in r.items():
            types.setdefault(k, set()).add(type(v).__name__)
    return {k: sorted(v) for k, v in sorted(types.items())}


def redact(row):
    return {k: (v if (k.lower() in ENUMISH or not isinstance(v, str)) else "<str>")
            for k, v in row.items()}


def main():
    if AUTH == "token":
        if not (HOST and TOKEN_ID_FILE and TOKEN_SECRET_FILE):
            sys.exit("Set SOLID_HOST, SOLID_TOKEN_ID_FILE and SOLID_TOKEN_SECRET_FILE")
        pw = (read_file(TOKEN_ID_FILE), read_file(TOKEN_SECRET_FILE))
    else:
        if not (HOST and USER and PW_FILE):
            sys.exit("Set SOLID_HOST, SOLID_USER and SOLID_PASSWORD_FILE")
        pw = password()
    report = {"host": "<configured>", "tls_verification": "OFF (testing)" if INSECURE else "on",
              "auth": AUTH, "services": {}}
    for svc in CANDIDATES:
        status, body = get(svc, pw)
        entry = {"http_status": status}
        if isinstance(body, list) and body and isinstance(body[0], dict) and "errno" not in body[0]:
            entry["rows"] = len(body)
            entry["fields"] = shape(body)
            entry["sample"] = redact(body[0])
            if "alert" in svc:   # alerts are the point: show names and state for every row
                entry["alert_rows"] = [
                    {k: (str(v)[:80] if isinstance(v, str) else v) for k, v in r.items()
                     if k.lower() in ENUMISH or "name" in k.lower() or "state" in k.lower()
                     or "date" in k.lower() or "time" in k.lower()} for r in body]
        elif isinstance(body, list) and body and isinstance(body[0], dict):
            entry["error"] = {k: body[0].get(k) for k in ("errno", "errmsg")}
        elif isinstance(body, list):
            entry["rows"] = 0
        else:
            entry["note"] = body if isinstance(body, str) else type(body).__name__
            if isinstance(body, dict):
                entry["body"] = json.dumps(body)[:300]
        report["services"][svc] = entry
        if status in (401, 403, 503) and not entry.get("rows"):
            report["stopped_early"] = (
                "HTTP %s on '%s'. Stopped after ONE failed request so the account is not locked "
                "by repeated attempts. Fix the login (see README) before running again." % (status, svc))
            break
    os.makedirs(OUT, mode=0o700, exist_ok=True)
    path = os.path.join(OUT, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print(json.dumps(report, indent=2, sort_keys=True))
    print("\nWrote %s. Review before sharing (alert rows are shown)." % path)


if __name__ == "__main__":
    main()
