"""PantherNet adoption: clients that fail to authenticate on PantherNet and fall back to
MiddleburyCollege.

Definitions (decided with the network team):
  * A counted failure is a PantherNet MARVIS_EVENT_CLIENT_AUTH_FAILURE event classified as
    ``dot1x_failed`` (reason 23 or "802.1x Auth Fail" text) or ``handshake_timeout`` (reason 15).
    Other reasons are reported as noise buckets and never counted.
  * A client is identified by USERNAME (normalized), because most clients use a different random MAC
    per SSID. Usernames exist only in this process's memory: they are never a metric label, never
    logged, never written to disk.
  * A fallback is a counted failure followed by a MiddleburyCollege association by the same user within
    ``window_s``. It is evaluated only once the window has elapsed, so the metric lags by that long.

This module holds state and arithmetic only. The Mist client is passed in by ``run_slice`` and the
event pages are fetched through callables, so everything here is testable without a network.
"""
import collections
import re
import threading

from .metrics import Family, _mac, _num
from .mist_api import MistError

FAIL_EVENT = "MARVIS_EVENT_CLIENT_AUTH_FAILURE"
OVERLAP_S = 60                 # incremental passes re-read this far back, de-duplicated on arrival
REASONS = ("dot1x_failed", "handshake_timeout", "previous_auth_invalid", "client_left", "tx_failure", "other")
COUNTED = frozenset({"dot1x_failed", "handshake_timeout"})
MAX_NAMES = 4                  # Mist lists 1-4 usernames per client
# Counts rebuilt from event history. After a restart they are partial until the history has loaded, so
# they are withheld until then: a restart must not draw a false dip into a weeks-long trend.
PARTIAL_WHILE_LOADING = frozenset({
    "mist_pn_auth_failure_events", "mist_pn_auth_failure_clients", "mist_pn_auth_failure_clients_unresolved",
    "mist_pn_fallback_eligible_clients", "mist_pn_fallback_clients", "mist_pn_fallback_rate",
    "mist_pn_failure_reason_events"})


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def classify(ev):
    """Bounded reason bucket for a failure event; first match wins."""
    reason, status = _int(ev.get("reason_code")), _int(ev.get("status_code"))
    if reason == 23 or "802.1x auth fail" in str(ev.get("text") or "").lower():
        return "dot1x_failed"
    if reason == 15:
        return "handshake_timeout"
    if reason == 2:
        return "previous_auth_invalid"
    if reason in (3, 8):
        return "client_left"
    if status == 79:
        return "tx_failure"
    return "other"


def normalize_username(raw):
    """Lowercase, without a DOMAIN\\ prefix or @domain suffix. Empty string if nothing usable."""
    name = str(raw or "").strip().lower()
    name = name.rsplit("\\", 1)[-1].split("@", 1)[0].strip()
    return name


def _names(rows):
    """Distinct raw usernames from clients/search rows (`username` is a list of 1-4 values)."""
    out = set()
    for r in rows:
        u = r.get("username")
        for v in (u if isinstance(u, list) else [u]):
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
    return sorted(out)[:MAX_NAMES]


def _device(rows):
    """(os, model), lowercase, from the first row that has both; ('', '') otherwise."""
    for r in rows:
        os_, model = r.get("os"), r.get("model")
        if isinstance(os_, str) and isinstance(model, str) and os_ and model:
            return os_.strip().lower(), model.strip().lower()
    return "", ""


def event_time(ev):
    ts = _num(ev.get("timestamp"))
    return int(ts) if ts is not None else None


