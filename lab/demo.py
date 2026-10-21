"""Run complete HTTP fault experiments, or keep a loopback gateway running."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

from .client import cancel, generate, open_stream
from .gateway import Gateway, Route, Settings, TenantPolicy
from .protocol import UNIT_CONTRACT, read_events
from .upstream import ModelStub, Plan


def payload(request_id, **changes):
    value = {"request_id": request_id, "prompt": "hi", "capabilities": ["text"],
             "max_output_units": 100}
    value.update(changes)
    return value


def await_condition(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise RuntimeError("experiment_condition_timed_out")


def await_finished(gateway, request_id):
    return await_condition(lambda: (record if not record["active"] else None)
                           if (record := gateway.ledger.record("tenant-a", request_id)) else None)


def experiments():
    cases = []
    with ExitStack() as stack:
        first = stack.enter_context(ModelStub([Plan("safe_503")]))
        second = stack.enter_context(ModelStub([Plan(chunks=("route ", "works"))]))
        gateway = stack.enter_context(Gateway([
            Route("primary", first.url, trust_not_started=True), Route("backup", second.url)]))
        status, events = generate(gateway.url, payload("safe-failover", allow_failover=True))
        record = await_finished(gateway, "safe-failover")
        assert status == 200 and record["actual_units"] == 13
        assert first.operations_started == 0 and second.operations_started == 1
        cases.append({"name": "authorized_safe_failover", "http_status": status,
                      "events": [name for name, _ in events],
                      "upstream_operations": [first.operations_started, second.operations_started],
                      "accounting": record, "balance": gateway.ledger.snapshot("tenant-a")})
    with ExitStack() as stack:
        first = stack.enter_context(ModelStub([Plan("midstream_disconnect", chunks=("part", "never"))]))
        second = stack.enter_context(ModelStub())
        gateway = stack.enter_context(Gateway([
            Route("primary", first.url, trust_not_started=True), Route("backup", second.url)],
            policies={"tenant-a": TenantPolicy(budget_units=110)}))
        status, events = generate(gateway.url, payload("interrupted", max_output_units=20, allow_failover=True))
        record = await_finished(gateway, "interrupted")
        assert status == 200 and record["usage_state"] == "unknown" and second.requests == 0
        held = gateway.ledger.snapshot("tenant-a")
        # The fixture executed four output bytes and two input bytes. This is
        # authoritative in-process fixture evidence, not an HTTP client claim.
        gateway.ledger.reconcile("tenant-a", "interrupted", 6)
        cases.append({"name": "interrupted_not_replayed", "http_status": status,
                      "events": [name for name, _ in events], "backup_requests": second.requests,
                      "accounting_before_reconciliation": record, "balance_held": held,
                      "authoritative_fixture_units": 6,
                      "balance_after_reconciliation": gateway.ledger.snapshot("tenant-a")})
    with ExitStack() as stack:
        upstream = stack.enter_context(ModelStub([Plan(chunks=("x",) * 1000, delay=0.005)]))
        gateway = stack.enter_context(Gateway([Route("primary", upstream.url)],
            policies={"tenant-a": TenantPolicy(max_concurrent=1)}))
        connection, response = open_stream(gateway.url, payload("cancel", max_output_units=1000))
        try:
            assert next(read_events(response))[0] == "token"
            concurrency_status, _ = generate(gateway.url, payload("rejected-concurrent"))
            cancellation_status, _ = cancel(gateway.url, "cancel")
            record = await_finished(gateway, "cancel")
            await_condition(lambda: upstream.disconnects > 0)
            assert concurrency_status == 429 and cancellation_status == 202
            assert record["usage_state"] == "unknown"
            cases.append({"name": "cancel_and_concurrency", "concurrent_http_status": concurrency_status,
                          "cancel_http_status": cancellation_status, "upstream_observed_disconnect": True,
                          "accounting": record, "balance": gateway.ledger.snapshot("tenant-a")})
        finally:
            response.close()
            connection.close()
    with ExitStack() as stack:
        upstream = stack.enter_context(ModelStub([Plan(chunks=("x" * 2048,) * 2000, delay=0)]))
        gateway = stack.enter_context(Gateway([Route("primary", upstream.url)],
            policies={"tenant-a": TenantPolicy(budget_units=5_000_000)},
            settings=Settings(queue_events=2, client_write_timeout=0.5)))
        parsed = urlsplit(gateway.url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.connect()
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        connection.request("POST", "/v1/generate", body=json.dumps(payload("slow", max_output_units=4_096_000)),
            headers={"Authorization": "Bearer tenant-a-secret", "Content-Type": "application/json"})
        response = connection.getresponse()
        try:
            assert response.status == 200
            await_condition(lambda: gateway.run_stats("tenant-a", "slow")["backpressure_waits"] > 0)
            stats = gateway.run_stats("tenant-a", "slow")
            assert stats["queue_peak"] <= 2
            cancel(gateway.url, "slow")
            record = await_finished(gateway, "slow")
            await_condition(lambda: upstream.disconnects > 0)
            cases.append({"name": "slow_consumer", "queue_capacity_events": 2,
                          "queue_peak": stats["queue_peak"], "backpressure_observed": True,
                          "upstream_observed_disconnect": True, "usage_state": record["usage_state"]})
        finally:
            response.close()
            connection.close()
    with ExitStack() as stack:
        upstream = stack.enter_context(ModelStub([Plan(chunks=("private-generated-value",))]))
        gateway = stack.enter_context(Gateway([Route("primary", upstream.url)]))
        generate(gateway.url, payload("privacy", prompt="private-input-value"))
        await_finished(gateway, "privacy")
        audit = gateway.audit.jsonl()
        forbidden = ["private-input-value", "private-generated-value", "tenant-a-secret", "fixture-upstream-secret"]
        assert not any(value in audit for value in forbidden)
        cases.append({"name": "audit_allowlist", "sensitive_values_absent": True,
                      "audit": [json.loads(line) for line in audit.splitlines()]})
    return {"experiment": "loopback-http-gateway-v1", "checked_at": datetime.now(timezone.utc).isoformat(),
            "unit_contract": UNIT_CONTRACT, "cases": cases,
            "limits": ["one process; volatile ledger and audit", "loopback HTTP; fixture credentials",
                       "no vendor API or real model capability evaluation",
                       "queue bound excludes socket and HTTP library buffers",
                       "socket cancellation does not prove provider compute or billing stopped"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, help="Also save the executed experiment record as JSON")
    parser.add_argument("--serve", action="store_true", help="Serve a normal text fixture until Ctrl-C")
    parser.add_argument("--port", type=int, default=8088, help="Loopback gateway port in --serve mode")
    args = parser.parse_args()
    if args.serve:
        with ModelStub() as upstream, Gateway([Route("local-text", upstream.url)], port=args.port) as gateway:
            print(json.dumps({"gateway": gateway.url, "credential": "tenant-a-secret",
                              "unit_contract": UNIT_CONTRACT, "capabilities": ["text"]}), flush=True)
            try:
                while True:
                    time.sleep(0.2)
            except KeyboardInterrupt:
                print(gateway.audit.jsonl())
        return
    result = experiments()
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
