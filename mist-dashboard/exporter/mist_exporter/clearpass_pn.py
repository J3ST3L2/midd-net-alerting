"""PantherNet adoption from ClearPass authentication logs, for the whole campus.

ClearPass is the RADIUS server for PantherNet on BOTH Mist and Aruba access points. It pushes its
authentication records over syslog (Administration > External Servers > Syslog Export Filters), one per RADIUS
authentication, with the username, client MAC, SSID and the NAS (access point or controller) address. That gives
the same three numbers the Mist row computes, vendor-independent, with the username on every record, so there is
no per-device lookup and no multi-hour history load:

  * unique clients per SSID (successful authentications),
  * PantherNet authentication failures (and unique users behind them), and
  * fallback: a PantherNet failure followed by a MiddleburyCollege success by the same user within the
    window, judged only after the window has passed (so it lags by that long).

Every number is exported for ``platform="campus"``; with ``CLEARPASS_ARUBA_NAS`` set to the Aruba controller
addresses it is also split into ``aruba`` and ``other`` (the Mist access points) by the NAS address on the
record. A failure belongs to the platform it happened on; a fallback is a MiddleburyCollege success on either.

Records are parsed tolerantly: ClearPass can export CEF or a plain key=value format, and field names differ
between export templates, so fields are matched by normalised name from candidate lists. Only field NAMES are
ever logged; usernames and MACs live in memory, are never a metric label, never logged, never written to disk.
There is no history to reload, so after a restart the counts are partial until a full lookback of events has
been received, and are withheld until then (see ``min_history_s``).
"""
import collections
import ipaddress
import re
import threading
import time

from .metrics import Family, _mac
from .pn_fallback import normalize_username

# Candidate field names, normalised (lowercase, letters and digits only). An exact match wins over a
# suffix match, so "Common.Username" and "Username" both resolve to the username.
USER = ("username", "suser", "duser", "user")
MAC = ("callingstationid", "smac", "srcmac", "clientmac", "macaddress", "mac")
SSID = ("arubaessidname", "essidname", "essid", "ssid")
NAS = ("nasipaddress", "nasip", "nasaddress", "src")   # the access point or controller that sent the request
SERVICE = ("destinationservicename", "servicename", "service")   # the ClearPass service that handled it
CALLED = ("calledstationid",)                      # "AA-BB-CC-DD-EE-FF:SSID" on some templates
STATUS = ("loginstatus", "outcome", "authstatus", "result", "status")
ERROR = ("errorcode", "reason", "alerts", "msg", "message")

ACCEPT = frozenset({"accept", "accepted", "success", "succeeded", "allow", "allowed", "ok"})
REJECT_PREFIXES = ("reject", "fail", "deny", "denied", "timeout", "error")

MAX_EVENTS = 500_000           # hard caps: a flood from an allowed sender must not grow memory without bound
MAX_CLIENTS = 500_000          # per SSID / for users with a MiddleburyCollege success
MAX_PER_USER = 50
MAX_REASONS = 12               # keeps the reason label bounded however many error codes ClearPass has
MAX_FIELD_NAMES = 200          # field names remembered for the "what does my export contain" log
_PRI = re.compile(r"^<\d+>")
# A key is a word followed by "=", at the start of the text or after whitespace. Matching only the key
# (never the value) keeps this linear: a lazy "(.*?)(?=...)" pattern backtracks quadratically on junk.
_KEY = re.compile(r"(?:^|(?<=\s))([A-Za-z][\w.\-]*)=")
MAX_LINE = 16384


# Like _KEY, but a key may also follow "|" (the start of a CEF extension).
_SHAPE_KEY = re.compile(r"(?:^|(?<=[\s|]))([A-Za-z][\w.\-]*)=")


def shape(raw, limit=1500):
    """The layout of a record with every value masked: keys (a word followed by "=", at the start or after
    whitespace or "|") are kept, every other word, number, address and name becomes "x", separators are kept.
    The value of a CEF "...Label" key is kept too: it names a field (cs1Label=Auth.Username), it is not data.
    Safe to log: it shows the format of a record without any username, MAC, address or SSID in it."""
    s = raw[:limit]
    keys = {m.start(): m for m in _SHAPE_KEY.finditer(s)}
    out, i, keep = [], 0, False                      # keep: inside the value of a "...Label" key
    for m in re.finditer(r"[\w.:@\/+\-]+", s):
        if m.start() < i:
            continue                                 # inside a key already emitted
        out.append(s[i:m.start()])
        key = keys.get(m.start())
        if key is not None:
            out.append(key.group(0))                 # "name=" stays
            keep = key.group(1).lower().endswith("label")
            i = key.end()
        elif keep:
            out.append(m.group(0))                   # a label's value, up to the next key
            i = m.end()
        else:
            out.append("x")
            i = m.end()
    out.append(s[i:])
    return re.sub(r"x(?:x)+", "x", "".join(out))