class Stream:
    """One event feed, loaded newest-first in bounded slices.

    A walk goes backwards in time from "now" to a stop time, one page per call, moving the window's
    end to the oldest event seen so far (events are de-duplicated by the owner, so overlapping a
    second is harmless). A walk that runs out of page budget resumes next slice; once it finishes,
    everything up to its start time is loaded and the next walk only needs to cover the new events."""

    def __init__(self):
        self.covered_to = 0
        self.walk = None

    def advance(self, fetch_page, ingest, now, lookback_s, pages):
        """fetch_page(start, end) -> (rows newest-first, more). Returns pages used."""
        if self.walk is None:
            stop = now - lookback_s
            if self.covered_to:
                stop = max(stop, self.covered_to - OVERLAP_S)
            self.walk = {"stop": stop, "end": now, "start": now}
        w, used = self.walk, 0
        while used < pages:
            rows, more = fetch_page(w["stop"], w["end"])
            used += 1
            stamps = []
            for ev in rows:
                ts = event_time(ev)
                if ts is not None:
                    stamps.append(ts)
                    ingest(ev, ts)
            if not more or not stamps or min(stamps) <= w["stop"]:
                self.covered_to, self.walk = w["start"], None
                break
            # Move the end to the oldest second seen; always strictly backwards so it cannot loop.
            w["end"] = min(min(stamps) + 1, w["end"] - 1)
        return used


