"""Small inspectable HTTP gateway with conservative replay and accounting rules.

Teaching constraints: loopback HTTP only, one process/in-memory state, one credit
per UTF-8 byte, no real model SDKs. No claim of durable accounting, authentication
for an untrusted network, precise model cancellation or production isolation.
"""
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
import http.client
import json
import queue
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from .protocol import UNIT_CONTRACT, ProtocolError, encode_event, end_chunks, read_events, send_chunk
from .upstream import QuietServer


@dataclass(frozen=True)
class Route:
    name: str
    url: str
    capabilities: frozenset = frozenset({"text"})
    credential: str = "fixture-upstream-secret"
    trust_not_started: bool = False

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise ValueError("The lab accepts loopback HTTP upstreams only")


@dataclass(frozen=True)
class TenantPolicy:
    budget_units: int = 10000
    max_concurrent: int = 2


@dataclass(frozen=True)
class Settings:
    queue_events: int = 2
    max_attempts: int = 2
    upstream_timeout: float = 1.0
    client_write_timeout: float = 0.3
    max_output_units: int = 8 * 1024 * 1024
    max_input_bytes: int = 32768


class AdmissionError(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status


class StreamFailure(Exception):
    pass


class Cancelled(StreamFailure):
    pass


class OpenFailure(Exception):
    def __init__(self, code, safe=False):
        self.code, self.safe = code, safe


class Audit:
    ALLOWED = {"event", "request_id", "tenant", "route", "attempt", "reason",
               "outcome", "usage_state", "reservation_units", "actual_units"}

    def __init__(self):
        self.records, self.lock = [], threading.Lock()

    def emit(self, event, **fields):
        # Field allowlist excludes prompt, generated text, request headers and
        # arbitrary exception strings; do not log arbitrary upstream objects.
        record = {"event": event, **fields}
        if not set(record) <= self.ALLOWED:
            raise ValueError("Unapproved audit field")
        with self.lock:
            self.records.append({"time": round(time.time(), 3), **record})

    def jsonl(self):
        with self.lock:
            return "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in self.records)


class Ledger:
    def __init__(self, policies):
        self.policies = policies
        self.runs, self.lock = {}, threading.Lock()

    def _totals(self, tenant):
        records = [r for (owner, _), r in self.runs.items() if owner == tenant]
        held = sum(r["reservation_units"] for r in records if r["usage_state"] in {"held", "unknown"})
        spent = sum(r["actual_units"] or 0 for r in records if r["usage_state"] == "settled")
        return records, held, spent, sum(r["active"] for r in records)

    def reserve(self, tenant, request_id, units):
        with self.lock:
            if (tenant, request_id) in self.runs:
                raise AdmissionError("duplicate_request_id", 409)
            _, held, spent, active = self._totals(tenant)
            policy = self.policies[tenant]
            if active >= policy.max_concurrent:
                raise AdmissionError("concurrency_limit", 429)
            if units > policy.budget_units - held - spent:
                raise AdmissionError("budget_exhausted", 402)
            self.runs[tenant, request_id] = {
                "reservation_units": units, "actual_units": None,
                "usage_state": "held", "outcome": "admitted", "active": True,
            }

    def finish(self, tenant, request_id, outcome, actual_units=None, known_unstarted=False):
        with self.lock:
            record = self.runs[tenant, request_id]
            if not record["active"]:
                return dict(record)
            if actual_units is not None and not 0 <= actual_units <= record["reservation_units"]:
                raise ValueError("usage_outside_reservation")
            record.update(active=False, outcome=outcome, actual_units=actual_units)
            record["usage_state"] = ("settled" if actual_units is not None else
                                     "released" if known_unstarted else "unknown")
            return dict(record)

    def reconcile(self, tenant, request_id, actual_units):
        """Operator-only in-process action after authoritative billing evidence.

        There is deliberately no client HTTP endpoint that can forgive debt.
        Zero is valid only if an operator has evidence that nothing was billed.
        """
        with self.lock:
            record = self.runs[tenant, request_id]
            if record["usage_state"] != "unknown":
                raise ValueError("only_unknown_usage_can_be_reconciled")
            if type(actual_units) is not int or not 0 <= actual_units <= record["reservation_units"]:
                raise ValueError("invalid_authoritative_usage")
            record.update(usage_state="settled", actual_units=actual_units)

    def record(self, tenant, request_id):
        with self.lock:
            record = self.runs.get((tenant, request_id))
            return dict(record) if record else None

    def snapshot(self, tenant):
        with self.lock:
            _, held, spent, active = self._totals(tenant)
            return {"budget_units": self.policies[tenant].budget_units,
                    "spent_units": spent, "held_units": held,
                    "available_units": self.policies[tenant].budget_units - held - spent,
                    "active": active}


class Running:
    def __init__(self, tenant, request_id):
        self.tenant, self.request_id = tenant, request_id
        self.cancelled = threading.Event()
        self.socket = None
        self.lock = threading.Lock()
        self.processing_possible = False
        self.metrics = {"queue_peak": 0, "backpressure_waits": 0, "producer_stopped": True}

    def bind_socket(self, sock):
        with self.lock:
            self.socket = sock
            if self.cancelled.is_set():
                self._shutdown(sock)

    @staticmethod
    def _shutdown(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close_upstream(self):
        with self.lock:
            self._shutdown(self.socket)

    def cancel(self):
        self.cancelled.set()
        self.close_upstream()

    def metric(self, key, value=None):
        with self.lock:
            if value is None:
                self.metrics[key] += 1
            else:
                self.metrics[key] = value

    def stats(self):
        with self.lock:
            return dict(self.metrics)


class Gateway:
    def __init__(self, routes, policies=None, credentials=None, settings=None, port=0):
        self.routes = routes
        self.settings = settings or Settings()
        if self.settings.queue_events < 1 or self.settings.max_attempts < 1:
            raise ValueError("queue_events and max_attempts must be positive")
        self.ledger = Ledger(policies or {"tenant-a": TenantPolicy()})
        self.credentials = credentials or {"tenant-a-secret": "tenant-a"}
        self.audit, self.running, self.lock = Audit(), {}, threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def authenticate(self):
                value = self.headers.get("Authorization", "")
                return owner.credentials.get(value[7:]) if value.startswith("Bearer ") else None

            def reply(self, status, payload):
                self.close_connection = True
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_DELETE(self):
                tenant = self.authenticate()
                if tenant is None:
                    self.reply(401, {"error": "unauthorized"})
                    return
                prefix = "/v1/runs/"
                request_id = self.path[len(prefix):] if self.path.startswith(prefix) else ""
                with owner.lock:
                    run = owner.running.get((tenant, request_id))
                if run is None:
                    self.reply(404, {"error": "not_found"})
                    return
                if not owner.ledger.record(tenant, request_id)["active"]:
                    self.reply(409, {"error": "already_finished"})
                    return
                run.cancel()
                owner.audit.emit("cancel_requested", tenant=tenant, request_id=request_id)
                self.reply(202, {"request_id": request_id, "state": "cancellation_requested"})

            def do_POST(self):
                self.close_connection = True
                self.connection.settimeout(owner.settings.client_write_timeout)
                if self.path != "/v1/generate":
                    self.reply(404, {"error": "not_found"})
                    return
                tenant = self.authenticate()
                if tenant is None:
                    self.reply(401, {"error": "unauthorized"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        raise ValueError
                    request = json.loads(self.rfile.read(size))
                    owner.validate(request)
                except (ValueError, TypeError, KeyError, OSError):
                    self.reply(400, {"error": "invalid_request"})
                    return
                candidates = [route for route in owner.routes
                              if set(request["capabilities"]) <= route.capabilities]
                if not candidates:
                    self.reply(422, {"error": "unsupported_capability"})
                    return
                request_id = request["request_id"]
                reservation = len(request["prompt"].encode("utf-8")) + request["max_output_units"]
                try:
                    owner.ledger.reserve(tenant, request_id, reservation)
                except AdmissionError as exc:
                    self.reply(exc.status, {"error": exc.code})
                    return
                run = Running(tenant, request_id)
                with owner.lock:
                    owner.running[tenant, request_id] = run
                owner.audit.emit("admitted", tenant=tenant, request_id=request_id, reservation_units=reservation)
                owner.serve(self, run, request, candidates)

        self.server = QuietServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def validate(self, request):
        allowed = {"request_id", "prompt", "capabilities", "max_output_units", "allow_failover"}
        if not isinstance(request, dict) or not set(request) <= allowed:
            raise ValueError
        if not isinstance(request["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request["request_id"]):
            raise ValueError
        if not isinstance(request["prompt"], str) or len(request["prompt"].encode("utf-8")) > self.settings.max_input_bytes:
            raise ValueError
        if type(request["max_output_units"]) is not int or not 1 <= request["max_output_units"] <= self.settings.max_output_units:
            raise ValueError
        capabilities = request["capabilities"]
        if not isinstance(capabilities, list) or not capabilities or len(capabilities) > 16:
            raise ValueError
        if any(not isinstance(c, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", c) for c in capabilities):
            raise ValueError
        if "allow_failover" in request and type(request["allow_failover"]) is not bool:
            raise ValueError

    def _open(self, run, route, request):
        parsed = urlsplit(route.url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port,
                                                timeout=self.settings.upstream_timeout)
        try:
            connection.connect()
        except OSError as exc:
            connection.close()
            raise OpenFailure("connect_failed") from exc
        run.bind_socket(connection.sock)
        if run.cancelled.is_set():
            connection.close()
            raise Cancelled("cancelled")
        # Once sending begins, a transport failure is ambiguous. The code does
        # not infer safe replay from zero model tokens or an empty response.
        run.processing_possible = True
        try:
            connection.request("POST", "/generate", body=json.dumps({
                "prompt": request["prompt"], "max_output_units": request["max_output_units"],
                "capabilities": request["capabilities"],
            }).encode(), headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer " + route.credential})
            response = connection.getresponse()
            safe = (response.status == 503 and route.trust_not_started and
                    response.getheader("X-Lab-Execution") == "not-started")
            if response.status != 200:
                if safe:
                    run.processing_possible = False
                run.close_upstream()
                connection.close()
                raise OpenFailure("upstream_not_started" if safe else "upstream_status_unknown", safe)
            if not response.getheader("Content-Type", "").startswith("text/event-stream"):
                run.close_upstream()
                connection.close()
                raise OpenFailure("upstream_protocol_unknown")
            return connection, response
        except (OSError, http.client.HTTPException) as exc:
            run.close_upstream()
            connection.close()
            raise OpenFailure("upstream_transport_unknown") from exc

    def serve(self, handler, run, request, candidates):
        connection = response = producer = None
        stop = threading.Event()
        emitted = False
        outcome, reason, actual = "failed", "unavailable", None
        try:
            limit = min(len(candidates), self.settings.max_attempts) if request.get("allow_failover", False) else 1
            for attempt, route in enumerate(candidates[:limit], 1):
                self.audit.emit("route_attempt", tenant=run.tenant, request_id=run.request_id,
                                route=route.name, attempt=attempt)
                try:
                    connection, response = self._open(run, route, request)
                    break
                except OpenFailure as exc:
                    reason = exc.code
                    self.audit.emit("route_failed", tenant=run.tenant, request_id=run.request_id,
                                    route=route.name, attempt=attempt, reason=reason)
                    if not exc.safe or attempt == limit:
                        raise StreamFailure(reason)
            events = queue.Queue(maxsize=self.settings.queue_events)

            def put(item):
                while not stop.is_set() and not run.cancelled.is_set():
                    try:
                        events.put(item, timeout=0.02)
                        run.metric("queue_peak", max(run.stats()["queue_peak"], events.qsize()))
                        return
                    except queue.Full:
                        run.metric("backpressure_waits")

            def produce():
                run.metric("producer_stopped", False)
                try:
                    for event in read_events(response):
                        put(event)
                        if stop.is_set() or run.cancelled.is_set():
                            return
                    put(("_eof", {}))
                except (OSError, ValueError, ProtocolError, http.client.HTTPException):
                    put(("_broken", {}))
                finally:
                    run.metric("producer_stopped", True)

            producer = threading.Thread(target=produce, daemon=True)
            producer.start()
            produced, usage = 0, None
            while True:
                if run.cancelled.is_set():
                    raise Cancelled("cancelled")
                try:
                    event, data = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if event == "token":
                    if usage is not None or set(data) != {"text"} or not isinstance(data["text"], str):
                        raise StreamFailure("invalid_token")
                    produced += len(data["text"].encode("utf-8"))
                    if produced > request["max_output_units"]:
                        raise StreamFailure("output_exceeds_reservation")
                elif event == "usage":
                    if usage is not None or set(data) != {"contract", "input_units", "output_units"}:
                        raise StreamFailure("invalid_usage")
                    if (data["contract"] != UNIT_CONTRACT or type(data["input_units"]) is not int or
                            type(data["output_units"]) is not int or
                            data["input_units"] != len(request["prompt"].encode("utf-8")) or
                            data["output_units"] != produced):
                        raise StreamFailure("untrusted_usage")
                    usage = data
                elif event == "done":
                    if usage is None or data:
                        raise StreamFailure("missing_final_usage")
                else:
                    raise StreamFailure("upstream_stream_interrupted")
                if not emitted:
                    handler.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                    handler.send_response(200)
                    handler.send_header("Content-Type", "text/event-stream")
                    handler.send_header("Transfer-Encoding", "chunked")
                    handler.send_header("Cache-Control", "no-store")
                    handler.send_header("Connection", "close")
                    handler.send_header("X-Request-ID", run.request_id)
                    handler.end_headers()
                    emitted = True
                send_chunk(handler, encode_event(event, data))
                if event == "done":
                    end_chunks(handler)
                    actual = usage["input_units"] + usage["output_units"]
                    outcome, reason = "completed", "final_usage_received"
                    break
        except Cancelled:
            outcome, reason = "cancelled", "cancelled"
        except StreamFailure as exc:
            reason = str(exc)  # Only locally-defined constant codes reach here.
        except (OSError, http.client.HTTPException, ProtocolError):
            outcome, reason = "cancelled", "downstream_disconnected_or_slow"
        finally:
            stop.set()
            run.close_upstream()
            if producer is not None:
                producer.join(self.settings.upstream_timeout + 0.5)
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            record = self.ledger.finish(run.tenant, run.request_id, outcome, actual,
                                        known_unstarted=not run.processing_possible)
            self.audit.emit("finished", tenant=run.tenant, request_id=run.request_id,
                            outcome=outcome, reason=reason, usage_state=record["usage_state"],
                            actual_units=actual)
            if actual is None:
                error = {"code": reason, "usage_state": record["usage_state"]}
                if emitted:
                    try:
                        send_chunk(handler, encode_event("error", error))
                        end_chunks(handler)
                    except OSError:
                        pass
                else:
                    handler.reply(409 if outcome == "cancelled" else 503, {"error": error})

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def run_stats(self, tenant, request_id):
        with self.lock:
            run = self.running[tenant, request_id]
        return run.stats()

    def start(self):
        self.thread.start()
        return self

    def close(self):
        with self.lock:
            runs = list(self.running.values())
        for run in runs:
            run.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
