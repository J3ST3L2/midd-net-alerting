#!/usr/bin/env python3
"""Read-only discovery of the Mist org alarms API.

Makes only GET requests. Never writes to Mist or Keep. Never prints the token.
Output is redacted: string values are replaced by placeholders except a small
allowlist of enum-like fields, so the report is safe to paste into a chat.

Config (environment variables):
  MIST_API_HOST         default api.mist.com (cloud region specific)
  MIST_ORG_ID           required
  MIST_API_TOKEN_FILE   path to file containing the token  (preferred)
  MIST_API_TOKEN        token itself (fallback; avoid shell history)
  MIST_WINDOW_HOURS     history window for the wide query, default 72
  MIST_PAGE_LIMIT       page size, default 100
  MIST_MAX_PAGES        pages per query, default 5
  MIST_DISCOVERY_OUT    output dir, default ./mist-discovery-out
"""
import collections
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get("MIST_API_HOST", "api.mist.com").strip()
ORG = os.environ.get("MIST_ORG_ID", "").strip()
WINDOW_H = float(os.environ.get("MIST_WINDOW_HOURS", "72"))
LIMIT = int(os.environ.get("MIST_PAGE_LIMIT", "100"))
MAX_PAGES = int(os.environ.get("MIST_MAX_PAGES", "5"))
OUT = os.environ.get("MIST_DISCOVERY_OUT", "./mist-discovery-out")
NARROW_S = 300

SAFE_KEYS = {"type", "group", "severity", "status", "acked", "count", "key"}
TIME_KEYS = re.compile(r"(time|timestamp|seen|created|updated|resolved|start|end)", re.I)
ARRAY_DEVICE_KEYS = ("aps", "switches", "gateways", "hostnames", "macs", "devices")


