"""Minimal HTTP/SSE client used by both the demo and integration tests."""
import http.client
import json
from urllib.parse import urlsplit

from .protocol import read_events


def open_stream(url, request, credential="tenant-a-secret", timeout=5):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    connection.request("POST", "/v1/generate", body=json.dumps(request).encode(), headers={
        "Authorization": "Bearer " + credential, "Content-Type": "application/json",
    })
    return connection, connection.getresponse()


def generate(url, request, credential="tenant-a-secret"):
    connection, response = open_stream(url, request, credential)
    try:
        if response.status != 200:
            return response.status, json.loads(response.read())
        return response.status, list(read_events(response))
    finally:
        response.close()
        connection.close()


def cancel(url, request_id, credential="tenant-a-secret"):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request("DELETE", "/v1/runs/" + request_id,
                       headers={"Authorization": "Bearer " + credential})
    response = connection.getresponse()
    try:
        return response.status, json.loads(response.read())
    finally:
        response.close()
        connection.close()
