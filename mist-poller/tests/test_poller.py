import dataclasses
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mist_poller.config import Config  # noqa: E402
from mist_poller.keep import KeepError  # noqa: E402
from mist_poller.mist_api import MistError  # noqa: E402
from mist_poller.normalize import extract  # noqa: E402
from mist_poller.poller import Poller  # noqa: E402
from mist_poller.state import State  # noqa: E402

NOW = 1_790_000_000
AP1, AP2, SW1 = "5c5b35000001", "5c5b35000002", "5c5b35aa0001"


def alarm(id, type, ts=NOW - 60, **kw):
    a = {"id": id, "type": type, "timestamp": ts, "last_seen": ts, "count": 1,
         "severity": "critical", "group": "infrastructure", "org_id": "o", "site_id": "s1"}
    a.update(kw)
    return a


class FakeMist:
    def __init__(self):
        self.alarms, self.fail = [], False
        self.calls = []

    def search_alarms(self, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise MistError("boom")
        return list(self.alarms)

    def list_sites(self):
        return {"s1": "Davis Library"}


class FakeKeep:
    def __init__(self):
        self.posted, self.down = [], False

    def post(self, payload):
        if self.down:
            raise KeepError("down")
        self.posted.append(payload)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def make(**cfg_kw):
    cfg = Config(dry_run=False, keep_api_key="k", mist_token="t", org_id="o", **cfg_kw)
    mist, keep, clock = FakeMist(), FakeKeep(), Clock()
    p = Poller(cfg, mist, keep, State(":memory:"), clock)
    return p, mist, keep, clock


def bootstrapped(**kw):
    p, mist, keep, clock = make(**kw)
    p.run_cycle()                  # empty cold start
    clock.t += 60
    return p, mist, keep, clock


def statuses(keep):
    return [(x["labels"]["mist_event_type"], x["status"]) for x in keep.posted]


class NormalizeTests(unittest.TestCase):
    cfg = Config()

    def test_aggregated_alarm_fans_out_per_device(self):
        a = alarm("a1", "device_down", count=3, aps=[AP1, AP2], hostnames=["AP-ONE", "AP-TWO"])
        evs = extract(a, self.cfg, {"s1": "Davis Library"})
        self.assertEqual([e.fingerprint for e in evs],
                         ["mist:alarms:device_state:" + AP1, "mist:alarms:device_state:" + AP2])
        self.assertEqual(evs[0].payload["name"], "AP-ONE: device_down")
        self.assertEqual(evs[0].payload["labels"]["mist_site_name"], "Davis Library")
        self.assertEqual(evs[0].payload["labels"]["mist_category"], "wifi")

    def test_pair_types_share_fingerprint(self):
        d = extract(alarm("a", "device_down", aps=[AP1]), self.cfg, {})[0]
        r = extract(alarm("b", "device_reconnected", aps=[AP1]), self.cfg, {})[0]
        self.assertEqual(d.fingerprint, r.fingerprint)
        self.assertEqual((d.phase, r.phase), ("fire", "resolve"))

    def test_arp_suppressed(self):
        self.assertEqual(extract(alarm("a", "infra_arp_failure", switches=[SW1]), self.cfg, {}), [])

    def test_severity_mapping(self):
        for sev, want in (("critical", "critical"), ("warn", "warning"), ("info", "low"), ("zzz", "warning")):
            e = extract(alarm("a", "device_down", severity=sev, aps=[AP1]), self.cfg, {})[0]
            self.assertEqual(e.payload["severity"], want)

    def test_categories(self):
        def cat(t, **kw):
            return extract(alarm("a", t, **kw), self.cfg, {})[0].payload["labels"]["mist_category"]
        self.assertEqual(cat("port_flap", switches=[SW1]), "infra")       # 'ap' inside 'flap'
        self.assertEqual(cat("loop_detected_by_ap", aps=[AP1]), "infra")
        self.assertEqual(cat("rogue_ap", aps=[AP1]), "wifi")
        self.assertEqual(cat("device_down", aps=[AP1]), "wifi")
        self.assertEqual(cat("device_down", switches=[SW1]), "infra")
        self.assertEqual(cat("auth_failure", switches=[SW1]), "wifi")

    def test_marvis_lifecycle_uses_alarm_id(self):
        o = extract(alarm("m1", "port_flap", status="open", switches=[SW1]), self.cfg, {})[0]
        r = extract(alarm("m1", "port_flap", status="resolved", resolved_time=NOW,
                          switches=[SW1]), self.cfg, {})[0]
        self.assertEqual(o.fingerprint, r.fingerprint)
        self.assertTrue(o.fingerprint.startswith("mist:alarm:m1:"))
        self.assertFalse(o.oneshot)

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            extract({"type": "device_down"}, self.cfg, {})
        with self.assertRaises(ValueError):
            extract({"id": "x", "type": "device_down", "timestamp": "nope"}, self.cfg, {})

    def test_missing_device_info_still_yields_one_event(self):
        evs = extract(alarm("a", "vc_master_changed"), self.cfg, {})
        self.assertEqual(len(evs), 1)
        self.assertTrue(evs[0].fingerprint.endswith(":org"))


class LifecycleTests(unittest.TestCase):
    def test_cold_start_posts_open_state_once_and_not_history(self):
        p, mist, keep, clock = make()
        mist.alarms = [
            alarm("d1", "device_down", ts=NOW - 7200, aps=[AP1]),           # still down
            alarm("d2", "device_down", ts=NOW - 7200, aps=[AP2]),
            alarm("r2", "device_reconnected", ts=NOW - 3600, aps=[AP2]),    # recovered
            alarm("x", "device_restarted", ts=NOW - 90000, aps=[AP1]),      # old one-shot
        ]
        p.run_cycle()
        self.assertEqual(statuses(keep), [("device_down", "firing")])
        self.assertEqual(keep.posted[0]["labels"]["mist_mac"], AP1)
        clock.t += 60
        p.run_cycle()
        self.assertEqual(len(keep.posted), 1)           # restart/replay does not storm

    def test_overlap_does_not_duplicate(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        for _ in range(4):
            p.run_cycle()
            clock.t += 60
        self.assertEqual(statuses(keep), [("device_down", "firing")])

    def test_recovery_resolves_same_fingerprint(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        p.run_cycle()
        clock.t += 60
        mist.alarms.append(alarm("r1", "device_reconnected", ts=clock.t - 10, aps=[AP1]))
        p.run_cycle()
        self.assertEqual(statuses(keep), [("device_down", "firing"), ("device_down", "resolved")])
        self.assertEqual(keep.posted[0]["fingerprint"], keep.posted[1]["fingerprint"])
        self.assertEqual(keep.posted[1]["labels"]["mist_state"], "resolved")

    def test_recovery_without_known_firing_is_ignored(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("r1", "device_reconnected", ts=clock.t - 10, aps=[AP1])]
        p.run_cycle()
        self.assertEqual(keep.posted, [])

    def test_same_poll_down_then_up_in_order(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("r1", "device_reconnected", ts=clock.t - 10, aps=[AP1]),
                       alarm("d1", "device_down", ts=clock.t - 50, aps=[AP1])]   # listed out of order
        p.run_cycle()
        self.assertEqual(statuses(keep), [("device_down", "firing"), ("device_down", "resolved")])

    def test_device_joining_existing_aggregated_alarm_fires_once(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 100, aps=[AP1], count=1)]
        p.run_cycle()
        clock.t += 60
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 160, last_seen=clock.t - 5,
                             aps=[AP1, AP2], count=2)]
        p.run_cycle()
        self.assertEqual([x["labels"]["mist_mac"] for x in keep.posted], [AP1, AP2])

    def test_marvis_open_then_resolved(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("m1", "port_flap", ts=clock.t - 30, status="open", switches=[SW1],
                             severity="warn")]
        p.run_cycle()
        clock.t += 60
        mist.alarms = [alarm("m1", "port_flap", ts=clock.t - 90, status="resolved",
                             resolved_time=clock.t - 5, switches=[SW1], severity="warn")]
        p.run_cycle()
        self.assertEqual(statuses(keep), [("port_flap", "firing"), ("port_flap", "resolved")])

    def test_oneshot_auto_resolves(self):
        p, mist, keep, clock = bootstrapped(auto_resolve_minutes=30)
        mist.alarms = [alarm("x", "device_restarted", ts=clock.t - 10, aps=[AP1], severity="info")]
        p.run_cycle()
        self.assertEqual(statuses(keep), [("device_restarted", "firing")])
        clock.t += 31 * 60
        p.run_cycle()
        self.assertEqual(statuses(keep)[-1], ("device_restarted", "resolved"))
        self.assertEqual(keep.posted[-1]["labels"]["mist_state"], "auto_resolved")
        clock.t += 60
        p.run_cycle()
        self.assertEqual(len(keep.posted), 2)

    def test_mist_failure_does_not_advance_cursor(self):
        p, mist, keep, clock = bootstrapped()
        before = p.state.get("last_success")
        mist.fail = True
        with self.assertRaises(MistError):
            p.run_cycle()
        self.assertEqual(p.state.get("last_success"), before)
        mist.fail = False
        clock.t += 60
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        p.run_cycle()
        self.assertEqual(len(keep.posted), 1)

    def test_keep_outage_queues_and_preserves_order(self):
        p, mist, keep, clock = bootstrapped()
        keep.down = True
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        p.run_cycle()
        clock.t += 60
        mist.alarms.append(alarm("r1", "device_reconnected", ts=clock.t - 5, aps=[AP1]))
        p.run_cycle()
        self.assertEqual(keep.posted, [])
        self.assertEqual(p.state.pending_stats()[0], 2)
        keep.down = False
        clock.t += 600
        p.drain()
        self.assertEqual(statuses(keep), [("device_down", "firing"), ("device_down", "resolved")])
        self.assertEqual(p.state.pending_stats()[0], 0)

    def test_restart_with_existing_db_does_not_replay(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        p.run_cycle()
        p2 = Poller(p.cfg, mist, keep, p.state, clock)      # "restart" on the same DB
        clock.t += 60
        p2.run_cycle()
        self.assertEqual(len(keep.posted), 1)

    def test_dry_run_posts_nothing(self):
        cfg = dataclasses.replace(Config(), dry_run=True, org_id="o", mist_token="t")
        mist, keep, clock = FakeMist(), FakeKeep(), Clock()
        mist.alarms = [alarm("d1", "device_down", ts=NOW - 60, aps=[AP1])]
        p = Poller(cfg, mist, keep, State(":memory:"), clock)
        p.run_cycle()
        self.assertEqual(keep.posted, [])
        self.assertEqual(p.state.pending_stats()[0], 0)

    def test_suppressed_types_never_queue(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("a%d" % i, "infra_arp_failure", ts=clock.t - 5, switches=[SW1])
                       for i in range(50)]
        p.run_cycle()
        self.assertEqual(keep.posted, [])


if __name__ == "__main__":
    unittest.main()
