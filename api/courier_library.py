"""
Phase-1 read-only Courier library endpoints for Hermes WebUI.

Implements GET /v1/skills, /v1/memory, /v1/cron, /v1/logs by adapting
existing WebUI / hermes-agent internals to the Courier wire shapes defined
in shared/contract/hermes-courier-api.yaml.

Each helper returns either:
- a list[dict] conforming to the matching schema (Skill / MemoryItem /
  CronJob / LogEntry), including an empty list when the source is genuinely
  empty, or
- a dict matching UnavailablePayload (``supported: False`` + ``type:
  <name>_unavailable``) when a backing subsystem cannot be reached at all.

The Android client's `parseCapabilityListing` accepts both shapes, so this
module never needs to guess; it just returns truthful data.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)


# ── Shared helpers ───────────────────────────────────────────────────────────


def _iso_from_mtime(value) -> str:
    if value is None:
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc).isoformat()
    except Exception:
        return ""


def _iso_from_any(value) -> str:
    """Best-effort coercion of job timestamps (int/float/ISO string) to ISO."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return _iso_from_mtime(value)
    text = str(value).strip()
    if not text:
        return ""
    try:
        dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.isoformat()
    except Exception:
        return text


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "\u2026"


def _active_hermes_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home())
    except Exception:
        return Path(
            os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
        ).expanduser()


def _unavailable(name: str, detail: str, endpoint: str, fallbacks: list[str] | None = None) -> dict:
    payload = {
        "type": f"{name}_unavailable",
        "detail": detail,
        "endpoint": endpoint,
        "supported": False,
    }
    if fallbacks:
        payload["fallbackPollEndpoints"] = list(fallbacks)
    return payload


# ── /v1/skills ───────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", text or "").strip("-").lower()
    return text or "skill"


def _skill_id(name: str, category: str | None) -> str:
    base = name if not category else f"{category}/{name}"
    return _slug(base)


def courier_skills_response() -> list[dict] | dict:
    """Return Courier-shaped skill list.

    Source: ``tools.skills_tool.skills_list`` (shipped with hermes-agent).
    Falls back to ``UnavailablePayload`` only when the agent skills module
    cannot be imported at all — an empty ``skills/`` directory still returns
    ``[]``.
    """
    try:
        from tools.skills_tool import skills_list as _skills_list
    except Exception as exc:
        logger.debug("skills module unavailable: %s", exc)
        return _unavailable(
            "skills",
            f"hermes-agent tools.skills_tool unavailable: {exc}",
            "/v1/skills",
        )

    try:
        raw = _skills_list()
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception as exc:
        logger.debug("skills_list() failed: %s", exc)
        return _unavailable(
            "skills",
            f"skills_list() raised: {exc}",
            "/v1/skills",
        )

    items: list[dict] = []
    for entry in data.get("skills") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        description = _truncate(str(entry.get("description") or ""), 512)
        category = str(entry.get("category") or "").strip()
        skill: dict[str, Any] = {
            "skillId": _skill_id(name, category or None),
            "name": name,
            "description": description,
            "enabled": True,
            "scopes": [category] if category else [],
        }
        version = str(entry.get("version") or "").strip()
        if version:
            skill["version"] = version
        items.append(skill)
    items.sort(key=lambda s: (s.get("scopes", [""])[0] if s.get("scopes") else "", s["name"].lower()))
    return items


# ── /v1/memory ───────────────────────────────────────────────────────────────


_MEMORY_SEP = "\u00a7"  # matches the `§` item separator written to MEMORY.md


def _parse_memory_file(path: Path, source_label: str, mtime_iso: str) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.debug("failed reading %s: %s", path, exc)
        return []

    items: list[dict] = []
    # Split on the Hermes `§` record separator; fall back to whole-file when
    # the file has no separator at all.
    chunks = [c.strip() for c in raw.split(_MEMORY_SEP)] if _MEMORY_SEP in raw else [raw.strip()]
    for idx, chunk in enumerate(chunks):
        if not chunk:
            continue
        title_line = chunk.splitlines()[0].strip()
        # Drop common markdown markers from the displayed title.
        title = re.sub(r"^\*+|\*+$", "", title_line).strip()
        title = title.lstrip("#").strip() or title_line or source_label.title()
        # A stable per-chunk id so the Android client can key list updates.
        digest = hashlib.sha1(f"{source_label}:{idx}:{chunk}".encode("utf-8")).hexdigest()[:12]
        items.append(
            {
                "memoryId": f"{source_label}:{digest}",
                "title": _truncate(title, 140),
                "snippet": _truncate(chunk, 400),
                "body": chunk,
                "tags": [source_label],
                "updatedAt": mtime_iso,
                "pinned": False,
            }
        )
    return items


