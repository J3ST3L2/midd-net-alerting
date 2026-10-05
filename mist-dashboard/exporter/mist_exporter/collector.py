"""Polls Mist on a schedule and serves the last good snapshot to Prometheus.

Scrapes never call Mist: they render cached data, so Prometheus can scrape as often as it
likes without touching the Mist API rate limit. A failed refresh keeps the previous data and
shows up in mist_exporter_last_success_timestamp_seconds / mist_exporter_errors_total.
"""
import logging
import threading
import time

from .metrics import Family, alarm_families, device_families, render, wireless_families
from .mist_api import MistError

log = logging.getLogger("mist_exporter")

SOURCES = ("sites", "devices", "site_stats", "clients", "alarms")


class Collector:
    def __init__(self, cfg, client, clock=time.time):
        self.cfg, self.client, self.clock = cfg, client, clock
        self._lock = threading.Lock()
        self._data = {"sites": {}, "devices": [], "alarms": [], "site_stats": [],
                      "clients": {"ap": [], "band": [], "ssid": []}}
        self._ok = {}                        # source -> unix time of last success
        self._errors = {s: 0 for s in SOURCES}
        self._duration = {}
        self._due = {s: 0.0 for s in SOURCES}

    def _interval(self, source):
        return {"sites": self.cfg.sites_interval, "devices": self.cfg.devices_interval,
                "site_stats": self.cfg.clients_interval, "clients": self.cfg.clients_interval,
                "alarms": self.cfg.alarms_interval}[source]

    def _fetch(self, source):
        if source == "sites":
            return self.client.list_sites()
        if source == "devices":
            return self.client.list_devices()
        if source == "site_stats":
            return self.client.list_site_stats()
        if source == "clients":
            return {d: self.client.client_counts(d, self.cfg.client_window) for d in ("ap", "band", "ssid")}
        now = int(self.clock())
        return self.client.search_alarms(now - self.cfg.alarm_window_hours * 3600, now)

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
            site_stats, clients = self._data["site_stats"], self._data["clients"]
            ok, errors, duration = dict(self._ok), dict(self._errors), dict(self._duration)
        families = (device_families(devices, sites) + alarm_families(alarms, sites)
                    + wireless_families(devices, sites, site_stats, clients["ap"], clients["band"],
                                        clients["ssid"]))
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
