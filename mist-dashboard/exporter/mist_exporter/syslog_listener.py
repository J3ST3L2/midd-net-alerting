"""UDP and TCP syslog receiver for ClearPass authentication records.

Only sources in the allow-list are accepted; everything else is counted and dropped without being
parsed or logged. The listener refuses to start with an empty allow-list, so enabling it can never
accidentally open an unauthenticated feed. Record contents are never logged: the only thing written to
the log is the set of field NAMES seen, so an export template can be mapped without exposing a user.
"""
import ipaddress
import logging
import re
import socketserver
import threading

log = logging.getLogger("mist_exporter")

MAX_LINE = 16384
_OCTET_COUNT = re.compile(r"^\d+ (?=<)")        # RFC 6587 "123 <134>..." framing on TCP


def parse_allow(text):
    """Comma or space separated addresses / CIDR networks -> list of ip_network. Raises ValueError."""
    return [ipaddress.ip_network(part, strict=False) for part in re.split(r"[,\s]+", (text or "").strip()) if part]


class Listener:
    def __init__(self, host, port, tracker, allow, max_tcp=16, describe_every=600):
        if not allow:
            raise ValueError("refusing to start the ClearPass syslog listener without an allow-list")
        self.tracker, self.allow, self.describe_every = tracker, allow, describe_every
        self._tcp_slots = threading.BoundedSemaphore(max_tcp)
        self._stop = threading.Event()
        listener = self

        class UdpHandler(socketserver.BaseRequestHandler):
            def handle(self):
                listener._ingest(self.client_address[0], self.request[0].decode("utf-8", "replace"))

        class TcpHandler(socketserver.StreamRequestHandler):
            timeout = 300

            def handle(self):
                ip = self.client_address[0]
                if not listener.allowed(ip):             # before taking a slot: strangers cannot hold them
                    listener.tracker.count_dropped()
                    return
                if not listener._tcp_slots.acquire(blocking=False):
                    return
                try:
                    while not listener._stop.is_set():
                        raw = self.rfile.readline(MAX_LINE)
                        if not raw:
                            return
                        listener._ingest(ip, raw.decode("utf-8", "replace"))
                except OSError:
                    return
                finally:
                    listener._tcp_slots.release()

        socketserver.UDPServer.allow_reuse_address = True
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        socketserver.ThreadingTCPServer.daemon_threads = True
        self._udp = socketserver.UDPServer((host, port), UdpHandler)
        self._tcp = socketserver.ThreadingTCPServer((host, port or 0), TcpHandler)
        self.udp_port, self.tcp_port = self._udp.server_address[1], self._tcp.server_address[1]

    def allowed(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.allow)

    def _ingest(self, ip, text):
        if not self.allowed(ip):
            self.tracker.count_dropped()
            return
        for line in text.splitlines():
            line = _OCTET_COUNT.sub("", line.strip())
            if line:
                self.tracker.handle_line(line)

    def start(self):
        for server in (self._udp, self._tcp):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=self._describe_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        for server in (self._udp, self._tcp):
            server.shutdown()
            server.server_close()

    def _describe_loop(self):
        last = None
        while not self._stop.wait(self.describe_every):
            names = self.tracker.field_names()
            if names and names != last:
                log.info("clearpass fields seen (names only): %s", ", ".join(names))
                last = names
