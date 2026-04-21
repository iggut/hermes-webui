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
DEFAULT_LOCAL_GATEWAY_URL = "http://127.0.0.1:8787"
ENV_COURIER_EXTERNAL_BASE_URL = "HERMES_COURIER_EXTERNAL_BASE_URL"
ENV_COURIER_GATEWAY_URL = "HERMES_COURIER_GATEWAY_URL"
ENV_COURIER_PRODUCTION = "HERMES_COURIER_PRODUCTION"
REQUIRED_FIELDS = (
    "gatewayUrl",
    "deviceId",
    "publicKeyFingerprint",
    "appVersion",
    "issuedAt",
)
PAIRING_CONTRACT_VERSION = "2026-04-21"
PAIRING_MODE = "token-only"


def _normalized_base_url(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    return s.rstrip("/")


def _is_production_profile() -> bool:
    return os.getenv(ENV_COURIER_PRODUCTION, "").strip().lower() in ("1", "true", "yes", "on")


def _is_loopback_gateway(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return True
    return False


def _external_url_validation_messages(url: str) -> list[str]:
    """Return human-readable issues for HERMES_COURIER_EXTERNAL_BASE_URL (non-empty)."""
    issues: list[str] = []
    if not url:
        return issues
    try:
        parsed = urlparse(url)
    except Exception:
        issues.append("HERMES_COURIER_EXTERNAL_BASE_URL is not a valid URL.")
        return issues
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        issues.append(
            "HERMES_COURIER_EXTERNAL_BASE_URL should use https (Tailscale Serve uses HTTPS on *.ts.net)."
        )
    if _is_loopback_gateway(url):
        issues.append(
            "HERMES_COURIER_EXTERNAL_BASE_URL points at loopback; Android clients on other devices cannot reach it."
        )
    return issues


def resolve_courier_gateway_for_pairing(enrollment: dict | None) -> dict:
    """
    Pick gatewayUrl for pairing QR / payload.

    Precedence: enrollment gatewayUrl > HERMES_COURIER_EXTERNAL_BASE_URL >
    HERMES_COURIER_GATEWAY_URL > default local URL.
    """
    enr = enrollment if isinstance(enrollment, dict) else None
    from_enrollment = _as_trimmed_str(enr, "gatewayUrl") if enr else ""
    external = _normalized_base_url(os.getenv(ENV_COURIER_EXTERNAL_BASE_URL, ""))
    legacy = _normalized_base_url(os.getenv(ENV_COURIER_GATEWAY_URL, ""))

    url_resolution_warnings: list[str] = []
    if from_enrollment:
        gateway_url = from_enrollment
        source = "enrollment"
    elif external:
        gateway_url = external
        source = "external"
        url_resolution_warnings.extend(_external_url_validation_messages(external))
    elif legacy:
        gateway_url = legacy
        source = "legacy_env"
        if _is_production_profile() and _is_loopback_gateway(legacy):
            url_resolution_warnings.append(
                "HERMES_COURIER_GATEWAY_URL is loopback while HERMES_COURIER_PRODUCTION=1; "
                "set HERMES_COURIER_EXTERNAL_BASE_URL to your tailnet https URL."
            )
    else:
        gateway_url = DEFAULT_LOCAL_GATEWAY_URL
        source = "default_local"
        if _is_production_profile():
            url_resolution_warnings.append(
                "HERMES_COURIER_EXTERNAL_BASE_URL is not set while HERMES_COURIER_PRODUCTION=1; "
                "pairing QR uses local loopback (fine for same-device testing only)."
            )

    if (
        _is_production_profile()
        and source == "external"
        and _is_loopback_gateway(gateway_url)
    ):
        url_resolution_warnings.append(
            "HERMES_COURIER_PRODUCTION=1 but external base URL looks like loopback; "
            "expected a tailnet https URL for multi-device pairing."
        )

    if "://" not in gateway_url:
        # Defensive; callers should not hit this
        gateway_url = DEFAULT_LOCAL_GATEWAY_URL
        url_resolution_warnings.append("Resolved gatewayUrl was invalid; fell back to default local URL.")

    return {
        "gatewayUrl": gateway_url,
        "gatewayUrlSource": source,
        "urlResolutionWarnings": url_resolution_warnings,
        "externalBaseUrlConfigured": bool(external),
        "externalBaseUrl": external,
        "legacyGatewayEnvConfigured": bool(legacy),
        "defaultUsesLocalLoopback": source == "default_local",
    }


def courier_pairing_deployment_snapshot() -> dict:
    """
    Static deployment / URL readiness for operators and /v1/status (no enrollment context).
    """
    resolved = resolve_courier_gateway_for_pairing(None)
    ext = resolved["externalBaseUrl"]
    pairing_warnings = list(resolved["urlResolutionWarnings"])
    token = os.getenv("HERMES_COURIER_BEARER_TOKEN", "").strip()
    enabled_flag = os.getenv("HERMES_COURIER_ENABLE", "").strip().lower()
    courier_on = bool(token) and enabled_flag in ("", "1", "true", "yes", "on")

    ext_msgs = _external_url_validation_messages(ext) if ext else []
    https_ok = bool(ext and urlparse(ext).scheme.lower() == "https")
    non_loopback = bool(ext and not _is_loopback_gateway(ext))
    tailnet_profile_ready = bool(
        courier_on and ext and https_ok and non_loopback and not ext_msgs
    )

    if courier_on and token and not ext:
        pairing_warnings.append(
            "Tailscale-ready pairing is not fully configured: set HERMES_COURIER_EXTERNAL_BASE_URL "
            "to your private https://<machine>.<tailnet>.ts.net URL (from tailscale serve)."
        )

    if ext and not tailnet_profile_ready and courier_on:
        for m in ext_msgs:
            if m not in pairing_warnings:
                pairing_warnings.append(m)

    mode = "unavailable"
    if not token or not courier_on:
        mode = "unavailable"
    elif tailnet_profile_ready:
        mode = "tailnet"
    else:
        mode = "local"

    return {
        "defaultPairingGatewayUrl": resolved["gatewayUrl"],
        "gatewayUrlSource": resolved["gatewayUrlSource"],
        "externalBaseUrlConfigured": resolved["externalBaseUrlConfigured"],
        "externalBaseUrl": resolved["externalBaseUrl"],
        "legacyGatewayEnvConfigured": resolved["legacyGatewayEnvConfigured"],
        "defaultUsesLocalLoopback": resolved["defaultUsesLocalLoopback"],
        "tailscaleProfileReady": tailnet_profile_ready,
        "pairingUrlMode": mode,
        "pairingWarnings": pairing_warnings,
    }


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
    enrollment = (
        parse_enrollment_payload(json.dumps(enrollment_payload))
        if isinstance(enrollment_payload, dict)
        else None
    )
    resolved = resolve_courier_gateway_for_pairing(enrollment)
    gateway_url = resolved["gatewayUrl"]

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
        "pairingMode": PAIRING_MODE,
        "pairingContractVersion": PAIRING_CONTRACT_VERSION,
        "apiBasePath": "/v1",
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
        "pairingMode": PAIRING_MODE,
        "pairingContractVersion": PAIRING_CONTRACT_VERSION,
        "postScanBootstrapSupported": False,
        "gatewayUrlSource": resolved["gatewayUrlSource"],
    }
    payload_warnings = [w for w in resolved["urlResolutionWarnings"] if w]
    if include_bearer and not token_included:
        result["warning"] = "Bearer token is not configured in WebUI environment."
    if payload_warnings:
        result["pairingWarnings"] = payload_warnings
        # Backward-compatible single string for older clients
        result["pairingWarning"] = payload_warnings[0]
    result["bearerTokenConfigured"] = bearer_available
    result["tokenBackedPairingAvailable"] = bearer_available
    return result