def _norm_key(k):
    return re.sub(r"[^a-z0-9]", "", k.lower())


def parse_fields(raw):
    """key -> value from one syslog line, CEF or plain key=value. Raises nothing; {} if nothing found."""
    line = _PRI.sub("", raw[:MAX_LINE].strip())
    if "CEF:" in line:
        line = line.split("CEF:", 1)[1]
        parts = line.split("|", 7)                  # version|vendor|product|devver|sigid|name|severity|ext
        ext = parts[7] if len(parts) == 8 else ""
        fields = {"cefname": parts[5]} if len(parts) >= 6 else {}
    else:
        ext, fields = line, {}
    hits = list(_KEY.finditer(ext))
    for i, hit in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(ext)
        fields.setdefault(hit.group(1), ext[hit.end():end].strip())
    # CEF sends a custom column as csN=value plus csNLabel=Column Name. The label is the field's real name,
    # so make the value readable under it as well ("cs4Label=Login Status" -> "Login Status").
    for key in [k for k in fields if k.endswith("Label") and len(k) > 5]:
        base, label = key[:-5], fields[key]
        if label and base in fields:
            fields.setdefault(label, fields[base])
    return fields


def _pick(fields, names):
    """First value whose normalised key equals, then ends with, one of the candidate names."""
    norm = {_norm_key(k): v for k, v in fields.items()}
    for name in names:
        if norm.get(name):
            return norm[name]
    for name in names:
        for key, value in norm.items():
            if key.endswith(name) and value:
                return value
    return ""


def _status(value):
    v = value.strip().lower()
    if v in ACCEPT:
        return "accept"
    return "reject" if v.startswith(REJECT_PREFIXES) else None


def _reason(fields):
    """Bounded failure reason: the ClearPass error code when numeric, else a coarse keyword bucket."""
    raw = _pick(fields, ERROR)
    code = re.search(r"\b(\d{3,6})\b", raw)
    if code:
        return code.group(1)
    return "timeout" if "timeout" in raw.lower() else "other"


def _called_ssid(value):
    """SSID from a Called-Station-Id like 'AA-BB-CC-DD-EE-FF:PantherNet'."""
    m = re.match(r"^[0-9A-Fa-f]{2}([-:.]?[0-9A-Fa-f]{2}){5}[:]\s*(.+)$", value.strip())
    return m.group(2).strip() if m else ""


def extract(fields):
    """The few facts we need from a parsed record, or None if it is not an authentication result."""
    status = _status(_pick(fields, STATUS))
    if status is None:
        return None
    ssid = _pick(fields, SSID) or _called_ssid(_pick(fields, CALLED))
    user = normalize_username(_pick(fields, USER))
    mac = _mac(_pick(fields, MAC))
    return {"status": status, "ssid": ssid, "user": user, "mac": mac, "nas": _pick(fields, NAS),
            "reason": _reason(fields) if status == "reject" else ""}


