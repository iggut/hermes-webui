import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from api.courier_pairing import (
    build_pairing_payload,
    courier_pairing_deployment_snapshot,
    resolve_courier_gateway_for_pairing,
)
from api.courier_routes import courier_runtime_status
from tests.conftest import make_session_tracked


def _import_seeded_session(base_url, cleanup_list, title: str, user_body: str) -> str:
    """Create a session with pre-populated user messages via /api/session/import.

    Used to exercise the session-scoped conversation contract without a
    working agent runtime (the runtime is unavailable in the hermetic test
    subprocess so POST /v1/conversation cannot persist real messages).
    Returns the new session id.
    """
    req = urllib.request.Request(
        base_url + "/api/session/import",
        data=json.dumps(
            {
                "title": title,
                "messages": [
                    {"role": "user", "content": user_body, "timestamp": time.time()},
                ],
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    sid = payload["session"]["session_id"]
    cleanup_list.append(sid)
    return sid


COURIER_TOKEN = "test-courier-token"


def _request(base_url, method, path, body=None, bearer=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=payload, method=method)
    req.add_header("Accept", "application/json")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8") if resp.readable() else ""
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else None


def test_courier_dashboard_requires_bearer(base_url):
    code, payload = _request(base_url, "GET", "/v1/dashboard")
    assert code == 401
    assert payload["supported"] is False


def test_courier_dashboard_sessions_and_detail(base_url, cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    _request(base_url, "POST", "/api/session/rename", body={"session_id": sid, "title": "Courier test"}, bearer=None)

    code, payload = _request(base_url, "GET", "/v1/dashboard", bearer=COURIER_TOKEN)
    assert code == 200
    assert {"activeSessionCount", "pendingApprovalCount", "lastSyncLabel", "connectionState"} <= set(payload.keys())

    code, sessions = _request(base_url, "GET", "/v1/sessions", bearer=COURIER_TOKEN)
    assert code == 200
    assert isinstance(sessions, list)
    assert any(item["sessionId"] == sid for item in sessions)

    code, detail = _request(base_url, "GET", f"/v1/sessions/{sid}", bearer=COURIER_TOKEN)
    assert code == 200
    assert detail["sessionId"] == sid
    assert {"title", "status", "updatedAt"} <= set(detail.keys())


def test_courier_session_control_explicit_unsupported(base_url, cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    code, payload = _request(
        base_url,
        "POST",
        f"/v1/sessions/{sid}/actions",
        body={"action": "pause"},
        bearer=COURIER_TOKEN,
    )
    assert code == 200
    assert payload["supported"] is False
    assert payload["status"] == "unsupported"


def test_courier_approvals_and_decision(base_url, cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    urllib.request.urlopen(
        urllib.request.Request(
            base_url + f"/api/approval/inject_test?session_id={sid}&pattern_key=test_key&command=echo+hello",
            method="GET",
        ),
        timeout=10,
    ).read()

    code, approvals = _request(base_url, "GET", "/v1/approvals", bearer=COURIER_TOKEN)
    assert code == 200
    assert approvals
    approval_id = approvals[0]["approvalId"]

    code, decision = _request(
        base_url,
        "POST",
        f"/v1/approvals/{approval_id}/decision",
        body={"decision": "deny", "reason": "test"},
        bearer=COURIER_TOKEN,
    )
    assert code == 200
    assert decision["ok"] is True


def test_courier_conversation_and_events_reachability(base_url, cleanup_test_sessions):
    sid = _import_seeded_session(
        base_url,
        cleanup_test_sessions,
        title="Session-scoped reachability",
        user_body="session-scoped reachability ping",
    )

    code, payload = _request(
        base_url, "GET", f"/v1/conversation?sessionId={sid}", bearer=COURIER_TOKEN
    )
    assert code == 200
    assert isinstance(payload, list)
    assert payload, "Expected at least one conversation event for the seeded session"
    for event in payload:
        assert event["sessionId"] == sid
        assert {"sessionId", "eventId", "author", "body", "timestamp"} <= set(event.keys())

    code, events = _request(base_url, "GET", "/v1/events", bearer=COURIER_TOKEN)
    assert code == 200
    assert events["supported"] is True
    assert events["transport"] == "snapshot"
    assert events["endpoint"] == "/v1/events"
    assert events["type"].startswith("snapshot")
    assert isinstance(events.get("sessions"), list)
    assert isinstance(events.get("approvals"), list)
    assert isinstance(events.get("dashboard"), dict)
    assert "activeSessionCount" in events["dashboard"]
    conversation = events.get("conversation")
    assert isinstance(conversation, dict), (
        "Expected the realtime envelope to carry a conversation event for the "
        "most-recently-updated session"
    )
    assert conversation["sessionId"], "Realtime conversation event must carry a sessionId"
    assert conversation["body"]


def test_courier_conversation_get_filters_by_session_id(base_url, cleanup_test_sessions):
    sid_a = _import_seeded_session(
        base_url,
        cleanup_test_sessions,
        title="Courier scope A",
        user_body="message in session A",
    )
    sid_b = _import_seeded_session(
        base_url,
        cleanup_test_sessions,
        title="Courier scope B",
        user_body="message in session B",
    )

    code, events_a = _request(
        base_url, "GET", f"/v1/conversation?sessionId={sid_a}", bearer=COURIER_TOKEN
    )
    assert code == 200
    assert events_a and all(event["sessionId"] == sid_a for event in events_a)
    assert any("session A" in event["body"] for event in events_a)
    assert not any("session B" in event["body"] for event in events_a)

    code, events_b = _request(
        base_url, "GET", f"/v1/conversation?sessionId={sid_b}", bearer=COURIER_TOKEN
    )
    assert code == 200
    assert events_b and all(event["sessionId"] == sid_b for event in events_b)
    assert any("session B" in event["body"] for event in events_b)

    code, events_unknown = _request(
        base_url,
        "GET",
        "/v1/conversation?sessionId=does-not-exist",
        bearer=COURIER_TOKEN,
    )
    assert code == 200
    assert events_unknown == []


def _ws_handshake(base_url: str, path: str, bearer: str):
    """Minimal RFC 6455 client: opens a TCP socket, sends the upgrade,
    reads the 101 response + the first text frame, then closes.
    """
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {bearer}\r\n"
        "\r\n"
    ).encode("ascii")
    sock = socket.create_connection((host, port), timeout=10)
    sock.sendall(request)

    buf = b""
    deadline = time.monotonic() + 10
    while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    head_text = head.decode("iso-8859-1")
    status_line = head_text.split("\r\n", 1)[0]
    headers = {}
    for line in head_text.split("\r\n")[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()

    # Read at least one full frame from `rest` (plus more bytes if needed).
    while len(rest) < 2 and time.monotonic() < deadline:
        more = sock.recv(4096)
        if not more:
            break
        rest += more
    frame_payload = None
    opcode = None
    if len(rest) >= 2:
        b0 = rest[0]
        b1 = rest[1]
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        offset = 2
        if length == 126:
            while len(rest) < offset + 2:
                rest += sock.recv(4096)
            length = struct.unpack(">H", rest[offset : offset + 2])[0]
            offset += 2
        elif length == 127:
            while len(rest) < offset + 8:
                rest += sock.recv(4096)
            length = struct.unpack(">Q", rest[offset : offset + 8])[0]
            offset += 8
        while len(rest) < offset + length and time.monotonic() < deadline:
            more = sock.recv(4096)
            if not more:
                break
            rest += more
        frame_payload = rest[offset : offset + length]

    sock.close()
    return status_line, headers, key, opcode, frame_payload


def test_courier_events_websocket_upgrade_returns_real_snapshot(base_url, cleanup_test_sessions):
    make_session_tracked(cleanup_test_sessions)
    status_line, headers, sent_key, opcode, frame = _ws_handshake(
        base_url, "/v1/events", COURIER_TOKEN
    )
    assert status_line.startswith("HTTP/1.1 101")
    assert headers.get("upgrade", "").lower() == "websocket"
    assert headers.get("connection", "").lower() == "upgrade"
    expected_accept = base64.b64encode(
        hashlib.sha1(
            (sent_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")
    assert headers.get("sec-websocket-accept") == expected_accept
    assert opcode == 0x1, f"expected text frame, got opcode {opcode!r}"
    assert frame is not None and len(frame) > 0
    envelope = json.loads(frame.decode("utf-8"))
    assert envelope["type"].startswith("snapshot")
    assert "dashboard" in envelope
    assert "sessions" in envelope
    assert "approvals" in envelope


def test_courier_events_websocket_requires_bearer(base_url):
    # Bearer token missing → 401 JSON response before upgrade even starts.
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    request = (
        "GET /v1/events HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode('ascii')}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(request)
    buf = b""
    deadline = time.monotonic() + 5
    while b"\r\n\r\n" not in buf and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    sock.close()
    status_line = buf.split(b"\r\n", 1)[0].decode("iso-8859-1")
    assert " 401 " in status_line, f"expected 401 response, got: {status_line!r}"


def test_courier_debug_seed_creates_decidable_approval(base_url, cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    code, seeded = _request(
        base_url,
        "POST",
        "/v1/approvals/_debug_seed",
        body={"sessionId": sid, "title": "Seeded smoke approval", "command": "echo smoke"},
        bearer=COURIER_TOKEN,
    )
    assert code == 200
    assert seeded["ok"] is True
    assert seeded["sessionId"] == sid
    approval_id = seeded["approvalId"]
    assert approval_id

    code, approvals = _request(base_url, "GET", "/v1/approvals", bearer=COURIER_TOKEN)
    assert code == 200
    assert any(item["approvalId"] == approval_id for item in approvals)

    code, decision = _request(
        base_url,
        "POST",
        f"/v1/approvals/{approval_id}/decision",
        body={"decision": "approve"},
        bearer=COURIER_TOKEN,
    )
    assert code == 200
    assert decision["status"] == "ok"
    assert decision["action"] == "approve"
    assert decision["approvalId"] == approval_id


def test_courier_conversation_post_returns_fast_unsupported_when_runtime_unavailable(base_url, cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    started = time.monotonic()
    code, payload = _request(
        base_url,
        "POST",
        "/v1/conversation",
        body={"sessionId": sid, "body": "ping"},
        bearer=COURIER_TOKEN,
    )
    elapsed = time.monotonic() - started
    assert code == 200
    assert payload["status"] in {"ok", "unsupported"}
    assert isinstance(payload["body"], str) and payload["body"].strip()
    assert payload["sessionId"] == sid, (
        "POST /v1/conversation response must echo the target sessionId so the "
        "client can associate the assistant turn with the correct session"
    )
    if payload["status"] == "unsupported":
        assert payload["supported"] is False
        assert elapsed < 6.0


def test_courier_pairing_parse_accepts_android_enrollment_uri(base_url):
    payload = (
        "hermes-courier-enroll://gateway?"
        "gatewayUrl=https%3A%2F%2Fgateway.example&"
        "deviceId=android-courier-pixel&"
        "publicKeyFingerprint=abc123&"
        "appVersion=0.1.0&"
        "issuedAt=2026-04-21T00%3A00%3A00Z"
    )
    code, data = _request(
        base_url,
        "POST",
        "/api/courier/pairing/parse",
        body={"payload": payload},
    )
    assert code == 200
    assert data["ok"] is True
    assert data["enrollment"]["gatewayUrl"] == "https://gateway.example"
    assert data["enrollment"]["deviceId"] == "android-courier-pixel"


def test_courier_pairing_parse_rejects_missing_required_fields(base_url):
    code, data = _request(
        base_url,
        "POST",
        "/api/courier/pairing/parse",
        body={"payload": '{"gatewayUrl":"https://gateway.example"}'},
    )
    assert code == 400
    assert "Missing required field" in data["error"]


def test_courier_pairing_generate_returns_compatible_uri(base_url):
    enrollment = {
        "gatewayUrl": "https://gateway.example",
        "deviceId": "android-courier-pixel",
        "publicKeyFingerprint": "abc123",
        "appVersion": "0.1.0",
        "issuedAt": "2026-04-21T00:00:00Z",
    }
    code, data = _request(
        base_url,
        "POST",
        "/api/courier/pairing/generate",
        body={"enrollment": enrollment},
    )
    assert code == 200
    assert data["ok"] is True
    assert data["pairingUri"].startswith("hermes-courier-enroll://gateway?")
    assert "gatewayUrl=https%3A%2F%2Fgateway.example" in data["pairingUri"]
    assert data["pairingPayload"]["courierMode"] == "bearer-token"
    assert data["pairingPayload"]["pairingMode"] == "token-only"
    assert data["pairingPayload"]["pairingContractVersion"] == "2026-04-21"
    assert data["pairingPayload"]["apiBasePath"] == "/v1"
    assert data["pairingPayload"]["bearerToken"] == COURIER_TOKEN
    assert data["tokenIncluded"] is True
    assert data["pairingPayload"]["bearerToken"] == COURIER_TOKEN
    assert data["pairingQrDataUrl"].startswith("data:image/svg+xml")
    assert data["pairingMode"] == "token-only"
    assert data["pairingContractVersion"] == "2026-04-21"
    assert data["postScanBootstrapSupported"] is False



def test_courier_pairing_status_reports_token_backed_availability(base_url):
    code, data = _request(base_url, "GET", "/api/courier/pairing/status")
    assert code == 200
    assert data["bearerTokenConfigured"] is True
    assert data["tokenBackedPairingAvailable"] is True
    assert data["pairingMode"] == "token-only"
    assert data["qrPairingAvailable"] is True
    assert data["postScanBootstrapAvailable"] is False
    assert data["courierEnabled"] is True
    assert data["defaultPairingGatewayUrl"] == "http://127.0.0.1:8787"
    assert data["gatewayUrlSource"] == "default_local"
    assert data["externalBaseUrlConfigured"] is False
    assert data["externalBaseUrl"] == ""
    assert data["tailscaleProfileReady"] is False
    assert data["pairingUrlMode"] == "local"
    assert isinstance(data["pairingWarnings"], list)
    assert isinstance(data["issues"], list)
    assert isinstance(data["unavailableReasons"], list)


def test_courier_pairing_generate_status_matches_payload(base_url):
    code, status = _request(base_url, "GET", "/api/courier/pairing/status")
    assert code == 200
    code, data = _request(base_url, "POST", "/api/courier/pairing/generate", body={})
    assert code == 200
    assert data["tokenIncluded"] is status["tokenBackedPairingAvailable"]
    assert data["pairingQrSourceUri"] == data["pairingUri"]


def test_courier_v1_status_reports_runtime_state(base_url):
    code, payload = _request(base_url, "GET", "/v1/status", bearer=COURIER_TOKEN)
    assert code == 200
    assert payload["endpoint"] == "/v1"
    assert payload["auth"]["mode"] == "bearer-token"
    assert payload["auth"]["bearerTokenConfigured"] is True
    assert payload["auth"]["courierEnabled"] is True
    assert payload["pairing"]["tokenBackedPairingAvailable"] is True
    assert payload["pairing"]["pairingMode"] == "token-only"
    assert payload["pairing"]["qrPairingAvailable"] is True
    assert payload["pairing"]["postScanBootstrapAvailable"] is False
    assert payload["pairing"]["defaultPairingGatewayUrl"] == "http://127.0.0.1:8787"
    assert payload["pairing"]["pairingUrlMode"] == "local"
    assert payload["pairing"]["tailscaleProfileReady"] is False
    assert isinstance(payload["pairing"]["pairingWarnings"], list)
    assert "runtime" in payload


def test_courier_pairing_status_reports_missing_env_reasons(monkeypatch):
    monkeypatch.delenv("HERMES_COURIER_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_COURIER_ENABLE", "0")
    status = courier_runtime_status()
    pairing = status["pairing"]
    assert pairing["tokenBackedPairingAvailable"] is False
    assert pairing["qrPairingAvailable"] is False
    assert pairing["pairingMode"] == "unavailable"
    assert pairing["postScanBootstrapAvailable"] is False
    assert pairing["pairingUrlMode"] == "unavailable"
    assert any("HERMES_COURIER_BEARER_TOKEN is not set." in item for item in pairing["unavailableReasons"])


def test_external_base_url_overrides_pairing_gateway_url(monkeypatch):
    monkeypatch.setenv("HERMES_COURIER_EXTERNAL_BASE_URL", "https://myhost.mytailnet.ts.net")
    monkeypatch.setenv("HERMES_COURIER_BEARER_TOKEN", "tok")
    monkeypatch.setenv("HERMES_COURIER_ENABLE", "1")
    out = build_pairing_payload(None, include_bearer=True)
    assert out["pairingPayload"]["gatewayUrl"] == "https://myhost.mytailnet.ts.net"
    assert out["gatewayUrlSource"] == "external"
    assert out["pairingPayload"]["gatewayUrl"] == "https://myhost.mytailnet.ts.net"


def test_external_base_wins_over_legacy_gateway_url(monkeypatch):
    monkeypatch.setenv("HERMES_COURIER_EXTERNAL_BASE_URL", "https://a.ts.net")
    monkeypatch.setenv("HERMES_COURIER_GATEWAY_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("HERMES_COURIER_BEARER_TOKEN", "tok")
    r = resolve_courier_gateway_for_pairing(None)
    assert r["gatewayUrl"] == "https://a.ts.net"
    assert r["gatewayUrlSource"] == "external"


def test_deployment_snapshot_tailscale_profile_ready(monkeypatch):
    monkeypatch.setenv("HERMES_COURIER_EXTERNAL_BASE_URL", "https://router.tailabc.ts.net")
    monkeypatch.setenv("HERMES_COURIER_BEARER_TOKEN", "tok")
    monkeypatch.setenv("HERMES_COURIER_ENABLE", "1")
    snap = courier_pairing_deployment_snapshot()
    assert snap["tailscaleProfileReady"] is True
    assert snap["pairingUrlMode"] == "tailnet"
    assert snap["defaultPairingGatewayUrl"] == "https://router.tailabc.ts.net"


def test_deployment_snapshot_warns_on_http_external(monkeypatch):
    monkeypatch.setenv("HERMES_COURIER_EXTERNAL_BASE_URL", "http://insecure.example.ts.net")
    monkeypatch.setenv("HERMES_COURIER_BEARER_TOKEN", "tok")
    monkeypatch.setenv("HERMES_COURIER_ENABLE", "1")
    snap = courier_pairing_deployment_snapshot()
    assert snap["tailscaleProfileReady"] is False
    assert any("https" in w.lower() for w in snap["pairingWarnings"])


def test_token_configured_local_mode_when_external_missing(monkeypatch):
    monkeypatch.delenv("HERMES_COURIER_EXTERNAL_BASE_URL", raising=False)
    monkeypatch.setenv("HERMES_COURIER_BEARER_TOKEN", "tok")
    monkeypatch.setenv("HERMES_COURIER_ENABLE", "1")
    snap = courier_pairing_deployment_snapshot()
    assert snap["pairingUrlMode"] == "local"
    assert snap["tailscaleProfileReady"] is False
    assert any("EXTERNAL" in w or "tailnet" in w.lower() for w in snap["pairingWarnings"])


def test_enrollment_gateway_still_wins_over_external(monkeypatch):
    monkeypatch.setenv("HERMES_COURIER_EXTERNAL_BASE_URL", "https://from-env.ts.net")
    r = resolve_courier_gateway_for_pairing(
        {
            "gatewayUrl": "https://from-device.example/gw",
            "deviceId": "d",
            "publicKeyFingerprint": "a",
            "appVersion": "1",
            "issuedAt": "2026-01-01T00:00:00Z",
        }
    )
    assert r["gatewayUrl"] == "https://from-device.example/gw"
    assert r["gatewayUrlSource"] == "enrollment"


# ── Phase-1 library endpoints (/v1/skills, /v1/memory, /v1/cron, /v1/logs) ──
#
# These routes now return real, truthful data sourced from existing WebUI /
# hermes-agent internals (skills_tool, cron.jobs, ~/.hermes/memories,
# ~/.hermes/logs) rather than the legacy 404 that the Android client used to
# treat as `UnavailablePayload`. Tests accept either a list-of-items response
# or an explicit `UnavailablePayload` so they continue to pass on hosts where
# the backing module really is missing (e.g. hermes-agent not installed).


def _is_unavailable_payload(payload):
    return (
        isinstance(payload, dict)
        and payload.get("supported") is False
        and str(payload.get("type", "")).endswith("_unavailable")
    )


def test_courier_library_requires_bearer(base_url):
    for path in ("/v1/skills", "/v1/memory", "/v1/cron", "/v1/logs"):
        code, payload = _request(base_url, "GET", path)
        assert code == 401, f"{path} must require bearer auth"
        assert payload["supported"] is False


def test_courier_skills_returns_real_or_unavailable(base_url):
    code, payload = _request(base_url, "GET", "/v1/skills", bearer=COURIER_TOKEN)
    assert code == 200
    if _is_unavailable_payload(payload):
        assert payload["type"] == "skills_unavailable"
        assert payload.get("endpoint") == "/v1/skills"
        return
    assert isinstance(payload, list)
    for item in payload:
        assert {"skillId", "name", "enabled"} <= set(item.keys())
        assert isinstance(item["skillId"], str) and item["skillId"]
        assert isinstance(item["name"], str) and item["name"]
        assert isinstance(item["enabled"], bool)
        assert isinstance(item.get("description", ""), str)
        assert isinstance(item.get("scopes", []), list)


def test_courier_memory_returns_real_or_unavailable(base_url):
    code, payload = _request(base_url, "GET", "/v1/memory", bearer=COURIER_TOKEN)
    assert code == 200
    if _is_unavailable_payload(payload):
        assert payload["type"] == "memory_unavailable"
        return
    assert isinstance(payload, list)
    for item in payload:
        assert {"memoryId", "title", "updatedAt"} <= set(item.keys())
        assert isinstance(item["memoryId"], str) and item["memoryId"]
        assert isinstance(item["title"], str)
        assert isinstance(item.get("tags", []), list)
        assert isinstance(item.get("pinned", False), bool)


def test_courier_cron_returns_real_or_unavailable(base_url):
    code, payload = _request(base_url, "GET", "/v1/cron", bearer=COURIER_TOKEN)
    assert code == 200
    if _is_unavailable_payload(payload):
        assert payload["type"] == "cron_unavailable"
        return
    assert isinstance(payload, list)
    for item in payload:
        assert {"cronId", "name", "schedule", "enabled"} <= set(item.keys())
        assert isinstance(item["cronId"], str) and item["cronId"]
        assert isinstance(item["enabled"], bool)
        assert isinstance(item["schedule"], str)


def test_courier_logs_returns_real_or_unavailable(base_url):
    code, payload = _request(base_url, "GET", "/v1/logs?limit=25", bearer=COURIER_TOKEN)
    assert code == 200
    if _is_unavailable_payload(payload):
        assert payload["type"] == "logs_unavailable"
        return
    assert isinstance(payload, list)
    assert len(payload) <= 25
    for item in payload:
        assert {"logId", "severity", "timestamp", "message"} <= set(item.keys())
        assert item["severity"] in {"debug", "info", "warn", "error"}
        assert isinstance(item["message"], str)


def test_courier_logs_severity_filter_rejects_other_levels(base_url):
    code, payload = _request(
        base_url, "GET", "/v1/logs?limit=50&severity=error", bearer=COURIER_TOKEN
    )
    assert code == 200
    if _is_unavailable_payload(payload):
        return
    assert isinstance(payload, list)
    for item in payload:
        assert item["severity"] == "error"


def test_courier_library_unavailable_payload_shape_is_android_compatible():
    """Regression guard: the shape emitted for genuinely unavailable subsystems
    MUST be parseable by the Android client's `JSONObject.toUnavailableOrNull`.
    That helper requires `supported == false` and a `type` ending in
    `_unavailable`, and optionally parses `endpoint` and
    `fallbackPollEndpoints`. This avoids re-introducing the old pre-Phase-1
    404 behaviour that showed up in the app as a broken red capability card.
    """
    from api.courier_library import _unavailable

    payload = _unavailable("skills", "demo", "/v1/skills", ["/v1/dashboard"])
    assert payload["supported"] is False
    assert payload["type"].endswith("_unavailable")
    assert payload["endpoint"] == "/v1/skills"
    assert payload["fallbackPollEndpoints"] == ["/v1/dashboard"]


def test_courier_approval_decision_response_has_stable_shape(base_url, cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    urllib.request.urlopen(
        urllib.request.Request(
            base_url + f"/api/approval/inject_test?session_id={sid}&pattern_key=test_key&command=echo+hello",
            method="GET",
        ),
        timeout=10,
    ).read()
    _, approvals = _request(base_url, "GET", "/v1/approvals", bearer=COURIER_TOKEN)
    approval_id = approvals[0]["approvalId"]
    code, decision = _request(
        base_url,
        "POST",
        f"/v1/approvals/{approval_id}/decision",
        body={"decision": "deny"},
        bearer=COURIER_TOKEN,
    )
    assert code == 200
    assert decision["approvalId"] == approval_id
    assert decision["action"] == "deny"
    assert decision["status"] == "ok"
    assert decision["ok"] is True