def load_token():
    path = os.environ.get("MIST_API_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
    else:
        tok = os.environ.get("MIST_API_TOKEN", "").strip()
    if not tok:
        sys.exit("No token: set MIST_API_TOKEN_FILE (preferred) or MIST_API_TOKEN")
    return tok


TOKEN = None


def get(url):
    """GET url (absolute, same host only). Returns (status, parsed_json_or_None)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != HOST.lower():
        sys.exit("Refusing to send token to unexpected URL host")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Token " + TOKEN,
            "Accept": "application/json",
            "User-Agent": "midd-mist-discovery/1",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit("Network error: %s" % type(e).__name__)


def base(path, params=None):
    q = "?" + urllib.parse.urlencode(params) if params else ""
    return "https://%s/api/v1%s%s" % (HOST, path, q)


def search(start, end):
    """Page through alarms/search. Returns (alarms, page_meta, statuses)."""
    url = base("/orgs/%s/alarms/search" % ORG,
               {"start": int(start), "end": int(end), "limit": LIMIT})
    alarms, metas, statuses = [], [], []
    for page in range(MAX_PAGES):
        status, body = get(url)
        statuses.append(status)
        if status != 200 or not isinstance(body, dict):
            break
        results = body.get("results")
        if results is None:
            results = body.get("alarms", [])
        alarms.extend(results)
        metas.append({k: (v if isinstance(v, (int, float, bool)) else type(v).__name__)
                      for k, v in body.items() if k != "results"})
        nxt = body.get("next")
        if not nxt or not results:
            break
        url = nxt if nxt.startswith("http") else "https://%s%s" % (HOST, nxt)
    return alarms, metas, statuses


def type_name(v):
    return type(v).__name__


def redact(v, key=None):
    if isinstance(v, dict):
        return {k: redact(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x, key) for x in v[:3]] + (["...(%d total)" % len(v)] if len(v) > 3 else [])
    if isinstance(v, str):
        return v if key in SAFE_KEYS else "<str>"
    return v  # numbers/bools/None (timestamps, counts) are not sensitive


def summarize(alarms):
    fields = collections.defaultdict(collections.Counter)
    for a in alarms:
        for k, v in a.items():
            fields[k][type_name(v)] += 1
    enums = {}
    for k in ("status", "severity", "type", "group", "acked"):
        enums[k] = dict(collections.Counter(str(a.get(k)) for a in alarms).most_common(40))
    arr = {}
    for k in ARRAY_DEVICE_KEYS:
        lens = [len(a[k]) for a in alarms if isinstance(a.get(k), list)]
        if lens:
            arr[k] = {"present": len(lens), "max_len": max(lens),
                      "gt1": sum(1 for n in lens if n > 1)}
    counts = [a["count"] for a in alarms if isinstance(a.get("count"), int)]
    ids = [a.get("id") for a in alarms if a.get("id")]
    return {
        "alarm_count": len(alarms),
        "field_types": {k: dict(v) for k, v in sorted(fields.items())},
        "enums": enums,
        "device_arrays": arr,
        "count_field": {"present": len(counts), "max": max(counts) if counts else None,
                        "gt1": sum(1 for c in counts if c > 1)},
        "unique_ids": len(set(ids)),
        "duplicate_ids": len(ids) - len(set(ids)),
        "time_fields": sorted(k for k in fields if TIME_KEYS.search(k)),
    }


def window_semantics(wide, narrow_ids, now):
    """Which timestamp does start/end filter on?

    For each time-like field, count wide-set alarms whose value falls inside the
    narrow window but which the narrow query did NOT return. If 'last_seen'
    shows misses while 'timestamp' shows none, start/end filters on creation.
    """
    lo = now - NARROW_S
    result = {}
    time_keys = {k for a in wide for k, v in a.items()
                 if TIME_KEYS.search(k) and isinstance(v, (int, float)) and v > 1e9}
    for k in sorted(time_keys):
        inside = [a for a in wide
                  if isinstance(a.get(k), (int, float)) and lo <= a[k] <= now]
        missed = [a for a in inside if a.get("id") not in narrow_ids]
        result[k] = {"in_narrow_window": len(inside), "not_returned_by_narrow_query": len(missed)}
    return result


def resolved_check(wide):
    res = [a for a in wide if str(a.get("status")) == "resolved"]
    return {
        "resolved_returned": len(res),
        "resolved_with_resolved_time": sum(1 for a in res if a.get("resolved_time")),
        "open_returned": sum(1 for a in wide if str(a.get("status")) == "open"),
    }


def main():
    global TOKEN
    if not ORG:
        sys.exit("Set MIST_ORG_ID")
    TOKEN = load_token()
    now = time.time()
    report = {"host": "<configured>", "window_hours": WINDOW_H}

    wide, wmeta, wstat = search(now - WINDOW_H * 3600, now)
    report["wide_query"] = {"http_statuses": wstat, "pagination_meta_per_page": wmeta,
                            "pages_fetched": len(wstat),
                            "hit_max_pages": len(wstat) >= MAX_PAGES}
    if not wstat or wstat[0] != 200:
        print("alarms/search failed: HTTP %s" % (wstat[:1] or "no response"))
        report["error"] = True
    else:
        report["wide_summary"] = summarize(wide)
        report["resolved_semantics"] = resolved_check(wide)

        narrow, nmeta, nstat = search(now - NARROW_S, now)
        report["narrow_query"] = {"http_statuses": nstat, "alarm_count": len(narrow)}
        report["start_end_filters_on"] = window_semantics(
            wide, {a.get("id") for a in narrow}, now)
        if wide:
            report["redacted_sample"] = redact(wide[0])
            open_ex = next((a for a in wide if a.get("status") == "open"), None)
            res_ex = next((a for a in wide if a.get("status") == "resolved"), None)
            if open_ex:
                report["redacted_sample_open"] = redact(open_ex)
            if res_ex:
                report["redacted_sample_resolved"] = redact(res_ex)

    st, defs = get(base("/const/alarm_defs"))
    summary = {"http_status": st}
    if st == 200 and isinstance(defs, list):
        summary["definitions"] = len(defs)
        summary["severity_values"] = dict(collections.Counter(
            str(d.get("severity")) for d in defs if isinstance(d, dict)))
        summary["groups"] = dict(collections.Counter(
            str(d.get("group")) for d in defs if isinstance(d, dict)))
        summary["keys"] = sorted(str(d.get("key")) for d in defs if isinstance(d, dict))
    report["alarm_defs"] = summary

    os.makedirs(OUT, mode=0o700, exist_ok=True)
    path = os.path.join(OUT, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    print(json.dumps(report, indent=2, sort_keys=True))
    print("\nWrote %s (redacted; safe to share)" % path)


if __name__ == "__main__":
    main()
