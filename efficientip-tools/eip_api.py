"""Minimal SOLIDserver REST client (stdlib only), shared by the tools in this folder.

Configuration comes from the environment (see eip-tools.env.example):
    EIP_BASE_URL    https://<solidserver-manager>        (required)
    EIP_USER        read-only API account                (required)
    EIP_PASS        its password                         (required)
    EIP_VERIFY_TLS  "1" to verify the appliance certificate (default off, as the appliance
                    uses an internal CA that is not trusted on the host)
"""
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE = 500


class EipError(RuntimeError):
    pass


def config(env=None):
    env = os.environ if env is None else env
    cfg = {
        "base_url": env.get("EIP_BASE_URL", "").strip().rstrip("/"),
        "user": env.get("EIP_USER", "").strip(),
        "password": env.get("EIP_PASS", ""),
        "verify_tls": env.get("EIP_VERIFY_TLS", "0").strip() == "1",
    }
    missing = [k for k, v in (("EIP_BASE_URL", cfg["base_url"]), ("EIP_USER", cfg["user"]),
                              ("EIP_PASS", cfg["password"])) if not v]
    if missing:
        raise EipError("missing environment variable(s): " + ", ".join(missing))
    return cfg


def _http_get(url, user, password, verify_tls, timeout):
    token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + token,
                                               "Accept": "application/json"})
    ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get_rows(endpoint, where="", orderby="", max_rows=5000, cfg=None, timeout=60):
    """GET /rest/<endpoint> page by page; return the rows whose errno is 0."""
    cfg = cfg or config()
    endpoint = "/" + endpoint.lstrip("/")
    if not endpoint.startswith("/rest/"):
        endpoint = "/rest" + endpoint
    rows, offset, started, pages = [], 0, time.time(), 0
    while len(rows) < max_rows:
        params = {"limit": min(PAGE, max_rows - len(rows)), "offset": offset}
        if where:
            params["WHERE"] = where
        if orderby:
            params["ORDERBY"] = orderby
        url = cfg["base_url"] + endpoint + "?" + urllib.parse.urlencode(params)
        status, body = _http_get(url, cfg["user"], cfg["password"], cfg["verify_tls"], timeout)
        if status == 204 or not body.strip():
            break
        if status >= 400:
            raise EipError("SOLIDserver HTTP %s on %s: %s" % (status, endpoint, body[:300]))
        page = json.loads(body)
        if isinstance(page, dict):
            if str(page.get("errno", "0")) != "0":
                raise EipError(page.get("errmsg") or page.get("name") or "SOLIDserver API error")
            page = [page]
        if not isinstance(page, list):
            raise EipError("unexpected response from " + endpoint)
        good = [r for r in page if isinstance(r, dict) and str(r.get("errno", "0")) == "0"]
        rows.extend(good)
        pages += 1
        if pages % 10 == 0:  # progress on stderr, so a slow run is visibly alive (journal or terminal)
            sys.stderr.write("eip_api: %s %d rows in %d pages, %.0fs\n"
                             % (endpoint, len(rows), pages, time.time() - started))
        if len(page) < params["limit"]:
            break
        offset += params["limit"]
    sys.stderr.write("eip_api: %s done: %d rows, %.0fs\n" % (endpoint, len(rows), time.time() - started))
    return rows[:max_rows]
