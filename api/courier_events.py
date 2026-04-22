"""
Hermes Courier `/v1/events` transport.

This module provides a real live-update feed that mobile clients (Android
`RealtimeConnectionManager`, iOS Courier) can consume. Two shapes are
served at `GET /v1/events`:

1. A **JSON snapshot** (HTTP 200) when the request does not carry the
   RFC 6455 WebSocket upgrade headers. Callers can poll this to get the
   same envelope that the WebSocket stream would emit.

2. A **WebSocket stream** (HTTP 101 Switching Protocols) when the request
   carries `Upgrade: websocket` + a valid `Sec-WebSocket-Key`. The server
   emits one `RealtimeEventEnvelope` text frame immediately on connect,
   then pushes a fresh snapshot whenever the dashboard/sessions/approvals
   state changes, with a heartbeat ping at least every 20 seconds so
   intermediate proxies don't idle-close the connection.

Notes
-----
* The WebSocket implementation is a minimal RFC 6455 server: text frames
  only (server → client), text + close + ping + pong handled (client →
  server). Permessage-deflate / binary / continuation frames are not
  negotiated — the envelopes are small JSON blobs well under 64 KiB.
* Authentication is the same bearer-token gate as the rest of the
  Courier `/v1/*` surface: `handle_get` in ``api/routes.py`` runs
  :func:`api.courier_routes.validate_courier_auth` before we are reached.
* The module is deliberately self-contained (no new third-party
  dependencies) so it works inside the existing ``BaseHTTPRequestHandler``
  pipeline.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import select
import socket
import struct
import time
import uuid

from api.helpers import j

logger = logging.getLogger(__name__)


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Allowed client-to-server opcodes we act on. Anything else is ignored /
# treated as a protocol error and closes the connection.
_OPCODE_CONT = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA


def _iso_now() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _snapshot_envelope() -> dict:
    """Build a `RealtimeEventEnvelope`-shaped snapshot.

    The fields match ``shared/contract/hermes-courier-api.yaml``
    (`RealtimeEventEnvelope` component): ``type``, ``eventId``,
    ``timestamp``, ``dashboard``, ``sessions``, ``approvals``.
    """
    # Local imports avoid circular dependencies at module load time.
    from api.courier_routes import (
        _list_pending_approvals,
        _session_summary,
    )
    from api.models import all_sessions

    sessions_raw = all_sessions()
    sessions = [_session_summary(s) for s in sessions_raw]
    approvals_full = _list_pending_approvals()
    approvals = [
        {
            "approvalId": a["approvalId"],
            "title": a["title"],
            "detail": a["detail"],
            "requiresBiometrics": a["requiresBiometrics"],
        }
        for a in approvals_full
    ]
    dashboard = {
        "activeSessionCount": len(sessions_raw),
        "pendingApprovalCount": len(approvals),
        "lastSyncLabel": _dt.datetime.now(tz=_dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        ),
        "connectionState": "connected",
    }
    return {
        "type": "snapshot",
        "kind": "snapshot",
        "eventId": uuid.uuid4().hex,
        "timestamp": _iso_now(),
        "dashboard": dashboard,
        "sessions": sessions,
        "approvals": approvals,
    }


def _envelope_fingerprint(env: dict) -> str:
    """Stable fingerprint of the parts of the envelope we care about for change
    detection — excludes eventId/timestamp/lastSyncLabel so we don't emit on
    every tick just because the clock moved.
    """
    dash = dict(env.get("dashboard") or {})
    dash.pop("lastSyncLabel", None)
    payload = {
        "dashboard": dash,
        "sessions": env.get("sessions") or [],
        "approvals": env.get("approvals") or [],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8"), usedforsecurity=False).hexdigest()


# ── HTTP snapshot path (no WebSocket upgrade) ────────────────────────────

def _is_websocket_upgrade(handler) -> bool:
    headers = handler.headers
    connection = (headers.get("Connection") or "").lower()
    upgrade = (headers.get("Upgrade") or "").lower().strip()
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    version = (headers.get("Sec-WebSocket-Version") or "").strip()
    if upgrade != "websocket":
        return False
    if "upgrade" not in connection:
        return False
    if not key:
        return False
    if version and version != "13":
        return False
    return True


def handle_courier_events_get(handler) -> bool:
    """Entry point for ``GET /v1/events``.

    Returns a JSON snapshot for plain HTTP callers and performs the
    WebSocket handshake + stream loop for clients that request a
    protocol upgrade.
    """
    if _is_websocket_upgrade(handler):
        return _handle_websocket(handler)
    return _handle_snapshot(handler)


def _handle_snapshot(handler) -> bool:
    env = _snapshot_envelope()
    env["transport"] = "snapshot"
    env["supported"] = True
    env["endpoint"] = "/v1/events"
    env["websocketHint"] = "Send Upgrade: websocket to stream live events."
    # Keep the classic fallback hint around for any client that still
    # inspects it so we stay backwards compatible.
    env["fallbackPollEndpoints"] = [
        "/v1/dashboard",
        "/v1/approvals",
        "/v1/conversation",
    ]
    return j(handler, env)


# ── WebSocket handshake + frame loop ─────────────────────────────────────

def _poll_interval_seconds() -> float:
    raw = os.getenv("HERMES_COURIER_EVENTS_POLL_SECONDS", "1.0").strip()
    try:
        value = float(raw or "1.0")
    except (TypeError, ValueError):
        value = 1.0
    # Hard-guard against accidental tight loops or values that would make the
    # heartbeat ineffective.
    return max(0.25, min(value, 5.0))


def _heartbeat_interval_seconds() -> float:
    raw = os.getenv("HERMES_COURIER_EVENTS_HEARTBEAT_SECONDS", "20").strip()
    try:
        value = float(raw or "20")
    except (TypeError, ValueError):
        value = 20.0
    return max(5.0, min(value, 60.0))


def _idle_timeout_seconds() -> float:
    raw = os.getenv("HERMES_COURIER_EVENTS_MAX_LIFETIME_SECONDS", "0").strip()
    try:
        value = float(raw or "0")
    except (TypeError, ValueError):
        value = 0.0
    # 0 = no upper bound; the connection stays open until the peer closes.
    return max(0.0, value)


def _handle_websocket(handler) -> bool:
    key = handler.headers.get("Sec-WebSocket-Key", "").strip()
    accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    ).decode("ascii")

    # Bypass handler.send_response so we emit ONLY the HTTP/1.1 101 line
    # plus the required handshake headers — no Server / Date / security
    # headers. Those break some strict WebSocket clients by arriving
    # before the Upgrade header.
    handshake = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")
    try:
        handler.wfile.write(handshake)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        return True

    # Prevent the HTTP handler from trying to re-use this socket for a
    # subsequent request after we return.
    handler.close_connection = True

    sock = handler.connection
    # Switch to non-blocking so the select-driven loop can interleave
    # reads, snapshot checks, and heartbeats cleanly.
    try:
        sock.settimeout(0.0)
    except OSError:
        pass

    try:
        _run_ws_loop(sock)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    except Exception:
        logger.debug("Courier /v1/events websocket loop crashed", exc_info=True)

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    return True


def _run_ws_loop(sock: socket.socket) -> None:
    poll_interval = _poll_interval_seconds()
    heartbeat_interval = _heartbeat_interval_seconds()
    idle_limit = _idle_timeout_seconds()

    # Emit an initial snapshot so the client has something useful immediately.
    current = _snapshot_envelope()
    last_fp = _envelope_fingerprint(current)
    initial = dict(current)
    initial["type"] = "snapshot.initial"
    _send_text_frame(sock, json.dumps(initial, ensure_ascii=False))

    last_heartbeat = time.monotonic()
    last_activity = time.monotonic()
    read_buffer = bytearray()

    while True:
        if idle_limit > 0 and (time.monotonic() - last_activity) > idle_limit:
            _send_close_frame(sock, code=1000, reason="idle timeout")
            return
        readable, _w, _x = select.select([sock], [], [], poll_interval)
        if readable:
            try:
                chunk = sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                chunk = b""
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
            if not chunk:
                return  # peer closed
            read_buffer.extend(chunk)
            last_activity = time.monotonic()
            done = _drain_frames(sock, read_buffer)
            if done:
                return

        now = time.monotonic()

        try:
            latest = _snapshot_envelope()
        except Exception:
            logger.debug("Courier snapshot build failed", exc_info=True)
            latest = None
        if latest is not None:
            fp = _envelope_fingerprint(latest)
            if fp != last_fp:
                last_fp = fp
                latest["type"] = "snapshot.update"
                _send_text_frame(sock, json.dumps(latest, ensure_ascii=False))
                last_heartbeat = now

        if (now - last_heartbeat) >= heartbeat_interval:
            _send_ping_frame(sock, payload=b"hermes")
            last_heartbeat = now


def _drain_frames(sock: socket.socket, buf: bytearray) -> bool:
    """Consume complete frames from *buf*. Returns True when a close frame
    was received (or an unrecoverable protocol error was hit) so the loop
    can exit.
    """
    while True:
        parsed = _try_parse_frame(buf)
        if parsed is None:
            return False
        fin, opcode, payload, consumed = parsed
        del buf[:consumed]
        if opcode == _OPCODE_CLOSE:
            _send_close_frame(sock, code=1000, reason="bye")
            return True
        if opcode == _OPCODE_PING:
            _send_frame(sock, _OPCODE_PONG, bytes(payload))
            continue
        if opcode == _OPCODE_PONG:
            continue
        if opcode in (_OPCODE_TEXT, _OPCODE_BINARY, _OPCODE_CONT):
            # Mobile clients currently never send text frames to the server;
            # we quietly drop whatever they push without interpreting it so
            # we stay forward-compatible with future control messages.
            continue
        # Unknown / unsupported opcode → close with protocol-error code.
        _send_close_frame(sock, code=1002, reason="unsupported opcode")
        return True


def _try_parse_frame(buf: bytearray):
    """Return (fin, opcode, payload_bytes, consumed) or None if incomplete."""
    if len(buf) < 2:
        return None
    b0 = buf[0]
    b1 = buf[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < offset + 2:
            return None
        length = struct.unpack(">H", bytes(buf[offset : offset + 2]))[0]
        offset += 2
    elif length == 127:
        if len(buf) < offset + 8:
            return None
        length = struct.unpack(">Q", bytes(buf[offset : offset + 8]))[0]
        offset += 8
    if masked:
        if len(buf) < offset + 4:
            return None
        mask_key = bytes(buf[offset : offset + 4])
        offset += 4
    else:
        mask_key = None
    if len(buf) < offset + length:
        return None
    raw = bytes(buf[offset : offset + length])
    if mask_key is not None:
        raw = bytes(b ^ mask_key[i % 4] for i, b in enumerate(raw))
    consumed = offset + length
    return fin, opcode, raw, consumed


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))  # FIN + opcode
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(127)
        header.extend(struct.pack(">Q", length))
    try:
        sock.sendall(bytes(header) + payload)
    except (BrokenPipeError, ConnectionResetError, OSError):
        # Caller loop re-checks select and will unwind on the next iteration.
        raise


def _send_text_frame(sock: socket.socket, text: str) -> None:
    _send_frame(sock, _OPCODE_TEXT, text.encode("utf-8"))


def _send_ping_frame(sock: socket.socket, payload: bytes = b"") -> None:
    _send_frame(sock, _OPCODE_PING, payload[:125])


def _send_close_frame(sock: socket.socket, code: int = 1000, reason: str = "") -> None:
    body = struct.pack(">H", code) + reason.encode("utf-8")[:123]
    try:
        _send_frame(sock, _OPCODE_CLOSE, body)
    except OSError:
        pass