class Tracker:
    def __init__(self, window_s, lookback_s, pn_ssid="PantherNet", mc_ssid="MiddleburyCollege"):
        self.window_s, self.lookback_s = window_s, lookback_s
        self.pn_ssid, self.mc_ssid = pn_ssid, mc_ssid
        self.fail, self.assoc = Stream(), Stream()
        self._fail_events = {}      # (mac, ts, reason_code, status_code) -> (ts, mac, reason)
        self._assoc_ts = {}         # mac -> set of association times on MiddleburyCollege
        self._ident = {}            # mac -> {"key", "names", "os", "model"}; key "" = no username
        self._mc = {}               # user key -> {"at", "macs": {mac: (os, model)}}
        self._totals = {}           # ssid -> unique clients (Mist clients/search total)
        self._totals_at = 0
        self._latched = False
        self._busy = True           # outstanding work: history still loading or lookups still queued
        self._lock = threading.Lock()
        self._families = self._build(0, [], collections.Counter(), {}, {}, 0, 0, 0, 0, 0, 0, 0)

    # ---- ingestion -------------------------------------------------------------------------
    def ingest_failure(self, ev, ts):
        mac = _mac(ev.get("mac"))
        if mac:
            key = (mac, ts, _int(ev.get("reason_code")), _int(ev.get("status_code")))
            self._fail_events[key] = (ts, mac, classify(ev))

    def ingest_assoc(self, ev, ts):
        mac = _mac(ev.get("mac"))
        if mac:
            self._assoc_ts.setdefault(mac, set()).add(ts)

    def _prune(self, now):
        cutoff = now - self.lookback_s
        self._fail_events = {k: v for k, v in self._fail_events.items() if v[0] >= cutoff}
        for mac in list(self._assoc_ts):
            kept = {t for t in self._assoc_ts[mac] if t >= cutoff}
            if kept:
                self._assoc_ts[mac] = kept
            else:
                del self._assoc_ts[mac]
        live = {v[1] for v in self._fail_events.values()}
        self._ident = {m: i for m, i in self._ident.items() if m in live}
        keys = {i["key"] for i in self._ident.values() if i["key"]}
        self._mc = {k: v for k, v in self._mc.items() if k in keys}

    # ---- lookups (what still needs a Mist call) ----------------------------------------------
    def pending_identities(self):
        """Counted-failure MACs whose username has not been looked up, newest failure first."""
        newest = {}
        for ts, mac, reason in self._fail_events.values():
            if reason in COUNTED and mac not in self._ident:
                newest[mac] = max(ts, newest.get(mac, 0))
        return [m for m, _ in sorted(newest.items(), key=lambda kv: -kv[1])]

    def set_identity(self, mac, rows):
        names = _names(rows)
        keys = sorted({normalize_username(n) for n in names} - {""})
        os_, model = _device(rows)
        self._ident[mac] = {"key": keys[0] if keys else "", "names": names, "os": os_, "model": model}

    def _failures_by_user(self):
        users = collections.defaultdict(list)
        for ts, mac, reason in self._fail_events.values():
            ident = self._ident.get(mac)
            if reason in COUNTED and ident and ident["key"]:
                users[ident["key"]].append((ts, ident["os"], ident["model"]))
        return users

    def pending_mc_users(self, now):
        """Users with a matured failure whose MiddleburyCollege connections are not yet fetched (or were
        fetched before the failure's window closed). Returns [(key, raw usernames)]."""
        raw = {i["key"]: i["names"] for i in self._ident.values() if i["key"]}
        out = []
        for key, fails in self._failures_by_user().items():
            mature = [f[0] for f in fails if f[0] <= now - self.window_s]
            if not mature:
                continue
            have = self._mc.get(key)
            if have is None or have["at"] < max(mature) + self.window_s:
                out.append((max(mature), key, raw.get(key, [])))
        return [(k, names) for _, k, names in sorted(out, reverse=True)]

    def set_mc(self, key, now, rows):
        self._mc[key] = {"at": now, "macs": {_mac(r.get("mac")): _device([r]) for r in rows if _mac(r.get("mac"))}}

    def totals_due(self, now, interval):
        return now - self._totals_at >= interval

    def set_total(self, ssid, total, now):
        if _num(total) is not None:
            self._totals[ssid] = float(total)
            self._totals_at = now

    # ---- results -----------------------------------------------------------------------------
    def _evaluate(self, now):
        """(eligible, fallback) per match kind over users with a matured, fully looked-up failure."""
        res = {"user": [0, 0], "device": [0, 0]}
        for key, fails in self._failures_by_user().items():
            mature = [f for f in fails if f[0] <= now - self.window_s]
            have = self._mc.get(key)
            if not mature or have is None or have["at"] < max(f[0] for f in mature) + self.window_s:
                continue
            hit_user = hit_device = False
            for ts, os_, model in mature:
                for mac, dev in have["macs"].items():
                    if any(ts < t <= ts + self.window_s for t in self._assoc_ts.get(mac, ())):
                        hit_user = True
                        if os_ and model and dev == (os_, model):
                            hit_device = True
            res["user"][0] += 1
            res["user"][1] += hit_user
            if any(os_ and model for _, os_, model in mature):
                res["device"][0] += 1
                res["device"][1] += hit_device
        return res

    def publish(self, now):
        """Recompute the exported families (once per slice, so scrapes cost nothing)."""
        with self._lock:
            self._prune(now)
            reasons = collections.Counter(r for _, _, r in self._fail_events.values())
            counted = [(ts, m) for ts, m, r in self._fail_events.values() if r in COUNTED]
            idents = [self._ident.get(m) for _, m in counted]
            users = self._failures_by_user()
            unresolved = len({m for (_, m), i in zip(counted, idents) if i is not None and not i["key"]})
            pending_ident = len(self.pending_identities())
            pending_mc = len(self.pending_mc_users(now))
            res = self._evaluate(now)
            if (self.fail.covered_to and self.assoc.covered_to and not pending_ident and not pending_mc):
                self._latched = True
            self._busy = (not self._latched or pending_ident > 0 or pending_mc > 0
                          or self.fail.walk is not None or self.assoc.walk is not None)
            self._families = self._build(
                len(counted), counted, reasons, res, dict(self._totals), len(users), unresolved,
                pending_ident, pending_mc, 1 if self._latched else 0, self.window_s, now)

    def _build(self, n_events, counted, reasons, res, totals, n_users, unresolved, pend_i, pend_m,
               complete, window, now):
        def fam(name, help_, samples):
            return Family(name, help_, "gauge", samples)

        rate = [({"match": m}, e_f[1] / e_f[0]) for m, e_f in sorted(res.items()) if e_f[0]]
        fams = [
            fam("mist_pn_unique_clients", "Unique client devices seen in the last 24 h, per SSID (per-SSID MACs).",
                [({"ssid": s}, v) for s, v in sorted(totals.items())]),
            fam("mist_pn_auth_failure_events", "Counted PantherNet failure events in the last 24 h "
                "(802.1X failed + 4-way handshake timeout).", [({}, float(n_events))] if now else []),
            fam("mist_pn_auth_failure_clients", "Unique users with at least one counted PantherNet failure.",
                [({}, float(n_users))] if now else []),
            fam("mist_pn_auth_failure_clients_unresolved", "Failing devices with no username (excluded from "
                "fallback maths).", [({}, float(unresolved))] if now else []),
            fam("mist_pn_fallback_eligible_clients", "Failing users whose failure is older than the fallback "
                "window and whose MiddleburyCollege connections are looked up.",
                [({"match": m}, float(e_f[0])) for m, e_f in sorted(res.items())] if now else []),
            fam("mist_pn_fallback_clients", "Eligible users who connected to MiddleburyCollege within the "
                "window after a PantherNet failure.",
                [({"match": m}, float(e_f[1])) for m, e_f in sorted(res.items())] if now else []),
            fam("mist_pn_fallback_rate", "fallback_clients / eligible_clients (omitted when nothing is eligible).",
                rate),
            fam("mist_pn_failure_reason_events", "All PantherNet auth-failure events in the last 24 h, by reason "
                "bucket (includes buckets that are not counted as failures).",
                [({"reason": r}, float(reasons.get(r, 0))) for r in REASONS] if now else []),
            fam("mist_pn_lookups_pending", "Devices or users still waiting for a Mist lookup.",
                [({"stage": "username"}, float(pend_i)), ({"stage": "connections"}, float(pend_m))] if now else []),
            fam("mist_pn_fallback_window_seconds", "Configured fallback window.", [({}, float(window))] if now else []),
            fam("mist_pn_backfill_complete", "1 once the 24 h history and its lookups are loaded.",
                [({}, float(complete))] if now else []),
        ]
        if not complete:
            fams = [Family(f.name, f.help, f.type, []) if f.name in PARTIAL_WHILE_LOADING else f for f in fams]
        return fams

    def families(self):
        with self._lock:
            return self._families

    @property
    def complete(self):
        return self._latched

    @property
    def busy(self):
        """True while there is work queued, so the collector keeps running slices back to back.
        Without this, a 5-minute cadence could never clear a day's worth of new failing devices."""
        return self._busy


