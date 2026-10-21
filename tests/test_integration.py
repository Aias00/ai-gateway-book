"""Real loopback HTTP/SSE tests: no vendor SDKs, model keys or policy-only mocks."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import http.client
import json
import socket
import threading
import time
import unittest
from urllib.parse import urlsplit

from lab.client import cancel, generate, open_stream
from lab.gateway import Gateway, Route, Settings, TenantPolicy
from lab.protocol import UNIT_CONTRACT, read_events
from lab.upstream import ModelStub, Plan


def request(request_id="example", **changes):
    data = {"request_id": request_id, "prompt": "hi", "capabilities": ["text"],
            "max_output_units": 100}
    data.update(changes)
    return data


def eventually(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


class GatewayHTTPTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def stub(self, *plans):
        return self.stack.enter_context(ModelStub(plans))

    def gateway(self, routes, **kwargs):
        return self.stack.enter_context(Gateway(routes, **kwargs))

    def finished(self, gateway, request_id, tenant="tenant-a"):
        return eventually(lambda: (r if r and not r["active"] else None)
                          if (r := gateway.ledger.record(tenant, request_id)) is not None else None)

    def test_real_http_stream_and_trusted_byte_usage(self):
        stub = self.stub(Plan(chunks=("你好", " AI")))
        gateway = self.gateway([Route("local", stub.url)])
        status, events = generate(gateway.url, request(prompt="问题", max_output_units=20))
        self.assertEqual(status, 200)
        self.assertEqual([e for e, _ in events], ["token", "token", "usage", "done"])
        self.assertEqual(events[-2][1], {"contract": UNIT_CONTRACT, "input_units": 6, "output_units": 9})
        record = self.finished(gateway, "example")
        self.assertEqual(record["usage_state"], "settled")
        self.assertEqual(record["actual_units"], 15)
        self.assertEqual(gateway.ledger.snapshot("tenant-a")["held_units"], 0)
        self.assertEqual(stub.operations_started, 1)

    def test_capability_filter_precedes_admission(self):
        text, image = self.stub(), self.stub()
        gateway = self.gateway([Route("text-only", text.url),
                                Route("vision", image.url, frozenset({"text", "image"}))])
        status, _ = generate(gateway.url, request(capabilities=["image"]))
        self.assertEqual(status, 200)
        self.assertEqual((text.requests, image.requests), (0, 1))
        status, body = generate(gateway.url, request("audio", capabilities=["audio"]))
        self.assertEqual((status, body["error"]), (422, "unsupported_capability"))
        self.assertIsNone(gateway.ledger.record("tenant-a", "audio"))

    def test_default_never_replays_post_even_trusted_not_started(self):
        first, second = self.stub(Plan("safe_503")), self.stub()
        gateway = self.gateway([Route("first", first.url, trust_not_started=True), Route("second", second.url)])
        status, _ = generate(gateway.url, request())
        self.assertEqual(status, 503)
        self.assertEqual((first.requests, second.requests), (1, 0))
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "released")

    def test_explicit_failover_with_trusted_not_started_promise(self):
        first, second = self.stub(Plan("safe_503")), self.stub()
        gateway = self.gateway([Route("first", first.url, trust_not_started=True), Route("second", second.url)])
        status, events = generate(gateway.url, request(allow_failover=True))
        self.assertEqual(status, 200)
        self.assertEqual(events[-1], ("done", {}))
        self.assertEqual((first.requests, first.operations_started, second.operations_started), (1, 0, 1))
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "settled")

    def test_unknown_503_never_replayed_and_keeps_reservation(self):
        first, second = self.stub(Plan("unknown_503")), self.stub()
        gateway = self.gateway([Route("first", first.url, trust_not_started=True), Route("second", second.url)])
        status, _ = generate(gateway.url, request(allow_failover=True))
        self.assertEqual(status, 503)
        self.assertEqual((first.operations_started, second.requests), (1, 0))
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")
        self.assertEqual(gateway.ledger.snapshot("tenant-a")["held_units"], 102)

    def test_untrusted_route_cannot_declare_safe_replay(self):
        first, second = self.stub(Plan("safe_503")), self.stub()
        gateway = self.gateway([Route("untrusted", first.url), Route("second", second.url)])
        status, _ = generate(gateway.url, request(allow_failover=True))
        self.assertEqual(status, 503)
        self.assertEqual(second.requests, 0)
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")

    def test_failover_has_finite_attempt_bound(self):
        a, b, c = self.stub(Plan("safe_503")), self.stub(Plan("safe_503")), self.stub()
        gateway = self.gateway([Route("a", a.url, trust_not_started=True),
                                Route("b", b.url, trust_not_started=True), Route("c", c.url)])
        self.assertEqual(generate(gateway.url, request(allow_failover=True))[0], 503)
        self.assertEqual((a.requests, b.requests, c.requests), (1, 1, 0))
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "released")

    def test_connect_failure_has_no_automatic_new_attempt(self):
        unavailable = socket.socket()
        unavailable.bind(("127.0.0.1", 0))  # Reserved port, deliberately not listening.
        self.addCleanup(unavailable.close)
        second = self.stub()
        gateway = self.gateway([Route("unavailable", "http://127.0.0.1:" + str(unavailable.getsockname()[1])),
                                Route("backup", second.url)])
        status, _ = generate(gateway.url, request(allow_failover=True))
        self.assertEqual(status, 503)
        self.assertEqual(second.requests, 0)
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "released")

    def test_sse_line_endings_multidata_and_fragmented_utf8_over_http(self):
        for ending in ["\n", "\r\n", "\r"]:
            with self.subTest(ending=repr(ending)):
                lines = [": comment", "event:token", 'data:{"text":', 'data: "你"}', "",
                         "event:usage", 'data:{"contract":"utf8-bytes-v1","input_units":2,"output_units":3}', "",
                         "event:done", "data:{}", "", ""]
                wire = ending.join(lines).encode("utf-8")
                first_byte = wire.index("你".encode())
                chunks = (wire[:first_byte + 1], wire[first_byte + 1:first_byte + 2], wire[first_byte + 2:])
                stub = self.stub(Plan(raw_chunks=chunks))
                gateway = self.gateway([Route("local", stub.url)])
                status, events = generate(gateway.url, request())
                self.assertEqual(status, 200)
                self.assertEqual(events[0], ("token", {"text": "你"}))
                self.assertEqual(events[-1][0], "done")
                self.assertEqual(self.finished(gateway, "example")["actual_units"], 5)

    def test_eof_without_empty_sse_line_does_not_dispatch_partial_token(self):
        stub = self.stub(Plan(raw_chunks=(b'event:token\ndata:{"text":"partial"}\n',)))
        gateway = self.gateway([Route("local", stub.url)])
        status, body = generate(gateway.url, request())
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["usage_state"], "unknown")
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")

    def test_cr_only_complete_event_is_dispatched_before_any_later_bytes(self):
        release = threading.Event()
        first = b'event:token\rdata:{"text":"x"}\r\r'
        later = (b'event:usage\rdata:{"contract":"utf8-bytes-v1","input_units":2,"output_units":1}\r\r'
                 b'event:done\rdata:{}\r\r')
        stub = self.stub(Plan(raw_chunks=(first, later), release_after_first_chunk=release))
        gateway = self.gateway([Route("local", stub.url)])
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(open_stream, gateway.url, request())
            try:
                # Stub cannot write later bytes until we release it. Getting a
                # valid token here therefore proves immediate CR dispatch.
                connection, response = future.result(timeout=1)
                try:
                    events = read_events(response)
                    self.assertEqual(next(events), ("token", {"text": "x"}))
                    self.assertFalse(release.is_set())
                    release.set()
                    self.assertEqual(list(events)[-1], ("done", {}))
                finally:
                    response.close()
                    connection.close()
            finally:
                release.set()
        self.assertEqual(self.finished(gateway, "example")["actual_units"], 3)

    def test_oversized_sse_frame_is_rejected_before_forwarding(self):
        wire = b'event:token\ndata:{"text":"' + b'x' * (33 * 1024) + b'"}\n\n'
        stub = self.stub(Plan(raw_chunks=(wire,)))
        gateway = self.gateway([Route("local", stub.url)])
        status, _ = generate(gateway.url, request())
        self.assertEqual(status, 503)
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")

    def test_no_output_does_not_prove_no_execution(self):
        first, second = self.stub(Plan("pre_event_disconnect")), self.stub()
        gateway = self.gateway([Route("first", first.url, trust_not_started=True), Route("second", second.url)])
        self.assertEqual(generate(gateway.url, request(allow_failover=True))[0], 503)
        self.assertEqual((first.operations_started, second.requests), (1, 0))
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")

    def test_midstream_disconnect_reports_error_without_replay(self):
        first, second = self.stub(Plan("midstream_disconnect")), self.stub()
        gateway = self.gateway([Route("first", first.url, trust_not_started=True), Route("second", second.url)])
        status, events = generate(gateway.url, request(allow_failover=True))
        self.assertEqual(status, 200)
        self.assertEqual([e for e, _ in events], ["token", "error"])
        self.assertEqual(events[-1][1]["usage_state"], "unknown")
        self.assertEqual((first.operations_started, second.requests), (1, 0))
        self.finished(gateway, "example")

    def test_first_event_timeout_is_unknown_and_not_replayed(self):
        first, second = self.stub(Plan(first_delay=0.25)), self.stub()
        gateway = self.gateway([Route("first", first.url, trust_not_started=True), Route("second", second.url)],
                               settings=Settings(upstream_timeout=0.08))
        self.assertEqual(generate(gateway.url, request(allow_failover=True))[0], 503)
        self.assertEqual(second.requests, 0)
        self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")

    def test_missing_or_false_usage_cannot_settle(self):
        for mode in ["omit_usage", "invalid_usage"]:
            with self.subTest(mode=mode):
                stub = self.stub(Plan(mode))
                gateway = self.gateway([Route("local", stub.url)])
                status, events = generate(gateway.url, request())
                self.assertEqual(status, 200)
                self.assertEqual(events[-1][0], "error")
                self.assertEqual(self.finished(gateway, "example")["usage_state"], "unknown")

    def test_client_cannot_set_usage_or_tenant(self):
        stub = self.stub()
        gateway = self.gateway([Route("local", stub.url)])
        for extra in [{"usage": {"output_units": 0}}, {"tenant": "tenant-b"}, {"allow_failover": "true"}]:
            with self.subTest(extra=extra):
                self.assertEqual(generate(gateway.url, request(**extra))[0], 400)
        self.assertEqual(stub.requests, 0)

    def test_output_text_is_data_and_never_accounting(self):
        text = '{"usage":{"input_units":0,"output_units":0}}'
        stub = self.stub(Plan(chunks=(text,)))
        gateway = self.gateway([Route("local", stub.url)])
        status, events = generate(gateway.url, request())
        self.assertEqual(status, 200)
        self.assertEqual(events[0], ("token", {"text": text}))
        self.assertEqual(self.finished(gateway, "example")["actual_units"], len(text) + 2)

    def test_atomic_tenant_concurrency_admission(self):
        stub = self.stub(Plan(chunks=("x",) * 200, delay=0.01))
        gateway = self.gateway([Route("local", stub.url)], policies={"tenant-a": TenantPolicy(max_concurrent=1)})
        connection, response = open_stream(gateway.url, request("held", max_output_units=200))
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        self.assertEqual(next(read_events(response))[0], "token")
        self.assertEqual(generate(gateway.url, request("excess"))[0], 429)
        self.assertEqual(stub.requests, 1)
        self.assertEqual(cancel(gateway.url, "held")[0], 202)
        self.finished(gateway, "held")

    def test_parallel_reservation_cannot_overspend(self):
        stub = self.stub(Plan(first_delay=0.05))
        gateway = self.gateway([Route("local", stub.url)],
                               policies={"tenant-a": TenantPolicy(budget_units=150, max_concurrent=10)})
        barrier = threading.Barrier(6)

        def call(i):
            barrier.wait()
            return generate(gateway.url, request("parallel-" + str(i)))[0]

        with ThreadPoolExecutor(max_workers=6) as executor:
            statuses = list(executor.map(call, range(6)))
        # Initial reservation is 102; after completion actual is 15. Timing can
        # admit more later calls, but the balance must never become negative.
        self.assertIn(200, statuses)
        self.assertIn(402, statuses)
        self.assertTrue(all(s in {200, 402} for s in statuses))
        self.assertGreaterEqual(gateway.ledger.snapshot("tenant-a")["available_units"], 0)

    def test_interrupted_usage_is_held_until_authoritative_reconciliation(self):
        stub = self.stub(Plan("midstream_disconnect"))
        gateway = self.gateway([Route("local", stub.url)],
                               policies={"tenant-a": TenantPolicy(budget_units=110)})
        generate(gateway.url, request())
        self.finished(gateway, "example")
        self.assertEqual(generate(gateway.url, request("blocked"))[0], 402)
        self.assertEqual(gateway.ledger.snapshot("tenant-a")["active"], 0)
        gateway.ledger.reconcile("tenant-a", "example", 7)
        self.assertEqual(gateway.ledger.snapshot("tenant-a")["spent_units"], 7)
        self.assertEqual(gateway.ledger.snapshot("tenant-a")["held_units"], 0)
        self.assertEqual(generate(gateway.url, request("after-reconcile"))[0], 200)

    def test_duplicate_id_does_not_duplicate_upstream_side_effect(self):
        stub = self.stub()
        gateway = self.gateway([Route("local", stub.url)])
        self.assertEqual(generate(gateway.url, request())[0], 200)
        self.finished(gateway, "example")
        self.assertEqual(generate(gateway.url, request())[0], 409)
        self.assertEqual(stub.operations_started, 1)

    def test_cancellation_is_tenant_scoped_and_propagates_upstream(self):
        stub = self.stub(Plan(chunks=("x",) * 1000, delay=0.005))
        gateway = self.gateway([Route("local", stub.url)],
                               policies={"tenant-a": TenantPolicy(), "tenant-b": TenantPolicy()},
                               credentials={"tenant-a-secret": "tenant-a", "tenant-b-secret": "tenant-b"})
        connection, response = open_stream(gateway.url, request("cancel-me", max_output_units=1000))
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        self.assertEqual(next(read_events(response))[0], "token")
        self.assertEqual(cancel(gateway.url, "cancel-me", "tenant-b-secret")[0], 404)
        self.assertTrue(gateway.ledger.record("tenant-a", "cancel-me")["active"])
        self.assertEqual(cancel(gateway.url, "cancel-me")[0], 202)
        record = self.finished(gateway, "cancel-me")
        self.assertEqual((record["outcome"], record["usage_state"]), ("cancelled", "unknown"))
        eventually(lambda: stub.disconnects > 0)
        eventually(lambda: gateway.run_stats("tenant-a", "cancel-me")["producer_stopped"])
        self.assertEqual(gateway.ledger.snapshot("tenant-a")["active"], 0)

    def test_slow_consumer_has_bounded_event_queue_and_backpressure(self):
        stub = self.stub(Plan(chunks=("x" * 2048,) * 2000, delay=0))
        gateway = self.gateway([Route("local", stub.url)],
                               policies={"tenant-a": TenantPolicy(budget_units=5_000_000)},
                               settings=Settings(queue_events=2, client_write_timeout=0.5))
        parsed = urlsplit(gateway.url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.connect()
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        connection.request("POST", "/v1/generate", body=json.dumps(request("slow", max_output_units=4_096_000)),
                           headers={"Authorization": "Bearer tenant-a-secret", "Content-Type": "application/json"})
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        eventually(lambda: gateway.run_stats("tenant-a", "slow")["backpressure_waits"] > 0)
        self.assertLessEqual(gateway.run_stats("tenant-a", "slow")["queue_peak"], 2)
        self.assertLess(stub.events_written, 2000)
        cancel(gateway.url, "slow")
        self.assertEqual(self.finished(gateway, "slow")["usage_state"], "unknown")
        eventually(lambda: stub.disconnects > 0)

    def test_downstream_socket_close_stops_upstream_without_explicit_cancel(self):
        stub = self.stub(Plan(chunks=("x",) * 1000, delay=0.005))
        gateway = self.gateway([Route("local", stub.url)])
        connection, response = open_stream(gateway.url, request("disconnect", max_output_units=1000))
        self.assertEqual(next(read_events(response))[0], "token")
        # Force a real TCP close, not just abandoning an iterator while keeping
        # an HTTPResponse buffered file and socket alive.
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        response.close()
        connection.close()
        self.assertEqual(self.finished(gateway, "disconnect")["usage_state"], "unknown")
        eventually(lambda: stub.disconnects > 0)

    def test_audit_is_useful_without_prompt_tokens_or_keys(self):
        marker = "sensitive-prompt-marker"
        output = "sensitive-generated-marker"
        stub = self.stub(Plan(chunks=(output,)))
        gateway = self.gateway([Route("local", stub.url)])
        generate(gateway.url, request(prompt=marker))
        self.finished(gateway, "example")
        audit = gateway.audit.jsonl()
        self.assertIn('"event": "route_attempt"', audit)
        self.assertIn('"usage_state": "settled"', audit)
        for secret in [marker, output, "tenant-a-secret", "fixture-upstream-secret", "Authorization"]:
            self.assertNotIn(secret, audit)
        self.assertEqual(generate(gateway.url, request("unauth"), "wrong-secret")[0], 401)
        self.assertEqual(stub.requests, 1)


if __name__ == "__main__":
    unittest.main()
