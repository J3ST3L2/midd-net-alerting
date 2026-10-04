"""One-off refresh: python -m mist_poller.refresh [--dry-run]

Re-sends every alert the poller currently has open, rebuilt with today's enrichment
(IP, model, firmware, reason, ...). The Keep workflows see the mist_refresh label and
edit the existing Slack card in place instead of posting a new one. Only open alerts can be
refreshed: Keep forgets the Slack message once an alert resolves.

Run inside the running container:
    docker compose exec mist-poller python -m mist_poller.refresh --dry-run
"""
import sys
import time

from . import config
from .keep import KeepClient
from .mist_api import MistClient
from .normalize import extract
from .state import State


def refresh(cfg, mist, keep, state, dry_run=False, out=print):
    sites, devices = mist.list_sites(), mist.list_devices()
    sent = skipped = 0
    for row in state.alerts_with_status("firing"):
        found = [a for a in mist.search_alarms(row["alarm_ts"] - 30, row["alarm_ts"] + 30)
                 if a.get("id") == row["alarm_id"]]
        events = [e for e in extract(found[0], cfg, sites, devices)
                  if e.fingerprint == row["fingerprint"]] if found else []
        if not events:
            skipped += 1
            out("skip    %s (alarm not found or now suppressed)" % row["fingerprint"])
            continue
        payload = events[0].payload
        labels = payload["labels"]
        out("%s %s | %s | %s | reason: %s" % ("would refresh" if dry_run else "refresh",
            labels["mist_device"], labels["mist_event_type"], labels["mist_ip"], labels["mist_reason"]))
        if not dry_run:
            keep.post(dict(payload, labels=dict(labels, mist_refresh="true")))
            # Store the enriched payload (without the refresh marker) for the later recovery card.
            state.upsert_alert(row["fingerprint"], "firing", row["last_ts"], row["alarm_id"],
                               row["alarm_ts"], row["auto_resolve_at"], payload, int(time.time()))
        sent += 1
    out("%s %d, skipped %d" % ("would refresh" if dry_run else "refreshed", sent, skipped))
    return sent


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = config.load()
    mist = MistClient(cfg.mist_host, cfg.org_id, cfg.mist_token, cfg.page_limit, cfg.max_pages)
    refresh(cfg, mist, KeepClient(cfg.keep_url, cfg.keep_api_key), State(cfg.db_path),
            dry_run="--dry-run" in argv)


if __name__ == "__main__":
    main()