def run_slice(t, client, cfg, now):
    """One bounded unit of work: a few event pages per feed, a capped number of lookups, then publish.
    Progress survives an error (state lives in `t`); the error is re-raised afterwards so the
    collector counts it and honours Retry-After."""
    err = None
    try:
        pages = max(1, cfg.fallback_pages_per_slice // 2)
        t.fail.advance(lambda s, e: client.search_client_events(FAIL_EVENT, t.pn_ssid, s, e),
                       t.ingest_failure, now, t.lookback_s, pages)
        t.assoc.advance(lambda s, e: client.search_client_events(cfg.mc_event_type, t.mc_ssid, s, e),
                        t.ingest_assoc, now, t.lookback_s, pages)
        budget = cfg.fallback_lookups_per_slice
        for mac in t.pending_identities()[:budget]:
            rows, _ = client.search_clients(now - t.lookback_s, now, mac=mac)
            t.set_identity(mac, [r for r in rows if _mac(r.get("mac")) == mac])
            budget -= 1
        for key, names in t.pending_mc_users(now)[:max(budget, 0)]:
            found = []
            for name in names:
                found, _ = client.search_clients(now - t.lookback_s, now, ssid=t.mc_ssid, username=name)
                if found:
                    break
            t.set_mc(key, now, found)
        if t.totals_due(now, cfg.fallback_totals_interval):
            for ssid in (t.pn_ssid, t.mc_ssid):
                _, total = client.search_clients(now - t.lookback_s, now, limit=1, ssid=ssid)
                t.set_total(ssid, total, now)
    except MistError as e:
        err = e
    except Exception as e:
        # Never let an exception message travel to the log: it could quote a username or MAC.
        err = RuntimeError("fallback slice crashed: %s" % type(e).__name__)
    try:
        t.publish(now)
    except Exception as e:
        err = err or RuntimeError("fallback publish crashed: %s" % type(e).__name__)
    if err:
        raise err
