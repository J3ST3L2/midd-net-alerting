"""Aruba (AOS 8) client counts, read from each managed controller's read-only REST API.

Every controller is asked for its user table (`show user-table`); the rows are reduced to counts per
SSID, role, device type and band, and the rows are dropped. A username is used only to count distinct
people (and how many are on both PantherNet and MiddleburyCollege at once); it is held in memory for one
refresh and never exported, logged or stored. Counts are what leave this module.

The read-only account lives on the Managed Network node (/md), so each controller accepts it; the
Mobility Conductor itself holds no clients. A controller that cannot be reached keeps its last good
table for a few intervals (so a blip does not dip the totals), then is reported as down.
"""
import collections
import concurrent.futures
import http.cookiejar
import json
import logging
import re
import ssl
import time
import urllib.parse
import urllib.request

from .mist_api import MistError
from .pn_fallback import normalize_username

log = logging.getLogger("mist_exporter")

TOP_VALUES = 15          # roles and device types kept per SSID; the rest are summed into "other"


class ArubaError(MistError):
    pass


class ArubaClient:
    def __init__(self, user, password, timeout=60, ca_file="", verify=False, port=4343):
        self.user, self.password, self.timeout, self.port = user, password, timeout, port
        if ca_file:
            self.ctx = ssl.create_default_context(cafile=ca_file)
        elif verify:
            self.ctx = ssl.create_default_context()
        else:
            self.ctx = ssl._create_unverified_context()      # controllers present self-signed certificates

    def user_table(self, host):
        """The controller's `show user-table` rows (a list of dicts). Logs in and out within the call."""
        base = "https://%s:%d" % (host, self.port)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ctx),
                                             urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        token = None
        try:
            body = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
            with opener.open(urllib.request.Request(base + "/v1/api/login", data=body), timeout=self.timeout) as r:
                token = json.loads(r.read().decode())["_global_result"]["UIDARUBA"]
            query = urllib.parse.urlencode({"command": "show user-table", "UIDARUBA": token})
            with opener.open(base + "/v1/configuration/showcommand?" + query, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
            rows = json.loads(text).get("Users") if text else []
            return [r for r in (rows or []) if isinstance(r, dict)]
        except Exception as e:
            # Name the failure, never echo its text: an HTTP error body or a decode error can quote a row.
            raise ArubaError("%s: %s" % (host, type(e).__name__)) from None
        finally:
            if token:
                try:
                    opener.open(base + "/v1/api/logout?UIDARUBA=" + token, timeout=10).close()
                except Exception:
                    pass


def ssid_and_band(row):
    """('PantherNet', '5GHz') from the 'SSID/BSSID/5GHz-VHT' column."""
    parts = str(row.get("Essid/Bssid/Phy") or "").split("/")
    ssid = parts[0] or "unknown"
    band = parts[-1].split("-")[0] if len(parts) >= 3 and parts[-1] else "unknown"
    return ssid, band or "unknown"


def _label(value):
    return str(value).strip() if value not in (None, "") else "unknown"


def _top(counter, keep=TOP_VALUES):
    """The busiest keys of a Counter; everything else is summed into 'other'."""
    out = dict(counter.most_common(keep))
    rest = sum(counter.values()) - sum(out.values())
    if rest:
        out["other"] = rest
    return out


def summarize(tables, pn_ssid, mc_ssid):
    """Reduce {controller: rows} to counts. Wireless clients only, one per MAC across all controllers."""
    seen = set()
    per_ssid, per_ctrl = collections.Counter(), collections.Counter()
    roles, devices = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    bands, auth = collections.Counter(), collections.Counter()
    people = collections.defaultdict(set)
    for ctrl, rows in tables.items():
        for r in rows:
            if str(r.get("User Type") or "WIRELESS").upper() != "WIRELESS":
                continue
            mac = str(r.get("MAC") or "").lower()
            if mac:
                if mac in seen:
                    continue
                seen.add(mac)
            ssid, band = ssid_and_band(r)
            per_ssid[ssid] += 1
            per_ctrl[(ctrl, ssid)] += 1
            roles[ssid][_label(r.get("Role"))] += 1
            devices[ssid][_label(r.get("Type"))] += 1
            bands[(ssid, band)] += 1
            auth[(ssid, _label(r.get("Auth")))] += 1
            name = normalize_username(r.get("Name"))
            if name:
                people[ssid].add(name)
    return {"clients": dict(per_ssid), "by_controller": dict(per_ctrl),
            "roles": {s: _top(c) for s, c in roles.items()}, "devices": {s: _top(c) for s, c in devices.items()},
            "bands": dict(bands), "auth": dict(auth),
            "people": {s: len(n) for s, n in people.items()},
            "people_on_both": len(people.get(pn_ssid, set()) & people.get(mc_ssid, set()))}


class ArubaPoller:
    """Polls every controller, keeps each one's last good table, and summarizes what is current."""

    def __init__(self, client, controllers, pn_ssid, mc_ssid, interval, stale_s=None, workers=4, clock=time.time):
        self.client, self.controllers = client, list(controllers)
        self.pn_ssid, self.mc_ssid, self.clock, self.workers = pn_ssid, mc_ssid, clock, workers
        self.stale_s = stale_s if stale_s is not None else 3 * interval
        self._last = {}                              # controller -> (time, rows); memory only

    def _poll(self, host):
        started = self.clock()
        rows = self.client.user_table(host)
        return host, rows, self.clock() - started

    def poll(self):
        """One refresh. Raises ArubaError only if no controller answered and nothing is cached."""
        now = self.clock()
        status = {c: {"up": 0.0, "seconds": None, "clients": 0.0, "age": None} for c in self.controllers}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._poll, c): c for c in self.controllers}
            for fut in concurrent.futures.as_completed(futures):
                host = futures[fut]
                try:
                    _, rows, secs = fut.result()
                except ArubaError as e:
                    log.warning("aruba poll failed: %s", e)
                    continue
                self._last[host] = (now, rows)
                status[host].update(up=1.0, seconds=secs)
        tables = {}
        for host, (ts, rows) in list(self._last.items()):
            if now - ts > self.stale_s:
                del self._last[host]                  # too old to trust: stop counting its clients
                continue
            tables[host] = rows
            status[host]["clients"] = float(len(rows))
            status[host]["age"] = now - ts
        if not tables:
            raise ArubaError("no controller answered")
        out = summarize(tables, self.pn_ssid, self.mc_ssid)
        out["controllers"] = status
        return out
