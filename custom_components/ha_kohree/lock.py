"""Lock entity for the Kohree BLE lock."""

from __future__ import annotations

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
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


class KohreeLock(CoordinatorEntity[KohreeCoordinator], RestoreEntity, LockEntity):
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
        self._restored_is_locked: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last-known lock state after a restart.

        The bolt can't be read on demand and manual turns aren't reported, so
        after a reboot ``coordinator.lock_state`` is None until the next poll or
        actuation. Fall back to the restored state in the meantime so the entity
        shows its last value instead of ``unknown``.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        # Lock entity state strings ("locked"/"unlocked") — used as literals to
        # stay independent of homeassistant.const, which no longer exports the
        # STATE_LOCKED / STATE_UNLOCKED constants.
        if last is not None and last.state in ("locked", "unlocked"):
            self._restored_is_locked = last.state == "locked"

    @property
    def is_locked(self) -> bool | None:
        """Live motor lock state; restored last-known value until the first poll."""
        state = self.coordinator.lock_state
        if state is not None:
            return state
        return self._restored_is_locked

    async def async_lock(self, **kwargs) -> None:
        await self.coordinator.async_lock()

    async def async_unlock(self, **kwargs) -> None:
        await self.coordinator.async_unlock()
