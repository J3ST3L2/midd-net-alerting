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
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time

from .metrics import Family, _mac, _num
from .mist_api import MistError

log = logging.getLogger("mist_exporter")

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


class Hasher:
    """Keyed hash for anything identifying (usernames, MACs) that may be cached on disk.

    HMAC-SHA256 truncated to 128 bits. With a secret key, a copy of the cache file alone cannot be used to
    test a guessed username or MAC. It is still pseudonymous, not anonymous: whoever holds both the key and
    the file can check a guess. With no key it is plain hashing, used only for in-memory comparisons."""

    def __init__(self, key=b""):
        self._key = key

    def __call__(self, value):
        return hmac.new(self._key, str(value).lower().encode("utf-8"), hashlib.sha256).hexdigest()[:32]


class CacheStore:
    """SQLite cache of the two expensive kinds of Mist lookup, holding keyed hashes only.

      ident(mac_h, user_h, os, model, at)   which user a failing device belongs to ('' = no username)
      mc(user_h, at, macs)                  which devices a user connected to MiddleburyCollege with

    No raw username or MAC is ever written. Any database error switches the cache off (memory-only) with
    a one-line log naming the error type; it never stops the exporter."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            with self._lock:
                self._db.execute("CREATE TABLE IF NOT EXISTS ident(mac_h TEXT PRIMARY KEY, user_h TEXT NOT NULL, "
                                 "os TEXT, model TEXT, at INTEGER NOT NULL)")
                self._db.execute("CREATE TABLE IF NOT EXISTS mc(user_h TEXT PRIMARY KEY, at INTEGER NOT NULL, "
                                 "macs TEXT NOT NULL)")
                self._db.commit()
        except sqlite3.Error:
            self._db.close()                      # a corrupt file must not leak the connection
            raise
        self.ok = True

    def _guard(self, fn, default=None):
        if not self.ok:
            return default
        try:
            with self._lock:
                return fn()
        except sqlite3.Error as e:
            self.ok = False
            log.warning("pn cache disabled after a database error: %s", type(e).__name__)
            return default

    def load(self, min_at):
        """(ident, mc) entries newer than min_at, in the shape the tracker keeps in memory."""
        def go():
            ident = {m: {"key": u, "os": o or "", "model": md or "", "at": at} for m, u, o, md, at in
                     self._db.execute("SELECT mac_h, user_h, os, model, at FROM ident WHERE at >= ?", (min_at,))}
            mc = {}
            for u, at, macs in self._db.execute("SELECT user_h, at, macs FROM mc WHERE at >= ?", (min_at,)):
                mc[u] = {"at": at, "macs": {m: (o, md) for m, o, md in json.loads(macs)}}
            return ident, mc
        return self._guard(go, ({}, {}))

    def save_ident(self, mac_h, rec):
        self._guard(lambda: self._db.execute(
            "INSERT OR REPLACE INTO ident VALUES (?, ?, ?, ?, ?)",
            (mac_h, rec["key"], rec["os"], rec["model"], rec["at"])))

    def save_mc(self, user_h, rec):
        self._guard(lambda: self._db.execute(
            "INSERT OR REPLACE INTO mc VALUES (?, ?, ?)",
            (user_h, rec["at"], json.dumps([[m, o, md] for m, (o, md) in rec["macs"].items()]))))

    def close(self):
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    def commit(self, prune_before=None):
        def go():
            if prune_before is not None:
                self._db.execute("DELETE FROM ident WHERE at < ?", (prune_before,))
                self._db.execute("DELETE FROM mc WHERE at < ?", (prune_before,))
            self._db.commit()
        self._guard(go)


def open_cache(path, key_file):
    """(Hasher, CacheStore or None). Persistence needs a secret key of at least 32 characters in `key_file`;
    without one the tracker works exactly as before, in memory only."""
    key = ""
    if key_file and os.path.exists(key_file):
        with open(key_file, encoding="utf-8") as f:
            key = f.read().strip()
    if len(key) < 32:
        log.info("pn cache: persistence off (%s)", "no key file" if not key else "key shorter than 32 characters")
        return Hasher(b""), None
    try:
        store = CacheStore(path)
    except (sqlite3.Error, OSError) as e:
        log.warning("pn cache: persistence off (cannot open the database: %s)", type(e).__name__)
        return Hasher(key.encode("utf-8")), None
    log.info("pn cache: persistence on, keyed hashes only")
    return Hasher(key.encode("utf-8")), store


class Tracker:
    def __init__(self, window_s, lookback_s, pn_ssid="PantherNet", mc_ssid="MiddleburyCollege",
                 hasher=None, store=None, cache_ttl_s=72 * 3600, now=None):
        self.window_s, self.lookback_s = window_s, lookback_s
        self.pn_ssid, self.mc_ssid = pn_ssid, mc_ssid
        self._h, self._store, self.cache_ttl_s = hasher or Hasher(), store, cache_ttl_s
        self.fail, self.assoc = Stream(), Stream()
        # key -> (ts, raw mac, reason, mac hash). The raw MAC stays in memory: Mist lookups need it.
        self._fail_events = {}
        self._assoc_ts = {}         # mac hash -> set of association times on MiddleburyCollege
        self._ident = {}            # mac hash -> {"key": user hash ('' = none), "os", "model", "at"}
        self._names = {}            # user hash -> raw usernames, memory only; empty for entries from the cache
        self._mc = {}               # user hash -> {"at", "macs": {mac hash: (os, model)}}
        self._totals = {}           # ssid -> unique clients (Mist clients/search total)
        self._totals_at = 0
        self._latched = False
        self._busy = True           # outstanding work: history still loading or lookups still queued
        self._lock = threading.Lock()
        if store is not None:
            self._ident, self._mc = store.load((now if now is not None else time.time()) - cache_ttl_s)
        self.restored = (len(self._ident), len(self._mc))
        self._families = self._build(0, [], collections.Counter(), {}, {}, 0, 0, 0, 0, 0, 0, 0)

    # ---- ingestion -------------------------------------------------------------------------
    def ingest_failure(self, ev, ts):
        mac = _mac(ev.get("mac"))
        if mac:
            key = (mac, ts, _int(ev.get("reason_code")), _int(ev.get("status_code")))
            self._fail_events[key] = (ts, mac, classify(ev), self._h(mac))

    def ingest_assoc(self, ev, ts):
        mac = _mac(ev.get("mac"))
        if mac:
            self._assoc_ts.setdefault(self._h(mac), set()).add(ts)

    def _prune(self, now):
        cutoff = now - self.lookback_s
        self._fail_events = {k: v for k, v in self._fail_events.items() if v[0] >= cutoff}
        for mac_h in list(self._assoc_ts):
            kept = {t for t in self._assoc_ts[mac_h] if t >= cutoff}
            if kept:
                self._assoc_ts[mac_h] = kept
            else:
                del self._assoc_ts[mac_h]
        # Cached lookups expire by age, not by whether today's events mention them yet: a restart loads the
        # cache before the history has been re-read.
        oldest = now - self.cache_ttl_s
        self._ident = {m: i for m, i in self._ident.items() if i["at"] >= oldest}
        self._mc = {u: v for u, v in self._mc.items() if v["at"] >= oldest}
        self._names = {u: n for u, n in self._names.items() if u in {i["key"] for i in self._ident.values()}}

    # ---- lookups (what still needs a Mist call) ----------------------------------------------
    def pending_identities(self):
        """Counted-failure MACs whose username has not been looked up, newest failure first."""
        newest = {}
        for ts, mac, reason, mac_h in self._fail_events.values():
            if reason in COUNTED and mac_h not in self._ident:
                newest[mac] = max(ts, newest.get(mac, 0))
        return [m for m, _ in sorted(newest.items(), key=lambda kv: -kv[1])]

    def set_identity(self, mac, rows, now):
        names = _names(rows)
        keys = sorted({normalize_username(n) for n in names} - {""})
        user_h = self._h(keys[0]) if keys else ""
        os_, model = _device(rows)
        rec = {"key": user_h, "os": os_, "model": model, "at": now}
        self._ident[self._h(mac)] = rec
        if user_h:
            self._names[user_h] = names
        if self._store is not None:
            self._store.save_ident(self._h(mac), rec)

    def _failures_by_user(self):
        users = collections.defaultdict(list)
        for ts, _mac_, reason, mac_h in self._fail_events.values():
            ident = self._ident.get(mac_h)
            if reason in COUNTED and ident and ident["key"]:
                users[ident["key"]].append((ts, ident["os"], ident["model"]))
        return users

    def pending_mc_users(self, now):
        """Users with a matured failure whose MiddleburyCollege connections are not yet fetched (or were
        fetched before the failure's window closed). Returns [(user hash, raw usernames or [])]; the names
        are empty for a user known only from the cache, and run_slice then looks them up again."""
        out = []
        for key, fails in self._failures_by_user().items():
            mature = [f[0] for f in fails if f[0] <= now - self.window_s]
            if not mature:
                continue
            have = self._mc.get(key)
            if have is None or have["at"] < max(mature) + self.window_s:
                out.append((max(mature), key, self._names.get(key, [])))
        return [(k, names) for _, k, names in sorted(out, reverse=True)]

    def raw_mac_for_user(self, user_h):
        """A raw MAC from this window's failures whose cached identity belongs to this user."""
        for ts, mac, reason, mac_h in self._fail_events.values():
            ident = self._ident.get(mac_h)
            if reason in COUNTED and ident and ident["key"] == user_h:
                return mac
        return None

    def set_names(self, user_h, rows):
        names = _names(rows)
        if names:
            self._names[user_h] = names
        return names

    def set_mc(self, user_h, now, rows):
        rec = {"at": now, "macs": {self._h(_mac(r.get("mac"))): _device([r]) for r in rows if _mac(r.get("mac"))}}
        self._mc[user_h] = rec
        if self._store is not None:
            self._store.save_mc(user_h, rec)

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
                for mac_h, dev in have["macs"].items():
                    if any(ts < t <= ts + self.window_s for t in self._assoc_ts.get(mac_h, ())):
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
            reasons = collections.Counter(v[2] for v in self._fail_events.values())
            counted = [(v[0], v[3]) for v in self._fail_events.values() if v[2] in COUNTED]
            users = self._failures_by_user()
            unresolved = len({m for _, m in counted if m in self._ident and not self._ident[m]["key"]})
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
        if self._store is not None:
            self._store.commit(prune_before=now - self.cache_ttl_s)

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
            fam("mist_pn_cache_entries", "Cached lookups restored from disk at start (keyed hashes only).",
                [({"kind": "devices"}, float(self.restored[0])), ({"kind": "users"}, float(self.restored[1]))]
                if now else []),
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
            t.set_identity(mac, [r for r in rows if _mac(r.get("mac")) == mac], now)
            budget -= 1
        for key, names in t.pending_mc_users(now):
            if budget <= 0:
                break
            if not names:
                # Known only from the cache (hashes): ask Mist for the name again, through one of their MACs.
                mac = t.raw_mac_for_user(key)
                if mac is not None:
                    rows, _ = client.search_clients(now - t.lookback_s, now, mac=mac)
                    names = t.set_names(key, [r for r in rows if _mac(r.get("mac")) == mac])
                    budget -= 1
            found = []
            for name in names:
                found, _ = client.search_clients(now - t.lookback_s, now, ssid=t.mc_ssid, username=name)
                if found:
                    break
            if names or not t._mc.get(key):
                t.set_mc(key, now, found)
            budget -= 1
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
