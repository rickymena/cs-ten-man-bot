"""
In-process fake Discord webhook server with deterministic fault injection.

It binds 127.0.0.1 on an ephemeral port and runs in a daemon thread, so the
real webhook code talks to it over real sockets: the retry, timeout and
connection-drop paths are exercised exactly as in production.

Faults are chosen per request by a ``FaultPlan``: either a fixed queue of
faults (deterministic scenarios) or a seeded random mix (chaos runs). Delays
use ``Event.wait`` rather than ``time.sleep`` because the suite replaces
``time.sleep`` in the client process to skip the bot's own back-off waits.
"""

import json
import random
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer



# ── fault plans ──────────────────────────────────────────────────────────────

# Fault kinds:
#   None            respond normally
#   "500"           HTTP 500
#   "404"           HTTP 404
#   "429:<secs>"    HTTP 429 with Retry-After: <secs>
#   "drop"          read the request, then close the socket without replying
#   "slow:<secs>"   wait, then respond normally
#   "hang:<secs>"   accept the post (it reaches the channel), reply late


class FaultPlan:
    """Picks the fault for each request. Thread-safe."""

    def __init__(self, queue=None, mix=None, seed=0):
        self._queue = deque(queue or [])
        self._mix = list(mix or [])  # [(kind, probability), ...]
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            if self._queue:
                return self._queue.popleft()
            roll = self._rng.random()
            acc = 0.0
            for kind, p in self._mix:
                acc += p
                if roll < acc:
                    return kind
            return None

    def set(self, queue=None, mix=None):
        with self._lock:
            self._queue = deque(queue or [])
            self._mix = list(mix or [])


def _split(kind):
    if kind and ":" in kind:
        name, arg = kind.split(":", 1)
        return name, float(arg)
    return kind, None


class _Server:
    def __init__(self, handler_cls):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.httpd.daemon_threads = True
        self.httpd.owner = self
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Headers and body go out as separate writes; without TCP_NODELAY every
    # keep-alive request stalls ~40 ms on Nagle + delayed ACK, which would
    # dominate the throughput numbers.
    disable_nagle_algorithm = True

    def log_message(self, *args):  # keep test output quiet
        pass

    @property
    def owner(self):
        return self.server.owner

    def _send(self, status, body=b"", headers=None, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _drop(self):
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass

    def _common_fault(self, name, arg):
        """Handle faults shared by both servers. Returns True if it replied."""
        if name == "500":
            self._send(500, b'{"error":"sim"}')
        elif name == "404":
            self._send(404, b'{"message":"Unknown Webhook","code":10015}')
        elif name == "429":
            self._send(429, b'{"message":"rate limited"}', {"Retry-After": f"{arg:g}"})
        elif name == "drop":
            self._drop()
        else:
            return False
        return True


# ── fake Discord ─────────────────────────────────────────────────────────────


@dataclass
class Post:
    content: str
    payload: dict
    status: int
    reached_channel: bool   # Discord would have shown it (2xx, hang, drop-after-read)
    fault: str | None

    @property
    def embeds(self):
        return self.payload.get("embeds") or []


@dataclass
class _DiscordState:
    posts: list = field(default_factory=list)
    pings: list = field(default_factory=list)


class FakeDiscord(_Server):
    """Webhook sink at /api/webhooks/1/sim plus a healthcheck ping endpoint at /ping."""

    def __init__(self):
        super().__init__(_DiscordHandler)
        self.lock = threading.Lock()
        self.state = _DiscordState()
        self.faults = FaultPlan()

    @property
    def webhook_url(self):
        return self.base + "/api/webhooks/1/sim"

    @property
    def ping_url(self):
        return self.base + "/ping"

    @property
    def posts(self):
        with self.lock:
            return list(self.state.posts)

    @property
    def pings(self):
        with self.lock:
            return list(self.state.pings)

    def channel(self):
        """Messages a Discord user would actually see."""
        return [p for p in self.posts if p.reached_channel]

    def reset(self):
        with self.lock:
            self.state = _DiscordState()


class _DiscordHandler(_Handler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        sink = self.owner
        if self.path.startswith("/ping"):
            with sink.lock:
                sink.state.pings.append(("fail" if self.path.endswith("/fail") else "ok", raw))
            return self._send(200, b"OK", content_type="text/plain")
        if not self.path.startswith("/api/webhooks/"):
            return self._send(404, b"{}")

        payload = json.loads(raw or b"{}")
        content = payload.get("content", "")
        kind = sink.faults.next()
        name, arg = _split(kind)

        def record(status, reached):
            with sink.lock:
                sink.state.posts.append(Post(content, payload, status, reached, kind))

        if name == "slow":
            sink.stop_event.wait(arg)
            name = None
        if name == "hang":
            record(204, True)          # Discord posted it; the reply is what is late
            sink.stop_event.wait(arg)
            return self._drop()
        if name == "drop":
            record(0, True)            # processed, then the connection died
            return self._drop()
        if name in ("500", "404", "429"):
            record({"500": 500, "404": 404, "429": 429}[name], False)
            self._common_fault(name, arg)
            return
        if len(content) > 2000:
            record(400, False)
            return self._send(400, b'{"message":"Invalid Form Body","code":50035}')
        record(204, True)
        self._send(204)
