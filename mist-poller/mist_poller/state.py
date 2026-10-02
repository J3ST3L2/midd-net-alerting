"""SQLite state: cursor, dedupe memory, alert lifecycle and the delivery outbox."""
import contextlib
import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_state (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS seen_events (
    alarm_id TEXT NOT NULL, device_key TEXT NOT NULL, phase TEXT NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (alarm_id, device_key, phase));

CREATE TABLE IF NOT EXISTS alerts (
    fingerprint TEXT PRIMARY KEY,
    status TEXT NOT NULL,              -- firing | silent | resolved
    last_ts INTEGER NOT NULL,
    alarm_id TEXT NOT NULL,
    alarm_ts INTEGER NOT NULL,
    auto_resolve_at INTEGER,
    payload TEXT NOT NULL,
    updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS alerts_status_idx ON alerts(status);

CREATE TABLE IF NOT EXISTS pending_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | delivered | dry_run | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    next_try INTEGER NOT NULL DEFAULT 0,
    delivered_at INTEGER,
    last_error TEXT);
CREATE INDEX IF NOT EXISTS pending_idx ON pending_deliveries(status, id);
"""


class State:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # poll_state
    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM poll_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key, value):
        self.db.execute("INSERT INTO poll_state(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # dedupe
    def seen(self, alarm_id, device_key, phase):
        return self.db.execute(
            "SELECT 1 FROM seen_events WHERE alarm_id=? AND device_key=? AND phase=?",
            (alarm_id, device_key, phase)).fetchone() is not None

    def mark_seen(self, alarm_id, device_key, phase, now):
        self.db.execute("INSERT OR IGNORE INTO seen_events VALUES(?,?,?,?)",
                        (alarm_id, device_key, phase, now))

    # alerts
    def alert(self, fingerprint):
        return self.db.execute("SELECT * FROM alerts WHERE fingerprint=?", (fingerprint,)).fetchone()

    def upsert_alert(self, fingerprint, status, last_ts, alarm_id, alarm_ts, auto_resolve_at, payload, now):
        self.db.execute(
            "INSERT INTO alerts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
            "status=excluded.status,last_ts=excluded.last_ts,alarm_id=excluded.alarm_id,"
            "alarm_ts=excluded.alarm_ts,"
            "auto_resolve_at=excluded.auto_resolve_at,payload=excluded.payload,"
            "updated_at=excluded.updated_at",
            (fingerprint, status, last_ts, alarm_id, alarm_ts, auto_resolve_at, json.dumps(payload), now))

    def alerts_with_status(self, status):
        return self.db.execute("SELECT * FROM alerts WHERE status=?", (status,)).fetchall()

    def stale_tracked(self, before_ts):
        """Open lifecycle alerts (no auto-resolve) whose alarm predates the wide window."""
        return self.db.execute(
            "SELECT * FROM alerts WHERE status='firing' AND auto_resolve_at IS NULL "
            "AND fingerprint LIKE 'mist:alarm:%' AND alarm_ts<?", (before_ts,)).fetchall()

    def due_auto_resolves(self, now):
        return self.db.execute(
            "SELECT * FROM alerts WHERE status='firing' AND auto_resolve_at IS NOT NULL "
            "AND auto_resolve_at<=?", (now,)).fetchall()

    # outbox
    def enqueue(self, fingerprint, payload, now):
        self.db.execute("INSERT INTO pending_deliveries(fingerprint,payload,created_at) VALUES(?,?,?)",
                        (fingerprint, json.dumps(payload), now))

    def next_pending(self):
        return self.db.execute(
            "SELECT * FROM pending_deliveries WHERE status='pending' ORDER BY id LIMIT 1").fetchone()

    def finish_delivery(self, row_id, status, now, error=None):
        self.db.execute("UPDATE pending_deliveries SET status=?,delivered_at=?,last_error=?,"
                        "attempts=attempts+1 WHERE id=?", (status, now, error, row_id))

    def retry_later(self, row_id, next_try, error):
        self.db.execute("UPDATE pending_deliveries SET attempts=attempts+1,next_try=?,last_error=? "
                        "WHERE id=?", (next_try, error, row_id))

    def pending_stats(self):
        r = self.db.execute("SELECT COUNT(*) n, MIN(created_at) oldest FROM pending_deliveries "
                            "WHERE status='pending'").fetchone()
        return r["n"], r["oldest"]

    def prune(self, now, keep_days=7):
        cutoff = now - keep_days * 86400
        self.db.execute("DELETE FROM seen_events WHERE seen_at<?", (cutoff,))
        self.db.execute("DELETE FROM pending_deliveries WHERE status!='pending' AND created_at<?",
                        (cutoff,))
        self.db.execute("DELETE FROM alerts WHERE status='resolved' AND updated_at<?", (cutoff,))
