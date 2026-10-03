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

    devices = {AP1: {"ip": "10.1.2.3", "name": "AP-ONE", "model": "AP45", "status": ""}}

    def list_devices(self):
        return dict(self.devices)


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

    def test_switch_and_chassis_pairs_share_fingerprint(self):
        for fire, rec in (("switch_down", "switch_reconnected"),
                          ("sw_alarm_chassis_pem", "sw_alarm_chassis_pem_clear"),
                          ("sw_critical_port_down", "sw_critical_port_up")):
            f = extract(alarm("a", fire, switches=[SW1]), self.cfg, {})[0]
            r = extract(alarm("b", rec, switches=[SW1]), self.cfg, {})[0]
            self.assertEqual(f.fingerprint, r.fingerprint, fire)
            self.assertEqual((f.phase, r.phase), ("fire", "resolve"))
            self.assertFalse(f.oneshot)

    def test_unpaired_type_is_oneshot(self):
        e = extract(alarm("a", "switch_restarted", switches=[SW1]), self.cfg, {})[0]
        self.assertTrue(e.oneshot)

    def test_ip_and_name_from_device_cache(self):
        devs = {AP1: {"ip": "10.1.2.3", "name": "AP-ONE", "model": "AP45",
                      "version": "0.14.1", "last_seen": NOW - 120}}
        a = alarm("a", "device_down", aps=[AP1, AP2], hostnames=["only-one"])   # misaligned
        e1, e2 = extract(a, self.cfg, {}, devs)
        self.assertEqual(e1.payload["labels"]["mist_ip"], "10.1.2.3")
        self.assertEqual(e1.payload["labels"]["mist_device"], "AP-ONE")
        self.assertEqual(e1.payload["labels"]["mist_model"], "AP45")
        self.assertEqual(e2.payload["labels"]["mist_ip"], "n/a")
        self.assertEqual(e1.payload["labels"]["mist_firmware"], "0.14.1")
        self.assertEqual(e1.payload["labels"]["mist_last_seen"][:10], "2026-09-21")
        self.assertEqual(e2.payload["labels"]["mist_last_seen"], "n/a")
        self.assertEqual(e2.payload["labels"]["mist_model"], "n/a")
        self.assertEqual(e2.payload["labels"]["mist_device"], AP2)

    def test_device_lookup_failure_is_not_fatal(self):
        p, mist, keep, clock = make()

        def boom():
            raise MistError("nope")
        mist.list_devices = boom
        mist.alarms = [alarm("d1", "device_down", ts=NOW - 60, aps=[AP1])]
        p.run_cycle()
        self.assertEqual(keep.posted[0]["labels"]["mist_ip"], "n/a")

    def test_marvis_identity_from_impacted_entities(self):
        a = alarm("m1", "port_flap", status="open", group="marvis", severity="warn",
                  impacted_entities=[{"entity_mac": "5C:5B:35:AA:00:09", "entity_name": "SW-DAVIS-1",
                                      "entity_type": "switch"}])
        e = extract(a, self.cfg, {})[0]
        self.assertEqual(e.payload["labels"]["mist_device"], "SW-DAVIS-1")
        self.assertEqual(e.payload["labels"]["mist_mac"], "5c5b35aa0009")
        self.assertEqual(e.payload["labels"]["mist_category"], "infra")

    def test_security_group_and_prefixes_route(self):
        wifi = extract(alarm("a", "krack_attack", group="security"), self.cfg, {})[0]
        infra = extract(alarm("b", "sw_bpdu_error", aps=[AP1]), self.cfg, {})[0]
        self.assertEqual(wifi.payload["labels"]["mist_category"], "wifi")
        self.assertEqual(infra.payload["labels"]["mist_category"], "infra")

    def test_normal_severity_maps_low(self):
        e = extract(alarm("a", "device_down", severity="normal", aps=[AP1]), self.cfg, {})[0]
        self.assertEqual(e.payload["severity"], "low")


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
        mist.alarms = [alarm("x", "vc_master_changed", ts=clock.t - 10, switches=[SW1], severity="critical")]
        p.run_cycle()
        self.assertEqual(statuses(keep), [("vc_master_changed", "firing")])
        clock.t += 31 * 60
        p.run_cycle()
        self.assertEqual(statuses(keep)[-1], ("vc_master_changed", "resolved"))
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

    def test_stale_open_alarm_found_by_timestamp_lookup(self):
        p, mist, keep, clock = bootstrapped()
        old_ts = clock.t - 10 * 86400
        open_a = alarm("m1", "port_flap", ts=old_ts, status="open", switches=[SW1], severity="warn")
        mist.alarms = []                          # too old for the wide window
        p.state.upsert_alert("mist:alarm:m1:" + SW1, "firing", old_ts, "m1", old_ts, None,
                             extract(open_a, p.cfg, {})[0].payload, clock.t)
        resolved = dict(open_a, status="resolved", resolved_time=clock.t - 5)
        looked_up = []
        orig = mist.search_alarms

        def fake(start, end):
            looked_up.append((start, end))
            if end - start == 60:                 # targeted lookup around the old timestamp
                return [resolved]
            return orig(start, end)
        mist.search_alarms = fake
        clock.t += 1000                           # force a wide reconcile
        p.run_cycle()
        self.assertTrue(any(e - s == 60 and s < old_ts < e for s, e in looked_up))
        self.assertEqual(statuses(keep), [("port_flap", "resolved")])

    def _down_with_status(self, status, **cfg):
        p, mist, keep, clock = bootstrapped(device_refresh_s=60, self_correct_s=60, **cfg)
        mist.devices = {AP1: {"ip": "10.0.0.1", "name": "AP-ONE", "model": "AP45", "status": status}}
        mist.alarms = [alarm("d1", "device_down", ts=clock.t - 30, aps=[AP1])]
        return p, mist, keep, clock

    def test_self_correct_resolves_when_mist_says_connected(self):
        p, mist, keep, clock = self._down_with_status("connected")
        p.run_cycle()                                 # fires; first connected observation recorded
        self.assertEqual(statuses(keep), [("device_down", "firing")])
        for _ in range(3):
            clock.t += 60
            p.run_cycle()
        self.assertEqual(statuses(keep), [("device_down", "firing"), ("device_down", "resolved")])
        self.assertEqual(keep.posted[1]["labels"]["mist_state"], "auto_corrected")
        self.assertEqual(keep.posted[0]["fingerprint"], keep.posted[1]["fingerprint"])

    def test_self_correct_leaves_disconnected_devices_alone(self):
        p, mist, keep, clock = self._down_with_status("disconnected")
        for _ in range(5):
            p.run_cycle()
            clock.t += 60
        self.assertEqual(statuses(keep), [("device_down", "firing")])

    def test_self_correct_ignores_cache_older_than_alert(self):
        p, mist, keep, clock = self._down_with_status("connected", )
        p.cfg = dataclasses.replace(p.cfg, device_refresh_s=100000)   # cache stays from before the alarm
        for _ in range(5):
            p.run_cycle()
            clock.t += 60
        self.assertEqual(statuses(keep), [("device_down", "firing")])

    def test_info_oneshot_events_are_not_posted(self):
        p, mist, keep, clock = bootstrapped()
        mist.alarms = [alarm("r", "device_restarted", ts=clock.t - 10, aps=[AP1], severity="info"),
                       alarm("s", "switch_restarted", ts=clock.t - 10, switches=[SW1], severity="info")]
        p.run_cycle()
        self.assertEqual(keep.posted, [])

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
