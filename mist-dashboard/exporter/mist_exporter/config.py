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
    client_window: str = "10m"        # "currently connected" = seen in this window
    sites_interval: int = 900         # site id -> name map
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
        client_window=env("CLIENT_WINDOW", "10m").strip(),
        sites_interval=int(env("SITES_INTERVAL_S", "900")),
        alarm_window_hours=int(env("ALARM_WINDOW_HOURS", "24")),
        page_limit=int(env("PAGE_LIMIT", "100")),
        max_pages=int(env("MAX_PAGES", "20")),
        timeout=int(env("MIST_TIMEOUT_S", "30")),
    )
