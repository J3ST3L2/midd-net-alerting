"""Read-only Mist REST client (GET only, token auth, same-host pagination)."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request


class MistError(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class MistClient:
    def __init__(self, host, org_id, token, limit=100, max_pages=20, timeout=30):
        self.host = host
        self.org_id = org_id
        self._token = token
        self.limit = limit
        self.max_pages = max_pages
        self.timeout = timeout

    def _url(self, path, params=None):
        q = "?" + urllib.parse.urlencode(params) if params else ""
        return "https://%s/api/v1%s%s" % (self.host, path, q)

    def _get(self, url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc.lower() != self.host.lower():
            raise MistError("refusing to send token to unexpected host")
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": "Token " + self._token,
            "Accept": "application/json",
            "User-Agent": "midd-mist-poller/1",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            raise MistError("Mist HTTP %d" % e.code, status=e.code,
                            retry_after=int(ra) if ra and ra.isdigit() else None)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise MistError("Mist request failed: %s" % type(e).__name__)

    def search_alarms(self, start, end):
        """All alarms in [start, end]. Raises if any page fails or pages are cut off,
        so the caller never advances its cursor on a partial result."""
        url = self._url("/orgs/%s/alarms/search" % self.org_id,
                        {"start": int(start), "end": int(end), "limit": self.limit})
        alarms = []
        for _ in range(self.max_pages):
            body = self._get(url)
            if not isinstance(body, dict):
                raise MistError("unexpected alarms response shape")
            results = body.get("results")
            if not isinstance(results, list):
                raise MistError("alarms response has no results list")
            alarms.extend(results)
            nxt = body.get("next")
            if not nxt or not results:
                return alarms
            url = nxt if nxt.startswith("http") else "https://%s%s" % (self.host, nxt)
        raise MistError("alarm search exceeded MAX_PAGES; refusing partial result")

    def list_sites(self):
        body = self._get(self._url("/orgs/%s/sites" % self.org_id, {"limit": 1000}))
        if not isinstance(body, list):
            raise MistError("unexpected sites response shape")
        return {s["id"]: s.get("name", "") for s in body if isinstance(s, dict) and s.get("id")}

    def list_devices(self):
        """MAC -> {ip, name, status, model} from org device stats (alarms carry no IPs)."""
        out, page_size = {}, 1000
        for page in range(1, 51):
            body = self._get(self._url("/orgs/%s/stats/devices" % self.org_id,
                                       {"type": "all", "limit": page_size, "page": page}))
            if not isinstance(body, list):
                raise MistError("unexpected device stats response shape")
            for d in body:
                if isinstance(d, dict) and d.get("mac"):
                    out[re.sub(r"[^0-9a-f]", "", str(d["mac"]).lower())] = {
                        "ip": d.get("ip") or "", "name": d.get("name") or "",
                        "status": d.get("status") or "", "model": d.get("model") or "",
                        "version": d.get("version") or "",
                        "last_seen": d.get("last_seen") if isinstance(d.get("last_seen"), int) else 0}
            if len(body) < page_size:
                return out
        raise MistError("device stats exceeded 50 pages")

    def search_device_events(self, mac, start, end, types=None, limit=20):
        """Device-level events for a MAC in [start, end]. Alarms carry no reason text for
        device_down/reconnected; the device events API does. Returns [] on any failure."""
        params = {"mac": mac, "start": int(start), "end": int(end), "limit": limit}
        if types:
            params["type"] = ",".join(types)
        try:
            body = self._get(self._url("/orgs/%s/devices/events/search" % self.org_id, params))
        except MistError:
            return []
        if not isinstance(body, dict):
            return []
        results = body.get("results")
        return results if isinstance(results, list) else []
