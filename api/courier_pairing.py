"""
Helpers for Hermes Courier pairing payload parsing/generation.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlencode, urlparse

import qrcode
from qrcode.image.svg import SvgImage


ENROLLMENT_SCHEME = "hermes-courier-enroll"
ENROLLMENT_HOST = "gateway"
REQUIRED_FIELDS = (
    "gatewayUrl",
    "deviceId",
    "publicKeyFingerprint",
    "appVersion",
    "issuedAt",
)


def _as_trimmed_str(payload: dict, key: str) -> str:
    return str(payload.get(key, "")).strip()


def parse_enrollment_payload(raw_payload: str) -> dict:
    raw = str(raw_payload or "").strip()
    if not raw:
        raise ValueError("Enrollment payload is required")

    parsed = _parse_enrollment_uri(raw) or _parse_enrollment_json(raw)
    if parsed is None:
        raise ValueError("Unsupported enrollment payload format")

    errors = []
    for field in REQUIRED_FIELDS:
        if not _as_trimmed_str(parsed, field):
            errors.append(f"Missing required field: {field}")
    if errors:
        raise ValueError("; ".join(errors))
    if "://" not in parsed["gatewayUrl"]:
        raise ValueError("gatewayUrl must include a URL scheme (http/https)")

    return {field: _as_trimmed_str(parsed, field) for field in REQUIRED_FIELDS}


def _parse_enrollment_uri(raw: str) -> dict | None:
    uri = urlparse(raw)
    if uri.scheme != ENROLLMENT_SCHEME:
        return None
    values = {k: (v[0] if v else "") for k, v in parse_qs(uri.query).items()}
    return {
        "gatewayUrl": values.get("gatewayUrl", ""),
        "deviceId": values.get("deviceId", ""),
        "publicKeyFingerprint": values.get("publicKeyFingerprint", ""),
        "appVersion": values.get("appVersion", ""),
        "issuedAt": values.get("issuedAt", ""),
    }


def _parse_enrollment_json(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return {k: obj.get(k, "") for k in REQUIRED_FIELDS}


def _build_pairing_qr_data_url(pairing_uri: str) -> str:
    """Return a data URL containing an SVG QR code for the pairing URI."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(pairing_uri)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return f"data:image/svg+xml;charset=UTF-8,{quote(svg)}"


def build_pairing_payload(enrollment_payload: dict | None = None, include_bearer: bool = True) -> dict:
    enrollment = parse_enrollment_payload(json.dumps(enrollment_payload)) if isinstance(enrollment_payload, dict) else None
    gateway_url = (enrollment or {}).get("gatewayUrl") or os.getenv("HERMES_COURIER_GATEWAY_URL", "").strip()
    if not gateway_url:
        gateway_url = "http://127.0.0.1:8787"

    bearer_token = os.getenv("HERMES_COURIER_BEARER_TOKEN", "").strip()
    token_included = bool(include_bearer and bearer_token)
    bearer_available = bool(bearer_token)
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    query = {
        "gatewayUrl": gateway_url,
        "deviceId": (enrollment or {}).get("deviceId", ""),
        "publicKeyFingerprint": (enrollment or {}).get("publicKeyFingerprint", ""),
        "appVersion": (enrollment or {}).get("appVersion", ""),
        "issuedAt": now_iso,
        "courierMode": "bearer-token",
    }
    if token_included:
        query["bearerToken"] = bearer_token
        query["token"] = bearer_token

    pairing_uri = f"{ENROLLMENT_SCHEME}://{ENROLLMENT_HOST}?{urlencode(query)}"
    result = {
        "pairingUri": pairing_uri,
        "pairingPayload": query,
        "tokenIncluded": token_included,
        "pairingQrDataUrl": _build_pairing_qr_data_url(pairing_uri),
    }
    if include_bearer and not token_included:
        result["warning"] = "Bearer token is not configured in WebUI environment."
    result["bearerTokenConfigured"] = bearer_available
    result["tokenBackedPairingAvailable"] = bearer_available
    return result
