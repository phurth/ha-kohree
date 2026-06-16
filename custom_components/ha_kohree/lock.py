"""Lock entity for the Kohree BLE lock."""

from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, DOMAIN
from .coordinator import KohreeCoordinator


def _device_info(address: str, device_name: str) -> DeviceInfo:
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
    """Set up the Kohree lock entity."""
    coordinator: KohreeCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_ADDRESS]
    device_name = entry.data.get(CONF_DEVICE_NAME, "")
    async_add_entities([KohreeLock(coordinator, address, device_name)])


class KohreeLock(CoordinatorEntity[KohreeCoordinator], LockEntity):
    """Kohree deadbolt over Tuya BLE."""

    _attr_has_entity_name = True
    _attr_name = None  # use the device name
    # True bolt state can't be read on demand and manual turns aren't reported,
    # so state is best-effort (commands + electronic-actuation pushes).
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: KohreeCoordinator,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        mac = address.replace(":", "").lower()
        self._attr_unique_id = f"{mac}_lock"
        self._attr_device_info = _device_info(address, device_name)

    @property
    def is_locked(self) -> bool | None:
        """Return motor lock state, or None if unknown."""
        return self.coordinator.lock_state

    async def async_lock(self, **kwargs) -> None:
        await self.coordinator.async_lock()

    async def async_unlock(self, **kwargs) -> None:
        await self.coordinator.async_unlock()
