"""Minimal Tuya cloud helpers for one-time bootstrap credential import."""

from __future__ import annotations

import base64
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Tuya Local uses this public Home Assistant client identifier/schema for
# one-time QR login and account-scoped device listing.
TUYA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
TUYA_SCHEMA = "haauthorize"


class TuyaCloudAuthError(Exception):
    """Raised when cloud bootstrap auth cannot be completed."""

    def __init__(self, error_key: str, message: str = "") -> None:
        super().__init__(message)
        self.error_key = error_key
        self.message = message


async def async_get_qr_code(hass: HomeAssistant, user_code: str) -> str:
    """Generate a Tuya QR login token for the given user code."""
    return await hass.async_add_executor_job(_get_qr_code, user_code)


async def async_login_and_get_devices(
    hass: HomeAssistant,
    user_code: str,
    qr_code: str,
) -> list[dict[str, str]]:
    """Complete login and return cloud devices with local bootstrap fields."""
    return await hass.async_add_executor_job(_login_and_get_devices, user_code, qr_code)


def _get_qr_code(user_code: str) -> str:
    """Blocking worker for QR token retrieval."""
    login_control = _new_login_control()

    response = login_control.qr_code(TUYA_CLIENT_ID, TUYA_SCHEMA, user_code)
    if not response.get("success", False):
        message = str(response.get("msg") or "Failed to create Tuya login QR code")
        raise TuyaCloudAuthError("qr_code_error", message)

    result = response.get("result") or {}
    qr_code = str(result.get("qrcode") or "").strip()
    if not qr_code:
        raise TuyaCloudAuthError("qr_code_error", "Tuya cloud did not return a QR token")
    return qr_code


def _login_and_get_devices(user_code: str, qr_code: str) -> list[dict[str, str]]:
    """Blocking worker for QR login and device cache retrieval."""
    login_control = _new_login_control()

    success, info = login_control.login_result(qr_code, TUYA_CLIENT_ID, user_code)
    if not success:
        message = "Tuya login failed"
        if isinstance(info, dict):
            message = str(info.get("msg") or message)
        raise TuyaCloudAuthError("login_error", message)

    if not isinstance(info, dict):
        raise TuyaCloudAuthError("login_error", "Tuya login returned invalid response")

    terminal_id = str(info.get("terminal_id") or "").strip()
    endpoint = str(info.get("endpoint") or "").strip()
    if not terminal_id or not endpoint:
        raise TuyaCloudAuthError("login_error", "Tuya login response missing endpoint data")

    token_info = {
        "t": info.get("t"),
        "uid": info.get("uid"),
        "expire_time": info.get("expire_time"),
        "access_token": info.get("access_token"),
        "refresh_token": info.get("refresh_token"),
    }

    manager = _new_manager(user_code, terminal_id, endpoint, token_info)
    manager.update_device_cache()

    devices: list[dict[str, str]] = []
    for device in manager.device_map.values():
        local_key = _safe_str(getattr(device, "local_key", ""))
        if not local_key:
            continue

        devices.append(
            {
                "id": _safe_str(getattr(device, "id", "")),
                "name": _safe_str(getattr(device, "name", "")),
                "product_id": _safe_str(getattr(device, "product_id", "")),
                "product_name": _safe_str(getattr(device, "product_name", "")),
                "local_key": local_key,
                "uuid": _safe_str(getattr(device, "uuid", "")),
                "sec_key": _safe_str(getattr(device, "sec_key", "")),
                "unlock_passcode": _extract_unlock_passcode(getattr(device, "status", None)),
                "mac": _safe_str(getattr(device, "mac", "")),
            }
        )

    if not devices:
        raise TuyaCloudAuthError(
            "no_cloud_devices",
            "No cloud devices with local keys were returned for this account",
        )

    _LOGGER.debug("Tuya cloud login returned %s candidate devices", len(devices))
    return devices


def _extract_unlock_passcode(status: Any) -> str:
    """Extract the BLE unlock passcode from the device's ble_unlock_check DP.

    The Raw DP value is base64 of: ff ff 00 01 <8 ascii digits> 01 <ts> 00 00.
    Returns the 8-digit passcode, or "" if unavailable.
    """
    value: Any = None
    if isinstance(status, dict):
        value = status.get("ble_unlock_check")
    elif isinstance(status, list):
        for item in status:
            if isinstance(item, dict) and item.get("code") == "ble_unlock_check":
                value = item.get("value")
                break
    if not isinstance(value, str) or not value:
        return ""
    try:
        raw = base64.b64decode(value)
    except Exception:  # noqa: BLE001
        return ""
    if len(raw) >= 12 and raw[0:4] == b"\xff\xff\x00\x01" and raw[4:12].isdigit():
        return raw[4:12].decode("ascii")
    return ""


def _new_login_control() -> Any:
    """Create LoginControl lazily so import errors map to config-flow errors."""
    try:
        from tuya_sharing import LoginControl
    except ImportError as err:  # pragma: no cover - dependency managed by HA
        raise TuyaCloudAuthError("cloud_auth_unavailable", str(err)) from err
    return LoginControl()


def _new_manager(
    user_code: str,
    terminal_id: str,
    endpoint: str,
    token_info: dict[str, Any],
) -> Any:
    """Create Tuya manager lazily for token-scoped cloud operations."""
    try:
        from tuya_sharing import Manager, SharingTokenListener
    except ImportError as err:  # pragma: no cover - dependency managed by HA
        raise TuyaCloudAuthError("cloud_auth_unavailable", str(err)) from err

    class _TokenListener(SharingTokenListener):
        def update_token(self, _token_info: dict[str, Any]) -> None:
            return

    return Manager(
        TUYA_CLIENT_ID,
        user_code,
        terminal_id,
        endpoint,
        token_info,
        _TokenListener(),
    )


def _safe_str(value: Any) -> str:
    """Normalize optional API fields to strings."""
    return str(value).strip() if value is not None else ""
