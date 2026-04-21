"""
Hermes Courier compatibility routes for Hermes WebUI.

Implements a minimal /v1 gateway surface for mobile clients.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from api.config import STREAMS, STREAMS_LOCK
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


def _run_sync_turn(session_id: str, message: str, timeout_seconds: float = 8.0):
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


def handle_courier_get(handler, parsed) -> bool:
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
        return j(
            handler,
            {
                "type": "events_unavailable",
                "detail": "WebSocket realtime is not implemented on Hermes WebUI /v1/events yet.",
                "supported": False,
                "endpoint": "/v1/events",
            },
            status=426,
        )

    return False


def handle_courier_post(handler, parsed, body) -> bool:
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
        try:
            done_session = _run_sync_turn(sid, body_text)
        except Exception as exc:
            return j(
                handler,
                {
                    "eventId": f"{sid}:{int(time.time())}:unsupported",
                    "author": "Hermes",
                    "body": f"Conversation send unsupported in current runtime: {exc}",
                    "timestamp": _iso_timestamp(time.time()),
                },
            )
        messages = done_session.get("messages") or []
        latest_assistant = ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                latest_assistant = _extract_text(msg)
                if latest_assistant:
                    break
        return j(
            handler,
            {
                "eventId": f"{sid}:{int(time.time())}",
                "author": "Hermes",
                "body": latest_assistant or "Message accepted.",
                "timestamp": _iso_timestamp(time.time()),
            },
        )

    return False
