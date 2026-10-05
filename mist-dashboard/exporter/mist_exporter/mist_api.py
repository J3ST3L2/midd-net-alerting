"""Read-only Mist REST client (GET only, token auth, same-host pagination)."""
import json
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
            "User-Agent": "midd-mist-exporter/1",
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

    def list_sites(self):
        """Site id -> site name."""
        body = self._get(self._url("/orgs/%s/sites" % self.org_id, {"limit": 1000}))
        if not isinstance(body, list):
            raise MistError("unexpected sites response shape")
        return {s["id"]: s.get("name", "") for s in body if isinstance(s, dict) and s.get("id")}

    def list_devices(self):
        """Raw org device stats rows (APs, switches and gateways)."""
        out, page_size = [], 1000
        for page in range(1, 51):
            body = self._get(self._url("/orgs/%s/stats/devices" % self.org_id,
                                       {"type": "all", "limit": page_size, "page": page}))
            if not isinstance(body, list):
                raise MistError("unexpected device stats response shape")
            out.extend(d for d in body if isinstance(d, dict))
            if len(body) < page_size:
                return out
        raise MistError("device stats exceeded 50 pages; refusing partial result")

    def list_site_stats(self):
        """Raw org site stats rows (per-site client and device counts)."""
        out, page_size = [], 1000
        for page in range(1, 11):
            body = self._get(self._url("/orgs/%s/stats/sites" % self.org_id,
                                       {"limit": page_size, "page": page}))
            if not isinstance(body, list):
                raise MistError("unexpected site stats response shape")
            out.extend(r for r in body if isinstance(r, dict))
            if len(body) < page_size:
                return out
        raise MistError("site stats exceeded 10 pages; refusing partial result")

    def client_counts(self, distinct, duration, site_id=None):
        """Wireless clients seen in the last `duration`, grouped by `distinct` (ap, band, ssid), for
        the whole org or one site. Mist caps the result list at `limit`, so a long group list (per-AP)
        may omit its smallest entries."""
        scope = "/sites/%s" % site_id if site_id else "/orgs/%s" % self.org_id
        body = self._get(self._url(scope + "/clients/count",
                                   {"distinct": distinct, "duration": duration, "limit": 1000}))
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise MistError("unexpected client count response shape")
        return [r for r in body["results"] if isinstance(r, dict)]

    def sle_summary(self, site_id, metric, duration):
        """Wifi SLE summary for one site and metric (coverage, capacity, ...): who was affected."""
        body = self._get(self._url("/sites/%s/sle/site/%s/metric/%s/summary" % (site_id, site_id, metric),
                                   {"duration": duration}))
        if not isinstance(body, dict):
            raise MistError("unexpected SLE summary response shape")
        return body

    def sle_impacted_aps(self, site_id, metric, duration):
        """APs behind a degraded SLE metric: rows of {ap_mac, name, degraded, total, duration}."""
        body = self._get(self._url("/sites/%s/sle/site/%s/metric/%s/impacted-aps" % (site_id, site_id, metric),
                                   {"duration": duration, "limit": 1000}))
        if not isinstance(body, dict) or not isinstance(body.get("aps"), list):
            raise MistError("unexpected impacted-aps response shape")
        return [a for a in body["aps"] if isinstance(a, dict)]

    def search_alarms(self, start, end):
        """All alarms in [start, end]. Raises if any page fails or pages are cut off."""
        url = self._url("/orgs/%s/alarms/search" % self.org_id,
                        {"start": int(start), "end": int(end), "limit": self.limit})
        alarms = []
        for _ in range(self.max_pages):
            body = self._get(url)
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise MistError("unexpected alarms response shape")
            alarms.extend(a for a in body["results"] if isinstance(a, dict))
            nxt = body.get("next")
            if not nxt or not body["results"]:
                return alarms
            url = nxt if nxt.startswith("http") else "https://%s%s" % (self.host, nxt)
        raise MistError("alarm search exceeded MAX_PAGES; refusing partial result")
