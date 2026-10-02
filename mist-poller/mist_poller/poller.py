"""Poll cycle: fetch -> normalize -> dedupe/lifecycle -> outbox -> Keep."""
import json
import logging
import time

from .keep import KeepError
from .mist_api import MistError
from .normalize import extract, resolved_payload

log = logging.getLogger("mist_poller")
SITES_REFRESH_S = 6 * 3600


class Poller:
    def __init__(self, cfg, mist, keep, state, clock=time.time):
        self.cfg, self.mist, self.keep, self.state, self.clock = cfg, mist, keep, state, clock
        self.sites = {}
        self.sites_at = 0

    # ---- one cycle -------------------------------------------------------
    def run_cycle(self):
        now = int(self.clock())
        st = self.state
        bootstrapping = st.get("bootstrapped") is None
        last_success = int(st.get("last_success", 0))
        wide = bootstrapping or now - int(st.get("last_reconcile", 0)) >= self.cfg.reconcile_interval
        if bootstrapping:
            start = now - self.cfg.bootstrap_hours * 3600
        elif wide:
            start = now - self.cfg.lookback_hours * 3600
        else:
            start = last_success - self.cfg.overlap_s

        self._refresh_sites(now)
        alarms = self.mist.search_alarms(start, now)   # raises on any failure: cursor stays put
        if wide and not bootstrapping:
            alarms = self._with_stale_lookups(alarms, start)
        st.set("last_mist_success", now)

        events, bad = [], 0
        for a in alarms:
            try:
                events.extend(extract(a, self.cfg, self.sites))
            except ValueError as e:
                bad += 1
                log.warning("skipping malformed alarm: %s", e)
        events.sort(key=lambda e: e.ts)

        stats = {"queued": 0}
        with st.transaction():
            self._apply(events, now, silent=bootstrapping, stats=stats)
            self._auto_resolve(now, stats)
            st.set("last_success", now)
            if wide:
                st.set("last_reconcile", now)
            if bootstrapping:
                st.set("bootstrapped", now)
            st.prune(now)
        log.info("cycle ok alarms=%d events=%d malformed=%d queued=%d window=%s%s",
                 len(alarms), len(events), bad, stats["queued"],
                 "bootstrap" if bootstrapping else ("wide" if wide else "overlap"),
                 " dry_run" if self.cfg.dry_run else "")
        self.drain(now)

    def _with_stale_lookups(self, alarms, wide_start):
        """Mist start/end filter on the alarm's own `timestamp`, so a still-open alarm
        older than the wide window can only be found by querying around that timestamp."""
        have = {a.get("id") for a in alarms}
        for row in self.state.stale_tracked(wide_start)[:25]:
            for a in self.mist.search_alarms(row["alarm_ts"] - 30, row["alarm_ts"] + 30):
                if a.get("id") not in have:
                    have.add(a.get("id"))
                    alarms.append(a)
        return alarms

    def _refresh_sites(self, now):
        if self.sites and now - self.sites_at < SITES_REFRESH_S:
            return
        try:
            self.sites = self.mist.list_sites()
            self.sites_at = now
        except MistError as e:
            log.warning("site lookup failed (%s); site names may be blank", e)
            self.sites_at = now - SITES_REFRESH_S + 300   # retry in ~5 min

    # ---- lifecycle -------------------------------------------------------
    def _apply(self, events, now, silent, stats):
        st, ttl = self.state, self.cfg.auto_resolve_minutes * 60
        for ev in events:
            if st.seen(ev.alarm_id, ev.device_key, ev.phase):
                continue
            st.mark_seen(ev.alarm_id, ev.device_key, ev.phase, now)
            row = st.alert(ev.fingerprint)

            if ev.phase == "fire":
                if row and row["status"] in ("firing", "silent"):
                    continue                              # already tracked
                if row and ev.ts < row["last_ts"]:
                    continue                              # stale, older than what we applied
                if ev.oneshot and now - ev.ts > ttl:
                    continue                              # would fire and resolve instantly
                auto_at = ev.ts + ttl if ev.oneshot else None
                status = "silent" if silent else "firing"
                st.upsert_alert(ev.fingerprint, status, ev.ts, ev.alarm_id, ev.alarm_ts, auto_at, ev.payload, now)
                if not silent:
                    self._enqueue(ev.fingerprint, ev.payload, now, stats)
            else:
                if not row or row["status"] == "resolved" or ev.ts < row["last_ts"]:
                    continue        # never post a recovery we never posted a firing for
                firing = json.loads(row["payload"])
                st.upsert_alert(ev.fingerprint, "resolved", ev.ts, row["alarm_id"], row["alarm_ts"], None, firing, now)
                if row["status"] == "firing":
                    self._enqueue(ev.fingerprint, resolved_payload(firing, ev.ts), now, stats)

        if silent:   # cold start: decide what to do with what is still open
            for row in st.alerts_with_status("silent"):
                if self.cfg.bootstrap_post_open:
                    payload = json.loads(row["payload"])
                    st.upsert_alert(row["fingerprint"], "firing", row["last_ts"], row["alarm_id"],
                                    row["alarm_ts"], row["auto_resolve_at"], payload, now)
                    self._enqueue(row["fingerprint"], payload, now, stats)

    def _auto_resolve(self, now, stats):
        for row in self.state.due_auto_resolves(now):
            firing = json.loads(row["payload"])
            self.state.upsert_alert(row["fingerprint"], "resolved", now, row["alarm_id"], row["alarm_ts"], None, firing, now)
            self._enqueue(row["fingerprint"], resolved_payload(firing, now, "auto_resolved"), now, stats)

    def _enqueue(self, fingerprint, payload, now, stats):
        self.state.enqueue(fingerprint, payload, now)
        stats["queued"] += 1

    # ---- outbox ----------------------------------------------------------
    def drain(self, now=None):
        """Deliver in insertion order so firing always precedes resolved.
        Stops at the first transient failure to preserve that order."""
        st = self.state
        while True:
            now = int(self.clock())
            row = st.next_pending()
            if row is None:
                return
            if row["next_try"] > now:
                return
            payload = json.loads(row["payload"])
            if self.cfg.dry_run:
                log.info("DRY_RUN would post: %s", json.dumps(payload, sort_keys=True))
                st.finish_delivery(row["id"], "dry_run", now)
                continue
            try:
                self.keep.post(payload)
            except KeepError as e:
                if e.permanent:
                    log.error("dropping undeliverable alert %s: %s", row["fingerprint"], e)
                    st.finish_delivery(row["id"], "failed", now, str(e))
                    continue
                backoff = min(300, 5 * 2 ** min(row["attempts"], 6))
                log.warning("Keep delivery failed (%s); retry in %ds", e, backoff)
                st.retry_later(row["id"], now + backoff, str(e))
                return
            st.finish_delivery(row["id"], "delivered", now)
            st.set("last_keep_success", now)
