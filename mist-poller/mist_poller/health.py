"""Docker healthcheck: python -m mist_poller.health

healthy   Mist poll succeeded recently and Keep deliveries are not backed up
degraded  Mist polling works but deliveries to Keep are stuck (exit 0, printed)
unhealthy no successful Mist poll recently, or DB unusable (exit 1)
"""
import sqlite3
import sys
import time

from . import config


def check(cfg, now=None):
    now = int(now or time.time())
    try:
        db = sqlite3.connect(cfg.db_path, timeout=5)
        get = lambda k: (db.execute("SELECT value FROM poll_state WHERE key=?", (k,)).fetchone() or [None])[0]
        last_mist = get("last_mist_success")
        n, oldest = db.execute("SELECT COUNT(*), MIN(created_at) FROM pending_deliveries "
                               "WHERE status='pending'").fetchone()
    except sqlite3.Error as e:
        return "unhealthy", "database error: %s" % type(e).__name__
    if last_mist is None or now - int(last_mist) > cfg.health_unhealthy_s:
        return "unhealthy", "no successful Mist poll in %ss" % cfg.health_unhealthy_s
    if n and oldest is not None and now - int(oldest) > cfg.health_unhealthy_s:
        return "degraded", "%d alerts waiting for Keep, oldest %ds" % (n, now - int(oldest))
    return "healthy", "last Mist poll %ds ago, %d pending" % (now - int(last_mist), n)


def main():
    level, msg = check(config.load())
    print("%s: %s" % (level, msg))
    sys.exit(1 if level == "unhealthy" else 0)


if __name__ == "__main__":
    main()
