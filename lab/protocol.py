"""The book's explicit teaching protocol; this is not an OpenAI-compatible API."""
import json

MAX_FRAME_BYTES = 32 * 1024
UNIT_CONTRACT = "utf8-bytes-v1"


class ProtocolError(Exception):
    pass


def encode_event(event, data):
    wire = ("event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False,
            separators=(",", ":")) + "\n\n").encode("utf-8")
    if len(wire) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large")
    return wire


def read_events(response):
    """Read complete bounded SSE frames from an HTTPResponse, including chunk decoding.

    Deliberately accept only the event/data subset used by the lab. A truncated
    final frame is an error, never an event that can be executed or billed.
    """
    event, data, size = None, [], 0
    first_line = True
    for line, wire_size, terminated in _lines(response):
        size += wire_size
        if size > MAX_FRAME_BYTES:
            raise ProtocolError("frame_too_large")
        if not terminated:
            raise ProtocolError("truncated_frame")
        try:
            text = line.decode("utf-8-sig" if first_line else "utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid_utf8") from exc
        first_line = False
        if not text:
            if event is not None or data:
                try:
                    payload = json.loads("\n".join(data))
                except (ValueError, TypeError) as exc:
                    raise ProtocolError("invalid_event_json") from exc
                if event is None or not isinstance(payload, dict):
                    raise ProtocolError("invalid_event")
                yield event, payload
            event, data, size = None, [], 0
        elif text.startswith(":"):
            pass
        else:
            field, _, value = text.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
            else:
                raise ProtocolError("unsupported_sse_field")
    if size:
        raise ProtocolError("truncated_frame")


def _lines(response):
    """Incremental SSE lines: CR, LF and CRLF, including split UTF-8/CRLF.

    Charge CR as two bytes immediately, reserving a possible following LF;
    skip that LF if present. This never undercounts wire bytes and dispatches
    CR-only empty lines immediately. A CR-only frame may be rejected slightly
    before the wire limit. The caller also bounds the sum of lines in a frame.
    """
    line = bytearray()
    skip_lf = False
    while True:
        chunk = response.read1(4096)
        if not chunk:
            if line:
                yield bytes(line), len(line), False
            return
        for byte in chunk:
            if skip_lf:
                skip_lf = False
                if byte == 10:
                    continue
            if byte == 13:
                yield bytes(line), len(line) + 2, True
                line.clear()
                skip_lf = True
            elif byte == 10:
                yield bytes(line), len(line) + 1, True
                line.clear()
            else:
                line.append(byte)
                if len(line) > MAX_FRAME_BYTES:
                    raise ProtocolError("frame_too_large")


def send_chunk(handler, data):
    handler.wfile.write(format(len(data), "x").encode() + b"\r\n" + data + b"\r\n")
    handler.wfile.flush()


def end_chunks(handler):
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()
