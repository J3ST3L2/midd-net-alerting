"""Runtime configuration, read from environment variables.

Secrets are read from files (MIST_API_TOKEN_FILE, KEEP_API_KEY_FILE) so they
never appear in compose files, process args or logs.
"""
import os
from dataclasses import dataclass, field


def _read_secret(file_var, value_var):
    path = os.environ.get(file_var)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get(value_var, "").strip()


def _csv(name, default):
    raw = os.environ.get(name, default)
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def _bool(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    mist_host: str = "api.mist.com"
    org_id: str = ""
    mist_token: str = field(default="", repr=False)
    keep_url: str = "http://127.0.0.1:8088"
    keep_api_key: str = field(default="", repr=False)
    state_dir: str = "/state"

    poll_interval: int = 60
    overlap_s: int = 900              # re-fetch this far behind last success
    reconcile_interval: int = 900     # wide re-query so late updates are seen
    lookback_hours: int = 72          # width of the reconcile query
    bootstrap_hours: int = 72         # history scanned on a first/empty start
    bootstrap_post_open: bool = True  # post still-open alarms once at cold start
    page_limit: int = 100
    max_pages: int = 20

    dry_run: bool = True              # safe default: log, do not post to Keep
    suppress_types: frozenset = frozenset({
        "infra_arp_failure", "infra_arp_success", "infra_dhcp_failure", "infra_dhcp_success",
        "infra_dns_failure", "infra_dns_success"})
    auto_resolve_minutes: int = 30    # Mist events with no recovery signal

    health_max_age_s: int = 180
    health_unhealthy_s: int = 300

    @property
    def db_path(self):
        return os.path.join(self.state_dir, "mist-poller.sqlite3")


def load():
    host = os.environ.get("MIST_API_HOST", "api.mist.com").strip()
    org = os.environ.get("MIST_ORG_ID", "").strip()
    token = _read_secret("MIST_API_TOKEN_FILE", "MIST_API_TOKEN")
    cfg = Config(
        mist_host=host,
        org_id=org,
        mist_token=token,
        keep_url=os.environ.get("KEEP_URL", "http://127.0.0.1:8088").rstrip("/"),
        keep_api_key=_read_secret("KEEP_API_KEY_FILE", "KEEP_API_KEY"),
        state_dir=os.environ.get("STATE_DIR", "/state"),
        poll_interval=int(os.environ.get("POLL_INTERVAL_S", "60")),
        overlap_s=int(os.environ.get("OVERLAP_S", "900")),
        reconcile_interval=int(os.environ.get("RECONCILE_INTERVAL_S", "900")),
        lookback_hours=int(os.environ.get("LOOKBACK_HOURS", "72")),
        bootstrap_hours=int(os.environ.get("BOOTSTRAP_HOURS", "72")),
        bootstrap_post_open=_bool("BOOTSTRAP_POST_OPEN", "true"),
        page_limit=int(os.environ.get("PAGE_LIMIT", "100")),
        max_pages=int(os.environ.get("MAX_PAGES", "20")),
        dry_run=_bool("DRY_RUN", "true"),
        suppress_types=_csv("SUPPRESS_TYPES",
                         "infra_arp_failure,infra_arp_success,infra_dhcp_failure,"
                         "infra_dhcp_success,infra_dns_failure,infra_dns_success"),
        auto_resolve_minutes=int(os.environ.get("AUTO_RESOLVE_MINUTES", "30")),
        health_max_age_s=int(os.environ.get("HEALTH_MAX_AGE_S", "180")),
        health_unhealthy_s=int(os.environ.get("HEALTH_UNHEALTHY_S", "300")),
    )
    return cfg
