"""A real HTTP model substitute with observable execution and fault boundaries."""
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

from .protocol import UNIT_CONTRACT, encode_event, end_chunks, send_chunk


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def handle_error(self, request, client_address):
        # Never print request bodies, authorization headers or exception text.
        pass


@dataclass(frozen=True)
class Plan:
    mode: str = "normal"
    chunks: tuple = ("hello", " gateway")
    delay: float = 0.01
    first_delay: float = 0.0
    raw_chunks: tuple = ()
    release_after_first_chunk: object = None


class ModelStub:
    """Loopback model service, not a simulated policy function.

    X-Lab-Execution: not-started is an explicit private protocol promise: for
    this one response the service did not start execution or accrue usage.
    The unknown_503 plan makes the opposite counterexample observable.
    """

    def __init__(self, plans=(), credential="fixture-upstream-secret"):
        self.plans = deque(plans)
        self.credential = credential
        self.lock = threading.Lock()
        self.requests = 0
        self.operations_started = 0
        self.events_written = 0
        self.disconnects = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.close_connection = True
                if self.path != "/generate":
                    self.reply(404, {"error": "not_found"})
                    return
                if self.headers.get("Authorization") != "Bearer " + owner.credential:
                    self.reply(401, {"error": "unauthorized"})
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if not 0 < n <= 65536:
                        raise ValueError
                    request = json.loads(self.rfile.read(n))
                    prompt = request["prompt"]
                    maximum = request["max_output_units"]
                    if not isinstance(prompt, str) or type(maximum) is not int or maximum < 1:
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    self.reply(400, {"error": "invalid_request"})
                    return
                with owner.lock:
                    owner.requests += 1
                    plan = owner.plans.popleft() if owner.plans else Plan()
                    if plan.mode != "safe_503":
                        owner.operations_started += 1
                if plan.mode in {"safe_503", "unknown_503"}:
                    self.reply(503, {"error": "unavailable"},
                               {"X-Lab-Execution": "not-started"} if plan.mode == "safe_503" else {})
                    return
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                self.connection.settimeout(3)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if plan.first_delay:
                        time.sleep(plan.first_delay)
                    if plan.raw_chunks:
                        for index, raw in enumerate(plan.raw_chunks):
                            send_chunk(self, raw)
                            if index == 0 and plan.release_after_first_chunk is not None:
                                plan.release_after_first_chunk.wait(3)
                        end_chunks(self)
                        return
                    if plan.mode == "pre_event_disconnect":
                        return  # No terminal chunk and no not-started promise.
                    produced = 0
                    for i, text in enumerate(plan.chunks):
                        text = text.encode("utf-8")[:maximum - produced].decode("utf-8", errors="ignore")
                        if not text:
                            break
                        if plan.delay:
                            time.sleep(plan.delay)
                        send_chunk(self, encode_event("token", {"text": text}))
                        produced += len(text.encode("utf-8"))
                        with owner.lock:
                            owner.events_written += 1
                        if plan.mode == "midstream_disconnect" and i == 0:
                            return
                    if plan.mode != "omit_usage":
                        usage = {"contract": UNIT_CONTRACT,
                                 "input_units": len(prompt.encode("utf-8")),
                                 "output_units": produced}
                        if plan.mode == "invalid_usage":
                            usage["output_units"] += 1
                        send_chunk(self, encode_event("usage", usage))
                    send_chunk(self, encode_event("done", {}))
                    end_chunks(self)
                except (OSError, TimeoutError):
                    with owner.lock:
                        owner.disconnects += 1

            def reply(self, status, payload, extra=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
