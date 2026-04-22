"""
Hermes Courier compatibility routes for Hermes WebUI.

Implements a minimal /v1 gateway surface for mobile clients.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from api.config import STREAMS, STREAMS_LOCK
from api.courier_events import handle_courier_events_get
from api.courier_library import handle_courier_library_get
from api.courier_pairing import courier_pairing_deployment_snapshot
from api.helpers import bad, j
from api.models import all_sessions, get_session, new_session
from api.streaming import _run_agent_streaming
from api.streaming import _get_ai_agent

try:
    from tools.approval import _pending, _lock
except Exception:  # pragma: no cover
    _pending = {}
    _lock = threading.Lock()


def _iso_timestamp(value) -> str:
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _courier_enabled() -> bool:
    token = os.getenv("HERMES_COURIER_BEARER_TOKEN", "").strip()
    if not token:
        return False
    flag = os.getenv("HERMES_COURIER_ENABLE", "").strip().lower()
    if flag in ("", "1", "true", "yes", "on"):
        return True
    return False


def courier_runtime_status() -> dict:
    token = os.getenv("HERMES_COURIER_BEARER_TOKEN", "").strip()
    enabled = _courier_enabled()
    runtime_issue = _courier_runtime_unavailable_reason()
    issues = []
    if not token:
        issues.append("HERMES_COURIER_BEARER_TOKEN is not set.")
    if not enabled:
        issues.append("HERMES_COURIER_ENABLE is disabled.")
    if runtime_issue:
        issues.append(f"Agent runtime unavailable: {runtime_issue}")
    token_pairing_ready = bool(token and enabled)
    pairing_mode = "token-only" if token_pairing_ready else "unavailable"
    deploy = courier_pairing_deployment_snapshot()
    return {
        "endpoint": "/v1",
        "auth": {
            "mode": "bearer-token",
            "bearerTokenConfigured": bool(token),
            "courierEnabled": enabled,
        },
        "pairing": {
            "tokenBackedPairingAvailable": token_pairing_ready,
            "pairingMode": pairing_mode,
            "pairingContractVersion": "2026-04-21",
            "qrPairingAvailable": token_pairing_ready,
            "postScanBootstrapAvailable": False,
            "unavailableReasons": issues,
            "defaultPairingGatewayUrl": deploy["defaultPairingGatewayUrl"],
            "gatewayUrlSource": deploy["gatewayUrlSource"],
            "externalBaseUrlConfigured": deploy["externalBaseUrlConfigured"],
            "externalBaseUrl": deploy["externalBaseUrl"],
            "legacyGatewayEnvConfigured": deploy["legacyGatewayEnvConfigured"],
            "defaultUsesLocalLoopback": deploy["defaultUsesLocalLoopback"],
            "tailscaleProfileReady": deploy["tailscaleProfileReady"],
            "pairingUrlMode": deploy["pairingUrlMode"],
            "pairingWarnings": deploy["pairingWarnings"],
        },
        "runtime": {
            "available": runtime_issue is None,
            "detail": runtime_issue or "",
        },
        "issues": issues,
    }


def _auth_error(handler, detail: str, status: int = 401):
    return j(
        handler,
        {
            "error": "Courier API unavailable",
            "detail": detail,
            "supported": False,
            "endpoint": "/v1",
        },
        status=status,
    )


def validate_courier_auth(handler):
    token = os.getenv("HERMES_COURIER_BEARER_TOKEN", "").strip()
    if not token:
        return _auth_error(
            handler,
            "Set HERMES_COURIER_BEARER_TOKEN to enable /v1 routes.",
            status=503,
        )
    if not _courier_enabled():
        return _auth_error(
            handler,
            "Courier API disabled. Set HERMES_COURIER_ENABLE=1 to enable.",
            status=503,
        )
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return _auth_error(handler, "Missing bearer token.", status=401)
    supplied = auth.removeprefix("Bearer ").strip()
    if supplied != token:
        return _auth_error(handler, "Invalid bearer token.", status=403)
    return None


def _session_status(session_obj, compact) -> str:
    if compact.get("archived"):
        return "archived"
    if getattr(session_obj, "active_stream_id", None):
        with STREAMS_LOCK:
            if session_obj.active_stream_id in STREAMS:
                return "running"
    return "idle"


def _session_summary(compact: dict) -> dict:
    session_id = compact.get("session_id", "")
    session_obj = None
    try:
        session_obj = get_session(session_id)
    except Exception:
        session_obj = None
    return {
        "sessionId": session_id,
        "title": compact.get("title") or "Untitled",
        "status": _session_status(session_obj, compact),
        "updatedAt": _iso_timestamp(compact.get("updated_at")),
    }


def _resolve_target_session_id(query: str = "") -> str | None:
    sid = ""
    if query:
        qs = parse_qs(query)
        sid = (qs.get("sessionId", [""])[0] or qs.get("session_id", [""])[0]).strip()
    if sid:
        return sid
    sessions = all_sessions()
    if not sessions:
        return None
    return sessions[0].get("session_id")


def _extract_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content or "").strip()


def _conversation_events_for_session(session_id: str, limit: int = 40) -> list[dict]:
    try:
        s = get_session(session_id)
    except KeyError:
        return []
    events = []
    for idx, msg in enumerate(s.messages[-limit:]):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant", "system"):
            continue
        body = _extract_text(msg)
        if not body:
            continue
        events.append(
            {
                "eventId": f"{session_id}:{idx}",
                "author": "You" if role == "user" else ("Hermes" if role == "assistant" else role),
                "body": body,
                "timestamp": _iso_timestamp(msg.get("timestamp") or msg.get("_ts") or s.updated_at),
            }
        )
    return events


def _conversation_send_payload(
    session_id: str,
    *,
    status: str,
    body: str,
    supported: bool,
    detail: str = "",
) -> dict:
    payload = {
        "eventId": f"{session_id}:{int(time.time())}:{status}",
        "author": "Hermes",
        "body": body,
        "timestamp": _iso_timestamp(time.time()),
        "status": status,
        "supported": supported,
    }
    if detail:
        payload["detail"] = detail
    return payload


def _list_pending_approvals() -> list[dict]:
    out = []
    with _lock:
        for sid, queue_val in dict(_pending).items():
            items = queue_val if isinstance(queue_val, list) else [queue_val]
            for item in items:
                if not isinstance(item, dict):
                    continue
                aid = item.get("approval_id") or uuid.uuid4().hex
                title = item.get("description") or "Approval required"
                command = item.get("command") or ""
                out.append(
                    {
                        "approvalId": aid,
                        "title": str(title),
                        "detail": str(command),
                        "requiresBiometrics": False,
                        "_session_id": sid,
                    }
                )
    return out


def _run_sync_turn(session_id: str, message: str, timeout_seconds: float = 3.0):
    if _get_ai_agent() is None:
        raise RuntimeError("agent runtime unavailable")
    s = get_session(session_id)
    stream_id = uuid.uuid4().hex
    q = queue.Queue()
    with STREAMS_LOCK:
        STREAMS[stream_id] = q
    s.active_stream_id = stream_id
    s.pending_user_message = message
    s.pending_attachments = []
    s.pending_started_at = time.time()
    s.save()
    thr = threading.Thread(
        target=_run_agent_streaming,
        args=(s.session_id, message, s.model, s.workspace, stream_id, []),
        daemon=True,
    )
    thr.start()
    done_session = None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            event, data = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if event == "done":
            done_session = data.get("session", {})
        if event in ("stream_end", "done", "error", "cancel"):
            break
    if not done_session:
        raise RuntimeError("Conversation turn did not complete in time")
    return done_session


def _courier_runtime_unavailable_reason() -> str | None:
    if _get_ai_agent() is None:
        return "agent runtime unavailable"
    try:
        from api.config import get_effective_default_model, resolve_model_provider
        from hermes_cli.runtime_provider import resolve_runtime_provider

        default_model = get_effective_default_model()
        _, requested_provider, _ = resolve_model_provider(default_model)
        runtime = resolve_runtime_provider(requested=requested_provider)
        if not isinstance(runtime, dict):
            return "runtime provider unresolved"
        api_key = str(runtime.get("api_key") or "").strip()
        acp_cmd = str(runtime.get("command") or "").strip()
        if not api_key and not acp_cmd:
            return "runtime credentials unavailable"
        if acp_cmd:
            cmd_bin = acp_cmd.split()[0]
            resolved = shutil.which(cmd_bin)
            if resolved is None and not (Path(cmd_bin).exists() and os.access(cmd_bin, os.X_OK)):
                return f"runtime command unavailable: {cmd_bin}"
    except Exception as exc:
        return f"runtime bootstrap unavailable: {exc}"
    return None


def handle_courier_get(handler, parsed) -> bool:
    if parsed.path == "/v1/status":
        return j(handler, courier_runtime_status())

    if parsed.path == "/v1/dashboard":
        sessions = all_sessions()
        pending = _list_pending_approvals()
        connected = "connected"
        return j(
            handler,
            {
                "activeSessionCount": len(sessions),
                "pendingApprovalCount": len(pending),
                "lastSyncLabel": _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
                "connectionState": connected,
            },
        )

    if parsed.path == "/v1/sessions":
        return j(handler, [_session_summary(s) for s in all_sessions()])

    if parsed.path.startswith("/v1/sessions/"):
        rest = parsed.path.removeprefix("/v1/sessions/")
        if "/" not in rest:
            sid = rest
            try:
                s = get_session(sid)
            except KeyError:
                return bad(handler, "Session not found", 404)
            compact = s.compact()
            return j(handler, _session_summary(compact))

    if parsed.path == "/v1/approvals":
        items = _list_pending_approvals()
        return j(
            handler,
            [
                {
                    "approvalId": x["approvalId"],
                    "title": x["title"],
                    "detail": x["detail"],
                    "requiresBiometrics": x["requiresBiometrics"],
                }
                for x in items
            ],
        )

    if parsed.path == "/v1/conversation":
        sid = _resolve_target_session_id(parsed.query)
        if not sid:
            return j(handler, [])
        return j(handler, _conversation_events_for_session(sid))

    if parsed.path == "/v1/events":
        return handle_courier_events_get(handler)

    if handle_courier_library_get(handler, parsed):
        return True

    return False


def _courier_approval_seed_enabled() -> bool:
    """Seed route is OFF unless the operator explicitly opts in.

    We intentionally keep this gated behind its own env flag so a paired
    mobile app cannot inject pending approvals against a production
    deployment without the operator knowing about it.
    """
    raw = os.getenv("HERMES_COURIER_ENABLE_APPROVAL_SEED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _handle_courier_approval_seed(handler, body: dict):
    """Inject a disposable pending approval so mobile clients can exercise the
    approvals list and the `POST /v1/approvals/{id}/decision` path end to end.

    The seeded entry is indistinguishable from a real pending approval except
    for the ``_debug: True`` marker, and is removed from ``_pending`` the
    moment any caller posts a decision against it (same cleanup path as
    real approvals). No agent is waiting on it, so the decision is a no-op
    other than recording the outcome in the approval subsystem.
    """
    if not _courier_enabled():
        return _auth_error(
            handler,
            "Courier API disabled. Set HERMES_COURIER_ENABLE=1 to enable.",
            status=503,
        )
    if not _courier_approval_seed_enabled():
        return j(
            handler,
            {
                "error": "Approval seed disabled",
                "detail": (
                    "Set HERMES_COURIER_ENABLE_APPROVAL_SEED=1 to allow "
                    "disposable approval creation through /v1/approvals/_debug_seed."
                ),
                "supported": False,
                "endpoint": "/v1/approvals/_debug_seed",
            },
            status=403,
        )

    sid = str(body.get("sessionId") or body.get("session_id") or "").strip()
    if not sid:
        sid = _resolve_target_session_id("")
    if not sid:
        sessions = all_sessions()
        sid = sessions[0]["session_id"] if sessions else new_session().session_id

    title = str(body.get("title") or "Courier debug approval").strip() or "Courier debug approval"
    command = str(body.get("command") or "echo hermes-courier-debug-approval").strip()
    pattern_key = str(body.get("patternKey") or body.get("pattern_key") or "courier_debug").strip() or "courier_debug"

    from api.routes import submit_pending as _submit_pending  # local import avoids circular load
    approval_id = uuid.uuid4().hex
    _submit_pending(
        sid,
        {
            "approval_id": approval_id,
            "command": command,
            "pattern_key": pattern_key,
            "pattern_keys": [pattern_key],
            "description": title,
            "_debug": True,
        },
    )
    return j(
        handler,
        {
            "ok": True,
            "approvalId": approval_id,
            "sessionId": sid,
            "title": title,
            "detail": command,
            "requiresBiometrics": False,
            "supported": True,
            "endpoint": "/v1/approvals/_debug_seed",
        },
    )


def handle_courier_post(handler, parsed, body) -> bool:
    if parsed.path == "/v1/approvals/_debug_seed":
        return _handle_courier_approval_seed(handler, body)

    if parsed.path.startswith("/v1/sessions/") and parsed.path.endswith("/actions"):
        parts = parsed.path.split("/")
        sid = parts[3] if len(parts) > 3 else ""
        action = str(body.get("action") or "").strip().lower() or "unknown"
        return j(
            handler,
            {
                "sessionId": sid,
                "action": action,
                "status": "unsupported",
                "detail": "Session-control actions are not mapped in Hermes WebUI yet.",
                "updatedAt": _iso_timestamp(time.time()),
                "supported": False,
                "endpoint": parsed.path,
            },
        )

    if parsed.path.startswith("/v1/sessions/"):
        parts = parsed.path.split("/")
        if len(parts) == 5:
            sid = parts[3]
            action = str(parts[4] or "").strip().lower()
            return j(
                handler,
                {
                    "sessionId": sid,
                    "action": action or "unknown",
                    "status": "unsupported",
                    "detail": "Session-control actions are not mapped in Hermes WebUI yet.",
                    "updatedAt": _iso_timestamp(time.time()),
                    "supported": False,
                    "endpoint": parsed.path,
                },
            )

    if parsed.path.startswith("/v1/approvals/") and parsed.path.endswith("/decision"):
        approval_id = parsed.path.split("/")[3]
        decision = str(body.get("decision") or "").strip().lower()
        if decision not in ("approve", "deny"):
            return bad(handler, "decision must be approve or deny", 400)
        approvals = _list_pending_approvals()
        target = next((a for a in approvals if a["approvalId"] == approval_id), None)
        if not target:
            return bad(handler, "Approval not found", 404)
        from api.routes import _handle_approval_respond
        mapped_choice = "session" if decision == "approve" else "deny"
        return _handle_approval_respond(
            handler,
            {
                "session_id": target["_session_id"],
                "approval_id": approval_id,
                "choice": mapped_choice,
            },
        )

    if parsed.path == "/v1/conversation":
        body_text = str(body.get("body") or "").strip()
        if not body_text:
            return bad(handler, "body is required", 400)
        sid = str(body.get("sessionId") or "").strip() or _resolve_target_session_id("")
        if not sid:
            sessions = all_sessions()
            if sessions:
                sid = sessions[0]["session_id"]
            else:
                sid = new_session().session_id
        runtime_unavailable = _courier_runtime_unavailable_reason()
        if runtime_unavailable:
            return j(
                handler,
                _conversation_send_payload(
                    sid,
                    status="unsupported",
                    body=f"Conversation send unsupported in current runtime: {runtime_unavailable}",
                    supported=False,
                    detail=runtime_unavailable,
                ),
            )
        try:
            timeout_seconds = float(os.getenv("HERMES_COURIER_CONVERSATION_TIMEOUT_SECONDS", "15").strip() or "15")
            timeout_seconds = max(3.0, min(timeout_seconds, 60.0))
            done_session = _run_sync_turn(sid, body_text, timeout_seconds=timeout_seconds)
        except Exception as exc:
            return j(
                handler,
                _conversation_send_payload(
                    sid,
                    status="unsupported",
                    body=f"Conversation send unsupported in current runtime: {exc}",
                    supported=False,
                    detail=str(exc),
                ),
            )
        messages = done_session.get("messages") or []
        latest_assistant = ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                latest_assistant = _extract_text(msg)
                if latest_assistant:
                    break
        if not latest_assistant:
            return j(
                handler,
                _conversation_send_payload(
                    sid,
                    status="unsupported",
                    body="Conversation send unsupported in current runtime: no assistant response was produced.",
                    supported=False,
                    detail="no assistant response was produced",
                ),
            )
        return j(
            handler,
            _conversation_send_payload(
                sid,
                status="ok",
                body=latest_assistant,
                supported=True,
            ),
        )

    return False
