"""Runtime configuration, read from environment variables.

The Mist token is read from a file (MIST_API_TOKEN_FILE) so it never appears in
compose files, process args or logs.
"""
import os
from dataclasses import dataclass, field


def _read_secret(file_var, value_var):
    path = os.environ.get(file_var)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return os.environ.get(value_var, "").strip()


def _csv(text):
    return tuple(p.strip() for p in text.split(",") if p.strip())


@dataclass(frozen=True)
class Config:
    mist_host: str = "api.mist.com"
    org_id: str = ""
    mist_token: str = field(default="", repr=False)

    listen_host: str = "0.0.0.0"
    listen_port: int = 9877

    devices_interval: int = 60        # org device stats: fleet health, clients, load
    alarms_interval: int = 120        # org alarms
    clients_interval: int = 60        # wireless client counts (site stats + clients/count)
    client_window: str = "30m"        # clients counted = seen in this window (closest to Mist site stats)
    sle_interval: int = 300           # wifi SLE scores (coverage, capacity, ...)
    sle_window: str = "1d"            # SLE look-back, Mist's own default
    sites_interval: int = 900         # site id -> name map
    # PantherNet -> MiddleburyCollege fallback (see pn_fallback.py)
    fallback_enabled: bool = True
    fallback_interval: int = 300      # once the history is loaded; every 60 s while it loads
    fallback_window_s: int = 1800     # a failure counts as a fallback if MC follows within this
    fallback_lookback_h: int = 24
    fallback_pages_per_slice: int = 16      # event pages per slice, split between the two feeds
    fallback_lookups_per_slice: int = 24    # username / connection lookups per slice
    fallback_totals_interval: int = 300     # unique-client totals per SSID
    # Cache of the expensive username lookups, as keyed hashes only, so a restart does not reload for hours.
    # Needs a secret key of 32+ characters in fallback_cache_key_file; without one it stays in memory.
    fallback_cache_path: str = "/state/pn-cache.sqlite3"
    fallback_cache_key_file: str = ""
    fallback_cache_ttl_h: int = 72
    pn_ssid: str = "PantherNet"
    mc_ssid: str = "MiddleburyCollege"
    mc_event_type: str = "CLIENT_AUTH_ASSOCIATION"
    # Aruba controllers, read-only (see aruba.py). Off unless controllers and a password are configured.
    aruba_controllers: tuple = ()
    aruba_user: str = "grafana-ro"
    aruba_password: str = field(default="", repr=False)
    aruba_interval: int = 120
    aruba_ca_file: str = ""           # verify the controllers' certificates against this CA; empty = self-signed ok
    aruba_timeout: int = 60
    # Which VLANs on the PantherNet SSID carry BYOD and managed devices (Mist buildings). Kept in .env, not in Git.
    pn_byod_vlans: tuple = ()
    pn_managed_vlans: tuple = ()
    alarm_window_hours: int = 24
    page_limit: int = 100
    max_pages: int = 20
    timeout: int = 30


def load():
    env = os.environ.get
    return Config(
        mist_host=env("MIST_API_HOST", "api.mist.com").strip(),
        org_id=env("MIST_ORG_ID", "").strip(),
        mist_token=_read_secret("MIST_API_TOKEN_FILE", "MIST_API_TOKEN"),
        listen_host=env("LISTEN_HOST", "0.0.0.0").strip(),
        listen_port=int(env("LISTEN_PORT", "9877")),
        devices_interval=int(env("DEVICES_INTERVAL_S", "60")),
        alarms_interval=int(env("ALARMS_INTERVAL_S", "120")),
        clients_interval=int(env("CLIENTS_INTERVAL_S", "60")),
        client_window=env("CLIENT_WINDOW", "30m").strip(),
        sle_interval=int(env("SLE_INTERVAL_S", "300")),
        sle_window=env("SLE_WINDOW", "1d").strip(),
        sites_interval=int(env("SITES_INTERVAL_S", "900")),
        fallback_enabled=env("FALLBACK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on"),
        fallback_interval=int(env("FALLBACK_INTERVAL_S", "300")),
        fallback_window_s=int(env("FALLBACK_WINDOW_S", "1800")),
        fallback_lookback_h=int(env("FALLBACK_LOOKBACK_H", "24")),
        fallback_pages_per_slice=int(env("FALLBACK_PAGES_PER_SLICE", "16")),
        fallback_lookups_per_slice=int(env("FALLBACK_LOOKUPS_PER_SLICE", "24")),
        fallback_totals_interval=int(env("FALLBACK_TOTALS_INTERVAL_S", "300")),
        fallback_cache_path=env("FALLBACK_CACHE_PATH", "/state/pn-cache.sqlite3").strip(),
        fallback_cache_key_file=env("PN_CACHE_KEY_FILE", "").strip(),
        fallback_cache_ttl_h=int(env("FALLBACK_CACHE_TTL_H", "72")),
        pn_ssid=env("PN_SSID", "PantherNet").strip(),
        mc_ssid=env("MC_SSID", "MiddleburyCollege").strip(),
        mc_event_type=env("MC_EVENT_TYPE", "CLIENT_AUTH_ASSOCIATION").strip(),
        aruba_controllers=tuple(h.strip() for h in env("ARUBA_CONTROLLERS", "").split(",") if h.strip()),
        aruba_user=env("ARUBA_USER", "grafana-ro").strip(),
        aruba_password=_read_secret("ARUBA_PASSWORD_FILE", "ARUBA_PASSWORD"),
        aruba_interval=int(env("ARUBA_INTERVAL_S", "120")),
        aruba_ca_file=env("ARUBA_CA_FILE", "").strip(),
        aruba_timeout=int(env("ARUBA_TIMEOUT_S", "60")),
        pn_byod_vlans=_csv(env("PN_BYOD_VLANS", "")),
        pn_managed_vlans=_csv(env("PN_MANAGED_VLANS", "")),
        alarm_window_hours=int(env("ALARM_WINDOW_HOURS", "24")),
        page_limit=int(env("PAGE_LIMIT", "100")),
        max_pages=int(env("MAX_PAGES", "20")),
        timeout=int(env("MIST_TIMEOUT_S", "30")),
    )
