"""Polls Mist on a schedule and serves the last good snapshot to Prometheus.

Scrapes never call Mist: they render cached data, so Prometheus can scrape as often as it
likes without touching the Mist API rate limit. A failed refresh keeps the previous data and
shows up in mist_exporter_last_success_timestamp_seconds / mist_exporter_errors_total.
"""
import logging
import threading
import time

from . import pn_fallback
from .metrics import Family, alarm_families, device_families, render, site_ap_families, sle_families, wireless_families
from .mist_api import MistError

log = logging.getLogger("mist_exporter")

SOURCES = ("sites", "devices", "site_stats", "clients", "sle", "alarms", "fallback")
SLE_METRICS = ("coverage", "capacity", "time-to-connect", "roaming", "throughput")
SLE_AP_METRICS = ("coverage", "capacity")   # per-AP detail only where it points at a fix


class Collector:
    def __init__(self, cfg, client, clock=time.time):
        self.cfg, self.client, self.clock = cfg, client, clock
        self._lock = threading.Lock()
        self._data = {"sites": {}, "devices": [], "alarms": [], "site_stats": [],
                      "clients": {"ap": [], "sites": {}}, "sle": {}}
        self._ok = {}                        # source -> unix time of last success
        self._errors = {s: 0 for s in SOURCES}
        self._duration = {}
        self._due = {s: 0.0 for s in SOURCES}
        self._fallback = pn_fallback.Tracker(cfg.fallback_window_s, cfg.fallback_lookback_h * 3600,
                                             cfg.pn_ssid, cfg.mc_ssid)
        if not cfg.fallback_enabled:
            self._due["fallback"] = float("inf")

    def _interval(self, source):
        return {"sites": self.cfg.sites_interval, "devices": self.cfg.devices_interval,
                "site_stats": self.cfg.clients_interval, "clients": self.cfg.clients_interval,
                "sle": self.cfg.sle_interval,
                "alarms": self.cfg.alarms_interval,
                # Every minute while there is queued work (history loading, lookups), else the steady interval.
                "fallback": 60 if self._fallback.busy else self.cfg.fallback_interval}[source]

    def _fetch(self, source):
        if source == "sites":
            return self.client.list_sites()
        if source == "devices":
            return self.client.list_devices()
        if source == "site_stats":
            return self.client.list_site_stats()
        if source == "clients":
            return self._fetch_clients()
        if source == "sle":
            return self._fetch_sle()
        if source == "fallback":
            pn_fallback.run_slice(self._fallback, self.client, self.cfg, int(self.clock()))
            return None
        now = int(self.clock())
        return self.client.search_alarms(now - self.cfg.alarm_window_hours * 3600, now)

    def _fetch_clients(self):
        """Per-AP counts (org-wide) plus band and SSID counts for each site that has clients."""
        win = self.cfg.client_window
        with self._lock:
            active = [s["id"] for s in self._data["site_stats"] if s.get("id") and s.get("num_clients")]
        return {"ap": self.client.client_counts("ap", win),
                "sites": {sid: {d: self.client.client_counts(d, win, site_id=sid) for d in ("band", "ssid")}
                          for sid in active}}

    def _fetch_sle(self):
        """SLE summaries (and per-AP detail for coverage/capacity) for each site that has clients.
        One failing call (a metric a site does not support) is skipped, not fatal; the refresh only
        fails if every call does."""
        win = self.cfg.sle_window
        with self._lock:
            active = [s["id"] for s in self._data["site_stats"] if s.get("id") and s.get("num_clients")]
        out, ok, failed = {}, 0, 0
        for sid in active:
            site = out.setdefault(sid, {"summary": {}, "aps": {}})
            for metric in SLE_METRICS:
                try:
                    site["summary"][metric] = self.client.sle_summary(sid, metric, win)
                    ok += 1
                except MistError as e:
                    failed += 1
                    log.warning("SLE summary %s failed: %s", metric, e)
            for metric in SLE_AP_METRICS:
                try:
                    site["aps"][metric] = self.client.sle_impacted_aps(sid, metric, win)
                    ok += 1
                except MistError as e:
                    failed += 1
                    log.warning("SLE impacted-aps %s failed: %s", metric, e)
        if failed and not ok:
            raise MistError("every SLE call failed")
        return out

    def refresh(self, source):
        started = self.clock()
        delay = self._interval(source)
        try:
            data = self._fetch(source)
            with self._lock:
                self._data[source] = data
                self._ok[source] = self.clock()
                self._duration[source] = self.clock() - started
        except MistError as e:
            delay = max(delay, e.retry_after or 0)
            with self._lock:
                self._errors[source] += 1
            log.error("refresh %s failed: %s; keeping previous data; retry in %ds", source, e, delay)
        except Exception:
            with self._lock:
                self._errors[source] += 1
            log.exception("refresh %s crashed; keeping previous data", source)
        self._due[source] = self.clock() + delay

    def run_due(self):
        """Refresh every source whose time has come (one at a time, so Mist calls never burst)."""
        for source in SOURCES:
            if self.clock() >= self._due[source]:
                self.refresh(source)

    def run_forever(self, stop):
        while not stop.is_set():
            self.run_due()
            stop.wait(1)

    def healthy(self):
        """Device data must be fresh; a stale fleet view is the failure that matters."""
        with self._lock:
            last = self._ok.get("devices")
        return last is not None and self.clock() - last <= 3 * self.cfg.devices_interval + 30

    def render(self):
        with self._lock:
            sites, devices, alarms = self._data["sites"], self._data["devices"], self._data["alarms"]
            site_stats, clients, sle = self._data["site_stats"], self._data["clients"], self._data["sle"]
            ok, errors, duration = dict(self._ok), dict(self._errors), dict(self._duration)
        families = (device_families(devices, sites) + alarm_families(alarms, sites)
                    + wireless_families(devices, sites, site_stats, clients["ap"], clients["sites"])
                    + site_ap_families(site_stats) + sle_families(sites, sle) + self._fallback.families())
        families += [
            Family("mist_exporter_last_success_timestamp_seconds",
                   "Unix time of the last successful Mist refresh, per source.", "gauge",
                   [({"source": s}, ts) for s, ts in sorted(ok.items())]),
            Family("mist_exporter_refresh_duration_seconds",
                   "Duration of the last successful Mist refresh, per source.", "gauge",
                   [({"source": s}, d) for s, d in sorted(duration.items())]),
            Family("mist_exporter_errors_total", "Failed Mist refreshes, per source.", "counter",
                   [({"source": s}, float(n)) for s, n in sorted(errors.items())]),
        ]
        return render(families)
