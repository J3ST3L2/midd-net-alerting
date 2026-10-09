import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_exporter.collector import Collector  # noqa: E402
from mist_exporter.config import Config  # noqa: E402
from mist_exporter.pn_fallback import CacheStore, Hasher, Tracker, open_cache, run_slice  # noqa: E402

# Reuse the scenario and fake Mist from the fallback tests.
from test_pn_fallback import (MACS, NOW, PN, MC, SECRETS, WINDOW, Clients, cfg, metric,  # noqa: E402
                              run_until_complete, scenario)

NAMES = SECRETS

KEY = "k" * 40


def make_tracker(path, key=KEY, now=NOW, ttl=72 * 3600):
    hasher, store = Hasher(key.encode()), CacheStore(path)
    return Tracker(WINDOW, 86400, PN, MC, hasher=hasher, store=store, cache_ttl_s=ttl, now=now)


class Hashing(unittest.TestCase):
    def test_deterministic_keyed_and_case_insensitive(self):
        a, b = Hasher(b"one"), Hasher(b"two")
        self.assertEqual(a("ZZ-A"), a("zz-a"))
        self.assertNotEqual(a("zz-a"), b("zz-a"))
        self.assertNotEqual(a("zz-a"), a("zz-b"))
        self.assertNotIn("zz-a", a("zz-a"))
        self.assertEqual(len(a("zz-a")), 32)


class Persistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "pn-cache.sqlite3")
        self.config = cfg()
        # Close every database the test opens (Windows cannot delete an open file); runs before dir.cleanup.
        opened, original = [], CacheStore.__init__

        def record(store, *args, **kw):
            original(store, *args, **kw)
            opened.append(store)

        patcher = mock.patch.object(CacheStore, "__init__", record)
        patcher.start()
        self.addCleanup(lambda: [o.close() for o in opened])
        self.addCleanup(patcher.stop)

    def first_run(self):
        t = make_tracker(self.path)
        client = Clients(*scenario())
        run_until_complete(t, client, self.config)
        return t, client

    def test_a_restart_with_the_cache_needs_far_fewer_mist_lookups(self):
        t1, c1 = self.first_run()
        cold_lookups = c1.calls["clients"]
        before = t1.families()

        t2 = make_tracker(self.path)                              # a "restarted" exporter, same key and file
        self.assertGreater(t2.restored[0], 0)
        c2 = Clients(*scenario())
        run_until_complete(t2, c2, self.config)
        self.assertLess(c2.calls["clients"], cold_lookups)
        # the matured users came from the cache, so only the totals calls (2) remain
        self.assertLessEqual(c2.calls["clients"], 2)
        # and the answer is the same
        for name, labels in (("mist_pn_auth_failure_clients", {}), ("mist_pn_fallback_clients", {"match": "user"}),
                             ("mist_pn_fallback_eligible_clients", {"match": "user"})):
            self.assertEqual(metric(t2.families(), name, **labels), metric(before, name, **labels), name)

    def test_the_database_file_never_contains_a_username_or_mac(self):
        self.first_run()
        raw = open(self.path, "rb").read().lower()
        for secret in NAMES + MACS + ["example.test", "iphone", "pixel"]:
            if secret in ("iphone", "pixel"):
                continue                      # os/model are device facts, not identifiers; skipped on purpose
            self.assertNotIn(secret.lower().encode(), raw, secret)
        # what it does hold is hashes
        db = sqlite3.connect(self.path)
        try:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM ident").fetchone()[0], 0)
            for (h,) in db.execute("SELECT mac_h FROM ident"):
                self.assertEqual(len(h), 32)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ident WHERE mac_h LIKE '%aabbcc%'").fetchone()[0], 0)
        finally:
            db.close()

    def test_the_file_is_private(self):
        self.first_run()
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o077, 0)

    def test_a_different_key_cannot_use_the_cache_and_does_not_crash(self):
        self.first_run()
        t = make_tracker(self.path, key="z" * 40)
        c = Clients(*scenario())
        run_until_complete(t, c, self.config)
        self.assertGreater(c.calls["clients"], 2)                 # hashes did not match: looked everything up again
        self.assertEqual(metric(t.families(), "mist_pn_auth_failure_clients"), 4.0)

    def test_old_entries_expire(self):
        self.first_run()
        t = make_tracker(self.path, now=NOW + 73 * 3600, ttl=72 * 3600)
        self.assertEqual(t.restored, (0, 0))

    def test_a_new_failure_for_a_cached_user_asks_mist_for_the_name_again(self):
        # Hash-only users have no raw username, so their MiddleburyCollege connections cannot be re-queried
        # by name until Mist is asked once more, through one of their MACs.
        self.first_run()
        t = make_tracker(self.path)
        client = Clients(*scenario())
        client.fails.append({"mac": "aa0000000001", "timestamp": NOW - 4000, "reason_code": 23,
                             "status_code": 0, "text": ""})      # user A fails again, later than the cached lookup
        run_slice(t, client, self.config, NOW + 7000)
        run_slice(t, client, self.config, NOW + 7000)
        run_until_complete(t, client, self.config)
        self.assertEqual(metric(t.families(), "mist_pn_fallback_eligible_clients", match="user"), 3.0)

    def test_no_key_means_memory_only(self):
        hasher, store = open_cache(self.path, "")
        self.assertIsNone(store)
        self.assertFalse(os.path.exists(self.path))
        short = os.path.join(self.dir.name, "short")
        open(short, "w").write("tooshort")
        self.assertIsNone(open_cache(self.path, short)[1])
        good = os.path.join(self.dir.name, "good")
        open(good, "w").write(KEY + "\n")
        self.assertIsNotNone(open_cache(self.path, good)[1])

    def test_a_broken_database_degrades_to_memory_and_never_crashes(self):
        store = CacheStore(self.path)
        t = Tracker(WINDOW, 86400, PN, MC, hasher=Hasher(KEY.encode()), store=store, now=NOW)
        store._db.close()                                          # every later call raises sqlite3.ProgrammingError
        run_until_complete(t, Clients(*scenario()), self.config)
        self.assertFalse(store.ok)
        self.assertEqual(metric(t.families(), "mist_pn_auth_failure_clients"), 4.0)

    def test_corrupt_cache_file_is_not_fatal(self):
        with open(self.path, "wb") as f:
            f.write(b"this is not a sqlite database" * 50)
        key_file = os.path.join(self.dir.name, "key")
        open(key_file, "w").write(KEY)
        hasher, store = open_cache(self.path, key_file)
        self.assertIsNone(store)                                   # logged and carried on in memory

    def test_collector_wires_the_cache_from_config(self):
        key_file = os.path.join(self.dir.name, "key")
        open(key_file, "w").write(KEY)
        config = Config(fallback_cache_path=self.path, fallback_cache_key_file=key_file)
        c = Collector(config, object(), clock=lambda: float(NOW))
        self.assertIsNotNone(c._fallback._store)
        self.assertIn("mist_pn_cache_entries", c.render())


if __name__ == "__main__":
    unittest.main()