def courier_memory_response() -> list[dict] | dict:
    """Return Courier-shaped memory items sourced from MEMORY.md + USER.md."""
    try:
        from api.helpers import _redact_value  # redact credentials from bodies
    except Exception:
        _redact_value = lambda v: v  # noqa: E731 - best-effort fallback

    home = _active_hermes_home()
    mem_dir = home / "memories"
    mem_file = mem_dir / "MEMORY.md"
    user_file = mem_dir / "USER.md"

    if not mem_file.exists() and not user_file.exists():
        # Memory directory exists as part of a profile but neither canonical
        # file is present — this is a legitimate empty state, not a gateway
        # gap, so return an empty list rather than an unavailable payload.
        return []

    items: list[dict] = []
    for path, label in ((mem_file, "memory"), (user_file, "user")):
        if not path.exists():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        items.extend(_parse_memory_file(path, label, _iso_from_mtime(mtime)))

    items.sort(key=lambda m: m["updatedAt"], reverse=True)
    return _redact_value(items)


# ── /v1/cron ─────────────────────────────────────────────────────────────────


def courier_cron_response() -> list[dict] | dict:
    """Return Courier-shaped cron list."""
    try:
        from cron.jobs import list_jobs as _list_jobs
    except Exception as exc:
        logger.debug("cron module unavailable: %s", exc)
        return _unavailable(
            "cron",
            f"hermes-agent cron.jobs unavailable: {exc}",
            "/v1/cron",
        )

    try:
        jobs = _list_jobs(include_disabled=True) or []
    except Exception as exc:
        logger.debug("list_jobs() failed: %s", exc)
        return _unavailable(
            "cron",
            f"list_jobs() raised: {exc}",
            "/v1/cron",
        )

    items: list[dict] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        schedule = job.get("schedule_display") or ""
        if not schedule:
            schedule_obj = job.get("schedule") or {}
            if isinstance(schedule_obj, dict):
                schedule = (
                    schedule_obj.get("display")
                    or schedule_obj.get("expr")
                    or schedule_obj.get("kind")
                    or ""
                )
            elif isinstance(schedule_obj, str):
                schedule = schedule_obj
        out: dict[str, Any] = {
            "cronId": job_id,
            "name": str(job.get("name") or "Unnamed job"),
            "schedule": str(schedule or ""),
            "enabled": bool(job.get("enabled", True)) and job.get("state") != "paused",
            "description": _truncate(str(job.get("prompt") or job.get("description") or ""), 400),
        }
        nxt = _iso_from_any(job.get("next_run_at"))
        if nxt:
            out["nextRunAt"] = nxt
        last = _iso_from_any(job.get("last_run_at"))
        if last:
            out["lastRunAt"] = last
        last_status = str(job.get("last_status") or "").strip()
        if last_status:
            out["lastStatus"] = last_status
        items.append(out)
    items.sort(key=lambda j: j.get("nextRunAt") or j.get("lastRunAt") or "", reverse=True)
    return items


# ── /v1/logs ─────────────────────────────────────────────────────────────────


_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)"
    r"\s+(?P<level>DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)"
    r"\s+(?P<body>.*)$",
    re.IGNORECASE,
)


def _normalize_severity(level: str) -> str:
    level = (level or "").strip().lower()
    if level in {"warn", "warning"}:
        return "warn"
    if level in {"critical", "fatal"}:
        return "error"
    if level in {"debug", "info", "error"}:
        return level
    return "info"


