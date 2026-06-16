"""Diagnostics for Kohree lock MVP integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import DOMAIN, TUYA_CLOUD_BOOTSTRAP_KEYS
from .coordinator import KohreeCoordinator

TO_REDACT = {CONF_ADDRESS, *TUYA_CLOUD_BOOTSTRAP_KEYS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics data for a Kohree config entry."""
    coordinator: KohreeCoordinator = hass.data[DOMAIN][entry.entry_id]

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "runtime": {
            "connected": coordinator.connected,
            "notify_enabled": coordinator.notify_enabled,
            "rssi": coordinator.rssi,
            "last_seen_age_seconds": coordinator.last_seen_age,
            "last_decoded_cmd": coordinator.last_decoded_cmd,
            "challenge_stage_seen": coordinator.challenge_stage_seen,
            "last_packet_hex": coordinator.last_packet_hex,
            "last_write_hex": coordinator.last_write_hex,
            "last_response_hex": coordinator.last_response_hex,
            "profile_name": coordinator.profile_name,
            "last_error": coordinator.last_error,
        },
    }
