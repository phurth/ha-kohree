"""Kohree BLE lock integration (MVP)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import KohreeCoordinator

PLATFORMS: list[str] = ["binary_sensor", "button", "lock", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kohree from a config entry."""
    coordinator = KohreeCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Reload when options (e.g. poll interval) change so the new cadence applies.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start BLE connect after entities subscribe to coordinator updates so
    # reconnect polling is scheduled correctly.
    entry.async_create_background_task(
        hass,
        coordinator.async_start(),
        "kohree_initial_connect",
    )
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Kohree config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: KohreeCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