class ClearPassTracker:
    def __init__(self, pn_ssid, mc_ssid, window_s, lookback_s, min_history_s=None, noise=(),
                 aruba_nas=(), clock=time.time):
        self.pn_ssid, self.mc_ssid = pn_ssid.lower(), mc_ssid.lower()
        self._names = {self.pn_ssid: pn_ssid, self.mc_ssid: mc_ssid}
        self.window_s, self.lookback_s = window_s, lookback_s
        self.min_history_s = lookback_s if min_history_s is None else min_history_s
        self.noise, self.clock = frozenset(str(n) for n in noise), clock
        self.aruba_nas = list(aruba_nas)
        # campus is always exported; the split needs the Aruba controller addresses to tell the two apart
        self.scopes = ("campus", "aruba", "other") if self.aruba_nas else ("campus",)
        self._lock = threading.Lock()
        self._started = clock()
        self._seen = {self.pn_ssid: {}, self.mc_ssid: {}}      # ssid -> {client key: (last accepted ts, platform)}
        self._fails = collections.deque(maxlen=MAX_EVENTS)      # (ts, user, mac, reason, platform) PantherNet rejects
        self._mc_ok = {}                                       # user -> [ts] MiddleburyCollege accepts, any platform
        self.received = self.unmapped = self.dropped = self.unclassified = 0
        self._dropped_by = collections.Counter()           # source address -> records dropped (bounded)
        self._last_unmapped_raw = ""                       # memory only; shown only as a masked layout
        self._last_event = 0
        self._field_names = collections.Counter()
        self._services, self._ssids = collections.Counter(), collections.Counter()   # names of networks, not people
        self._cache = (0, None)

    def platform_of(self, nas):
        """aruba / other by the NAS address on the record, or unclassified when there is none to judge by."""
        if not self.aruba_nas:
            return "campus"
        try:
            addr = ipaddress.ip_address(nas.strip())
        except ValueError:
            return "unclassified"
        return "aruba" if any(addr in net for net in self.aruba_nas) else "other"

    # ---- ingestion ---------------------------------------------------------------------------
    def handle_line(self, raw):
        """Parse and record one syslog line. Never raises, never logs content."""
        now = self.clock()
        try:
            fields = parse_fields(raw)
            with self._lock:
                for k in fields:
                    if len(self._field_names) < MAX_FIELD_NAMES:
                        self._field_names[k] += 1
                self._bump(self._services, _pick(fields, SERVICE)[:80])
                rec = extract(fields)
                if rec is not None:
                    self._bump(self._ssids, rec["ssid"][:80])
                if rec is None or not rec["ssid"] or not (rec["user"] or rec["mac"]):
                    self.unmapped += 1
                    self._last_unmapped_raw = raw[:MAX_LINE]
                    return False
                self.received += 1
                self._last_event = now
                rec["platform"] = self.platform_of(rec["nas"])
                if rec["platform"] == "unclassified":
                    self.unclassified += 1
                self._record(rec, now)
            return True
        except Exception:
            with self._lock:
                self.unmapped += 1
            return False

    def count_dropped(self, ip=""):
        with self._lock:
            self.dropped += 1
            if ip:
                self._dropped_by[ip if (ip in self._dropped_by or len(self._dropped_by) < 50) else "(others)"] += 1

    def dropped_sources(self, top=8):
        """[(source address, records dropped)], busiest first. Addresses only, never any content."""
        with self._lock:
            return self._dropped_by.most_common(top)

    def unmapped_layout(self):
        """Value-masked layout of the most recent record that could not be used ('' if none)."""
        with self._lock:
            return shape(self._last_unmapped_raw) if self._last_unmapped_raw else ""

    def _record(self, rec, now):
        ssid, user, mac = rec["ssid"].lower(), rec["user"], rec["mac"]
        if rec["status"] == "accept":
            seen = self._seen.get(ssid)
            if seen is not None and ((mac or user) in seen or len(seen) < MAX_CLIENTS):
                seen[mac or user] = (now, rec["platform"])
            if ssid == self.mc_ssid and user and (user in self._mc_ok or len(self._mc_ok) < MAX_CLIENTS):
                stamps = self._mc_ok.setdefault(user, [])
                if len(stamps) < MAX_PER_USER:
                    stamps.append(now)
        elif ssid == self.pn_ssid:
            self._fails.append((now, user, mac, rec["reason"], rec["platform"]))

    @staticmethod
    def _bump(counter, value):
        """Count a non-personal value (a service or SSID name), keeping at most 50 distinct ones."""
        if value and (value in counter or len(counter) < 50):
            counter[value] += 1

    def top_values(self, top=8):
        """(service names, SSIDs) seen, busiest first. These name networks and ClearPass services only."""
        with self._lock:
            return self._services.most_common(top), self._ssids.most_common(top)

    def field_names(self):
        """Names (never values) of the fields seen so far: what to map if the export differs."""
        with self._lock:
            return sorted(self._field_names)

    # ---- results -----------------------------------------------------------------------------
    def _prune(self, now):
        cutoff = now - self.lookback_s
        while self._fails and self._fails[0][0] < cutoff:
            self._fails.popleft()
        for ssid, seen in self._seen.items():
            self._seen[ssid] = {k: v for k, v in seen.items() if v[0] >= cutoff}
        self._mc_ok = {u: [t for t in ts if t >= cutoff] for u, ts in self._mc_ok.items()}
        self._mc_ok = {u: ts for u, ts in self._mc_ok.items() if ts}

    def history_s(self):
        return self.clock() - self._started

    def families(self, ttl=5):
        """Exported families, recomputed at most every `ttl` seconds."""
        now = self.clock()
        with self._lock:
            when, cached = self._cache
            if cached is not None and now - when < ttl:
                return cached
            self._prune(now)
            out = self._build(now)
            self._cache = (now, out)
            return out

    def _scope(self, now, scope):
        """Every number for one platform. A failure belongs to the platform it happened on; a fallback is a
        MiddleburyCollege success on any platform, since the user simply moved to the other network."""
        def keep(platform):
            return scope == "campus" or platform == scope

        fails = [f for f in self._fails if keep(f[4])]
        counted = [f for f in fails if f[3] not in self.noise]
        users = collections.defaultdict(list)
        for ts, user, _mac_, _r, _p in counted:
            if user:
                users[user].append(ts)
        eligible = fallback = 0
        for user, stamps in users.items():
            mature = [t for t in stamps if t <= now - self.window_s]
            if not mature:
                continue
            eligible += 1
            ok = self._mc_ok.get(user, [])
            fallback += any(t < s <= t + self.window_s for t in mature for s in ok)
        reasons = collections.Counter(f[3] for f in fails)
        top = dict(reasons.most_common(MAX_REASONS))
        top["other"] = top.get("other", 0) + sum(n for r, n in reasons.items() if r not in top)
        return {
            "unique": {self._names[s]: sum(1 for v in seen.values() if keep(v[1]))
                       for s, seen in sorted(self._seen.items())},
            "events": len(counted), "users": len(users),
            "unresolved": len({m for _, u, m, _r, _p in counted if not u and m}),
            "eligible": eligible, "fallback": fallback, "reasons": top}

    def _build(self, now):
        ready = self.history_s() >= self.min_history_s
        by = {scope: self._scope(now, scope) for scope in self.scopes}

        def gauge(name, help_, per_scope, kind="gauge", always=False):
            """per_scope(stats) -> [(extra labels, value)]; one set of samples per platform."""
            samples = []
            if ready or always:
                for scope, st in by.items():
                    samples += [(dict(labels, platform=scope), float(v)) for labels, v in per_scope(st)]
            return Family(name, help_, kind, samples)

        def plain(name, help_, value, kind="gauge", present=True):
            return Family(name, help_, kind, [({}, float(value))] if present else [])

        return [
            gauge("clearpass_pn_unique_clients", "Unique client devices with a successful authentication in the "
                  "last 24 h, per SSID and platform (from ClearPass).",
                  lambda st: [({"ssid": s}, n) for s, n in st["unique"].items()]),
            gauge("clearpass_pn_auth_failure_events", "PantherNet authentication failures in the last 24 h.",
                  lambda st: [({}, st["events"])]),
            gauge("clearpass_pn_auth_failure_clients", "Unique users with at least one PantherNet failure.",
                  lambda st: [({}, st["users"])]),
            gauge("clearpass_pn_auth_failure_clients_unresolved", "Failing devices with no username.",
                  lambda st: [({}, st["unresolved"])]),
            gauge("clearpass_pn_fallback_eligible_clients", "Failing users whose failure is older than the window.",
                  lambda st: [({}, st["eligible"])]),
            gauge("clearpass_pn_fallback_clients", "Eligible users with a MiddleburyCollege success within the "
                  "window after a PantherNet failure.", lambda st: [({}, st["fallback"])]),
            gauge("clearpass_pn_fallback_rate", "fallback_clients / eligible_clients (omitted when none).",
                  lambda st: [({}, st["fallback"] / st["eligible"])] if st["eligible"] else []),
            gauge("clearpass_pn_failure_reason_events", "PantherNet failures by ClearPass error code (top %d, the "
                  "rest as 'other')." % MAX_REASONS,
                  lambda st: [({"reason": r}, n) for r, n in sorted(st["reasons"].items())]),
            plain("clearpass_pn_history_seconds", "Seconds of events received since the exporter started.",
                  self.history_s()),
            plain("clearpass_pn_history_complete", "1 once a full lookback of events has been received.",
                  1.0 if ready else 0.0),
            plain("clearpass_pn_last_event_timestamp_seconds", "Unix time of the last usable ClearPass record.",
                  self._last_event, present=bool(self._last_event)),
            plain("clearpass_pn_events_received_total", "Usable ClearPass records received.", self.received,
                  "counter"),
            plain("clearpass_pn_unmapped_events_total", "Records that could not be used (not an authentication "
                  "result, or no SSID / username / MAC found).", self.unmapped, "counter"),
            plain("clearpass_pn_unclassified_events_total", "Records with no NAS address to tell Aruba from other "
                  "access points by (only with CLEARPASS_ARUBA_NAS set).", self.unclassified, "counter"),
            plain("clearpass_pn_dropped_events_total", "Records dropped because they came from a source address "
                  "that is not allowed.", self.dropped, "counter"),
        ]
