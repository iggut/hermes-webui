import json
import time
import urllib.error
import urllib.request

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