def _tail_lines(path: Path, max_lines: int, max_bytes: int = 256 * 1024) -> list[str]:
    """Return up to ``max_lines`` trailing lines from ``path`` without loading
    the whole file. Reads only the final ``max_bytes`` of the file."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            raw = f.read()
    except Exception as exc:
        logger.debug("tail failed for %s: %s", path, exc)
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        # Drop the first partial line that may have been split mid-record.
        lines = lines[1:]
    return lines[-max_lines:]


def _log_entry_from_line(
    line: str,
    *,
    source: str,
    file_mtime_iso: str,
    index: int,
) -> dict | None:
    line = line.rstrip()
    if not line:
        return None
    match = _LOG_LINE_RE.match(line)
    if match:
        ts_raw = match.group("ts").replace(",", ".")
        timestamp = _iso_from_any(ts_raw) or file_mtime_iso
        severity = _normalize_severity(match.group("level"))
        message = match.group("body").strip()
    else:
        timestamp = file_mtime_iso
        severity = "info"
        message = line.strip()
    if not message:
        return None
    digest = hashlib.sha1(
        f"{source}:{index}:{timestamp}:{message}".encode("utf-8")
    ).hexdigest()[:12]
    return {
        "logId": f"{source}:{digest}",
        "severity": severity,
        "timestamp": timestamp,
        "message": _truncate(message, 800),
        "source": source,
    }


def _parse_log_query(query: str) -> tuple[int, str | None]:
    qs = parse_qs(query or "")
    raw_limit = (qs.get("limit", [""])[0] or "").strip()
    try:
        limit = int(raw_limit) if raw_limit else 100
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    severity = (qs.get("severity", [""])[0] or "").strip().lower() or None
    if severity:
        severity = _normalize_severity(severity)
    return limit, severity


def courier_logs_response(query: str = "") -> list[dict] | dict:
    """Return Courier-shaped log entries, tailed from ~/.hermes/logs/*.log.

    Honours ``?limit=N`` (1..500, default 100) and ``?severity=debug|info|warn|error``.
    """
    limit, severity_filter = _parse_log_query(query)
    home = _active_hermes_home()
    log_dir = home / "logs"
    if not log_dir.exists():
        return []

    try:
        log_files = [p for p in log_dir.glob("*.log") if p.is_file()]
    except Exception as exc:
        logger.debug("log dir scan failed: %s", exc)
        return _unavailable(
            "logs",
            f"log directory scan failed: {exc}",
            "/v1/logs",
        )
    if not log_files:
        return []

    # Pull up to 2x the final limit from each file so a severity filter has
    # enough slack to still return `limit` entries.
    per_file = max(limit * 2 // max(1, len(log_files)), 50)
    per_file = min(per_file, 500)

    collected: list[dict] = []
    try:
        from api.helpers import _redact_text
    except Exception:
        _redact_text = lambda v: v  # noqa: E731

    for path in log_files:
        try:
            mtime_iso = _iso_from_mtime(path.stat().st_mtime)
        except OSError:
            mtime_iso = ""
        source = path.name
        for idx, line in enumerate(_tail_lines(path, per_file)):
            entry = _log_entry_from_line(
                line, source=source, file_mtime_iso=mtime_iso, index=idx
            )
            if not entry:
                continue
            if severity_filter and entry["severity"] != severity_filter:
                continue
            entry["message"] = _redact_text(entry["message"])
            collected.append(entry)

    collected.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return collected[:limit]


# ── /v1/models ───────────────────────────────────────────────────────────────


def courier_models_response() -> list[dict] | dict:
    """Return Courier-shaped model list sourced from api.config.get_available_models()."""
    try:
        from api.config import get_available_models
    except Exception as exc:
        logger.debug("models module unavailable: %s", exc)
        return _unavailable(
            "models",
            f"api.config.get_available_models unavailable: {exc}",
            "/v1/models",
        )

    try:
        data = get_available_models()
        # get_available_models returns a dict like {'groups': [{'provider': str, 'models': [...]}]}
        groups = data.get("groups") or []
        raw_items = []
        for g in groups:
            raw_items.extend(g.get("models") or [])
    except Exception as exc:
        logger.debug("get_available_models() failed: %s", exc)
        return _unavailable(
            "models",
            f"get_available_models() raised: {exc}",
            "/v1/models",
        )

    items: list[dict] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id") or entry.get("modelId")
        if not mid:
            continue
        # Use existing label or name as the Courier 'name'
        name = entry.get("label") or entry.get("name") or mid
        items.append(
            {
                "id": str(mid),
                "name": str(name),
                "capability": "conversation",
            }
        )

    return {"items": items}


# ── Dispatch ─────────────────────────────────────────────────────────────────


def handle_courier_library_get(handler, parsed) -> bool:
    """Dispatch the four Phase-1 read-only library endpoints.

    Returns True when the path matched; False otherwise.
    """
    from api.helpers import j  # local import to avoid circular load

    path = parsed.path
    if path == "/v1/skills":
        j(handler, courier_skills_response())
        return True
    if path == "/v1/memory":
        j(handler, courier_memory_response())
        return True
    if path == "/v1/cron":
        j(handler, courier_cron_response())
        return True
    if path == "/v1/logs":
        j(handler, courier_logs_response(parsed.query or ""))
        return True
    if path == "/v1/models":
        j(handler, courier_models_response())
        return True
    return False
