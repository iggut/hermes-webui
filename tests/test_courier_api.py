import json
import time
import urllib.error
import urllib.request

from api.courier_pairing import (
    build_pairing_payload,
    courier_pairing_deployment_snapshot,
    resolve_courier_gateway_for_pairing,
)
from api.courier_routes import courier_runtime_status
from tests.conftest import make_session_tracked


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
    make_session_tracked(cleanup_test_sessions)
    code, payload = _request(base_url, "GET", "/v1/conversation", bearer=COURIER_TOKEN)
    assert code == 200
    assert isinstance(payload, list)

    code, events = _request(base_url, "GET", "/v1/events", bearer=COURIER_TOKEN)
    assert code == 426
    assert events["supported"] is False
    assert events["retryable"] is True
    assert events["fallbackPollEndpoints"] == ["/v1/dashboard", "/v1/approvals", "/v1/conversation"]


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
