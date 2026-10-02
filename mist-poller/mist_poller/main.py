"""Entry point: python -m mist_poller.main [--once]"""
import json
import logging
import sys
import time

from . import config
from .keep import KeepClient
from .mist_api import MistClient, MistError
from .poller import Poller
from .state import State


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    log = logging.getLogger("mist_poller")

    cfg = config.load()
    if not (cfg.org_id and cfg.mist_token):
        sys.exit("MIST_ORG_ID and a Mist token (MIST_API_TOKEN_FILE) are required")
    if not cfg.dry_run and not cfg.keep_api_key:
        sys.exit("KEEP_API_KEY_FILE is required when DRY_RUN is false")

    poller = Poller(cfg,
                    MistClient(cfg.mist_host, cfg.org_id, cfg.mist_token, cfg.page_limit, cfg.max_pages),
                    KeepClient(cfg.keep_url, cfg.keep_api_key),
                    State(cfg.db_path))
    log.info("starting host=%s interval=%ds dry_run=%s suppress=%s",
             cfg.mist_host, cfg.poll_interval, cfg.dry_run, ",".join(sorted(cfg.suppress_types)))

    failures = 0
    while True:
        delay = cfg.poll_interval
        try:
            poller.run_cycle()
            failures = 0
        except MistError as e:
            failures += 1
            delay = e.retry_after or min(300, cfg.poll_interval * 2 ** min(failures, 3))
            log.error("Mist poll failed (%s); cursor not advanced; retry in %ds", e, delay)
            poller.drain()   # still flush anything queued for Keep
        except Exception:
            failures += 1
            delay = min(300, cfg.poll_interval * 2 ** min(failures, 3))
            log.exception("unexpected error; retry in %ds", delay)
        if "--once" in argv:
            return
        time.sleep(delay)


if __name__ == "__main__":
    main()
