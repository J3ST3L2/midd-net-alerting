"""PantherNet adoption on Aruba APs, from ClearPass authentication logs.

ClearPass is the RADIUS server for the Aruba side. It pushes its authentication records over syslog
(Administration > External Servers > Syslog Export Filters), one record per RADIUS authentication, with
the username, client MAC and SSID. That gives the same three numbers the Mist side computes, with the
username present on every record, so there is no per-device lookup and no multi-hour history load:

  * unique clients per SSID (successful authentications),
  * PantherNet authentication failures (and unique users behind them), and
  * fallback: a PantherNet failure followed by a MiddleburyCollege success by the same user within the
    window, judged only after the window has passed (so it lags by that long).

Records are parsed tolerantly: ClearPass can export CEF or a plain key=value format, and field names
differ between export templates, so fields are matched by normalised name from candidate lists. Only
field NAMES are ever logged; usernames and MACs live in memory, are never a metric label, never logged,
never written to disk. There is no history to reload, so after a restart the counts are partial until a
full lookback of events has been received, and are withheld until then (see ``min_history_s``).
"""
import collections
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
    return {"status": status, "ssid": ssid, "user": user, "mac": mac,
            "reason": _reason(fields) if status == "reject" else ""}


class ClearPassTracker:
    def __init__(self, pn_ssid, mc_ssid, window_s, lookback_s, min_history_s=None, noise=(), clock=time.time):
        self.pn_ssid, self.mc_ssid = pn_ssid.lower(), mc_ssid.lower()
        self._names = {self.pn_ssid: pn_ssid, self.mc_ssid: mc_ssid}
        self.window_s, self.lookback_s = window_s, lookback_s
        self.min_history_s = lookback_s if min_history_s is None else min_history_s
        self.noise, self.clock = frozenset(str(n) for n in noise), clock
        self._lock = threading.Lock()
        self._started = clock()
        self._seen = {self.pn_ssid: {}, self.mc_ssid: {}}      # ssid -> {client key: last accepted ts}
        self._fails = collections.deque(maxlen=MAX_EVENTS)      # (ts, user, mac, reason) PantherNet rejects
        self._mc_ok = {}                                       # user -> [ts] MiddleburyCollege accepts
        self.received = self.unmapped = self.dropped = 0
        self._last_event = 0
        self._field_names = collections.Counter()
        self._cache = (0, None)

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
                rec = extract(fields)
                if rec is None or not rec["ssid"] or not (rec["user"] or rec["mac"]):
                    self.unmapped += 1
                    return False
                self.received += 1
                self._last_event = now
                self._record(rec, now)
            return True
        except Exception:
            with self._lock:
                self.unmapped += 1
            return False

    def count_dropped(self):
        with self._lock:
            self.dropped += 1

    def _record(self, rec, now):
        ssid, user, mac = rec["ssid"].lower(), rec["user"], rec["mac"]
        if rec["status"] == "accept":
            seen = self._seen.get(ssid)
            if seen is not None and ((mac or user) in seen or len(seen) < MAX_CLIENTS):
                seen[mac or user] = now
            if ssid == self.mc_ssid and user and (user in self._mc_ok or len(self._mc_ok) < MAX_CLIENTS):
                stamps = self._mc_ok.setdefault(user, [])
                if len(stamps) < MAX_PER_USER:
                    stamps.append(now)
        elif ssid == self.pn_ssid:
            self._fails.append((now, user, mac, rec["reason"]))

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
            self._seen[ssid] = {k: t for k, t in seen.items() if t >= cutoff}
        self._mc_ok = {u: [t for t in ts if t >= cutoff] for u, ts in self._mc_ok.items()}
        self._mc_ok = {u: ts for u, ts in self._mc_ok.items() if ts}

    def history_s(self):
        return self.clock() - self._started

    def families(self, ttl=15):
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

    def _build(self, now):
        ready = self.history_s() >= self.min_history_s
        counted = [f for f in self._fails if f[3] not in self.noise]
        users = collections.defaultdict(list)
        for ts, user, _mac_, _r in counted:
            if user:
                users[user].append(ts)
        unresolved = len({m for _, u, m, _r in counted if not u and m})
        eligible = fallback = 0
        for user, stamps in users.items():
            mature = [t for t in stamps if t <= now - self.window_s]
            if not mature:
                continue
            eligible += 1
            ok = self._mc_ok.get(user, [])
            fallback += any(t < s <= t + self.window_s for t in mature for s in ok)
        reasons = collections.Counter(r for _, _, _, r in self._fails)
        top = dict(reasons.most_common(MAX_REASONS))
        top["other"] = top.get("other", 0) + sum(n for r, n in reasons.items() if r not in top)

        def gauge(name, help_, samples, kind="gauge"):
            return Family(name, help_, kind, samples if ready or name in ALWAYS else [])

        return [
            gauge("aruba_pn_unique_clients", "Unique client devices with a successful authentication in the "
                  "last 24 h, per SSID (Aruba, from ClearPass).",
                  [({"ssid": self._names[s]}, float(len(v))) for s, v in sorted(self._seen.items())]),
            gauge("aruba_pn_auth_failure_events", "PantherNet authentication failures in the last 24 h "
                  "(Aruba, from ClearPass).", [({}, float(len(counted)))]),
            gauge("aruba_pn_auth_failure_clients", "Unique users with at least one PantherNet failure.",
                  [({}, float(len(users)))]),
            gauge("aruba_pn_auth_failure_clients_unresolved", "Failing devices with no username.",
                  [({}, float(unresolved))]),
            gauge("aruba_pn_fallback_eligible_clients", "Failing users whose failure is older than the window.",
                  [({"match": "user"}, float(eligible))]),
            gauge("aruba_pn_fallback_clients", "Eligible users with a MiddleburyCollege success within the "
                  "window after a PantherNet failure.", [({"match": "user"}, float(fallback))]),
            gauge("aruba_pn_fallback_rate", "fallback_clients / eligible_clients (omitted when none).",
                  [({"match": "user"}, fallback / eligible)] if eligible else []),
            gauge("aruba_pn_failure_reason_events", "PantherNet failures by ClearPass error code (top "
                  "%d, rest as 'other')." % MAX_REASONS, [({"reason": r}, float(n)) for r, n in sorted(top.items())]),
            gauge("aruba_pn_history_seconds", "Seconds of events received since the exporter started.",
                  [({}, float(self.history_s()))]),
            gauge("aruba_pn_history_complete", "1 once a full lookback of events has been received.",
                  [({}, 1.0 if ready else 0.0)]),
            gauge("aruba_pn_last_event_timestamp_seconds", "Unix time of the last usable ClearPass record.",
                  [({}, float(self._last_event))] if self._last_event else []),
            gauge("aruba_pn_events_received_total", "Usable ClearPass records received.",
                  [({}, float(self.received))], "counter"),
            gauge("aruba_pn_unmapped_events_total", "Records that could not be used (not an authentication "
                  "result, or no SSID / username / MAC found).", [({}, float(self.unmapped))], "counter"),
            gauge("aruba_pn_dropped_events_total", "Records dropped because they came from a source address "
                  "that is not allowed.", [({}, float(self.dropped))], "counter"),
        ]


# Status metrics stay visible while the history fills, so a quiet or broken feed is obvious.
ALWAYS = frozenset({"aruba_pn_history_seconds", "aruba_pn_history_complete", "aruba_pn_last_event_timestamp_seconds",
                    "aruba_pn_events_received_total", "aruba_pn_unmapped_events_total",
                    "aruba_pn_dropped_events_total"})
