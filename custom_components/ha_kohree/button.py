"""Button entities for Kohree lock MVP connection controls."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, DOMAIN
from .coordinator import KohreeCoordinator


def _device_info(address: str, device_name: str) -> DeviceInfo:
    """Build Home Assistant device metadata for a Kohree lock."""
    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        name=device_name or f"Kohree {address}",
        manufacturer="Kohree",
        model="BLE Lock",
        connections={("bluetooth", address)},
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Kohree button entities."""
    coordinator: KohreeCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]
    device_name = entry.data.get(CONF_DEVICE_NAME, "")

    async_add_entities(
        [
            KohreeReconnectButton(coordinator, address, device_name),
            KohreeDisconnectButton(coordinator, address, device_name),
        ]
    )


class KohreeReconnectButton(CoordinatorEntity[KohreeCoordinator], ButtonEntity):
    """Force reconnect button for MVP testing."""

    _attr_has_entity_name = True
    _attr_name = "Reconnect"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: KohreeCoordinator,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_reconnect"
        self._attr_device_info = _device_info(address, device_name)

    async def async_press(self) -> None:
        await self.coordinator.async_reconnect()


class KohreeDisconnectButton(CoordinatorEntity[KohreeCoordinator], ButtonEntity):
    """Force disconnect button to release lock for app testing."""

    _attr_has_entity_name = True
    _attr_name = "Disconnect"
    _attr_icon = "mdi:bluetooth-off"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: KohreeCoordinator,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_disconnect"
        self._attr_device_info = _device_info(address, device_name)

    async def async_press(self) -> None:
        await self.coordinator.async_stop()
