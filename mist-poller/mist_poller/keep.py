"""Delivery to the local Keep API. The poller never talks to Slack."""
import json
import urllib.error
import urllib.request


class KeepError(Exception):
    """permanent=True means the payload itself is bad (4xx other than auth/429)."""

    def __init__(self, message, status=None, permanent=False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


class KeepClient:
    def __init__(self, url, api_key, timeout=15):
        self.endpoint = url.rstrip("/") + "/alerts/event"
        self._key = api_key
        self.timeout = timeout

    def post(self, payload):
        req = urllib.request.Request(
            self.endpoint, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-API-KEY": self._key,
                     "X-Service-Name": "mist-poller"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
        except urllib.error.HTTPError as e:
            permanent = 400 <= e.code < 500 and e.code not in (401, 403, 408, 429)
            raise KeepError("Keep HTTP %d" % e.code, status=e.code, permanent=permanent)
        except (urllib.error.URLError, TimeoutError) as e:
            raise KeepError("Keep unreachable: %s" % type(e).__name__)
