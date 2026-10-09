"""Entry point: python -m mist_exporter.main [--healthcheck]"""
import json
import logging
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .collector import Collector
from .mist_api import MistClient


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/metrics":
                self._send(200, collector.render(), "text/plain; version=0.0.4; charset=utf-8")
            elif self.path == "/healthz":
                ok = collector.healthy()
                self._send(200 if ok else 503, "ok\n" if ok else "stale\n")
            else:
                self._send(404, "not found\n")

        def log_message(self, *args):   # scrapes every 30s would drown the log
            pass
    return Handler


def healthcheck(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cfg = config.load()
    if "--healthcheck" in argv:
        sys.exit(0 if healthcheck(cfg.listen_port) else 1)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    log = logging.getLogger("mist_exporter")

    if not (cfg.org_id and cfg.mist_token):
        sys.exit("MIST_ORG_ID and a Mist token (MIST_API_TOKEN_FILE) are required")

    collector = Collector(cfg, MistClient(cfg.mist_host, cfg.org_id, cfg.mist_token,
                                          cfg.page_limit, cfg.max_pages, cfg.timeout))
    stop = threading.Event()
    threading.Thread(target=collector.run_forever, args=(stop,), daemon=True).start()

    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), make_handler(collector))
    log.info("starting host=%s listen=%s:%d devices=%ds alarms=%ds",
             cfg.mist_host, cfg.listen_host, cfg.listen_port, cfg.devices_interval, cfg.alarms_interval)
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
