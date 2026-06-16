"""Config flow for Kohree BLE lock integration (MVP)."""

from __future__ import annotations

import logging
import re
from string import hexdigits
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DEVICE_NAME,
    CONF_ONBOARDING_MODE,
    CONF_POLL_INTERVAL_MINUTES,
    DEFAULT_POLL_INTERVAL_MINUTES,
    MAX_POLL_INTERVAL_MINUTES,
    MIN_POLL_INTERVAL_MINUTES,
    CONF_TUYA_DEV_ID,
    CONF_TUYA_LOCAL_KEY,
    CONF_TUYA_PRODUCT_ID,
    CONF_TUYA_SEC_KEY,
    CONF_TUYA_USER_CODE,
    CONF_TUYA_UUID,
    CONF_UNLOCK_PASSCODE,
    DOMAIN,
    KNOWN_TUYA_LOCK_PIDS,
    KOHREE_NAME_PREFIX,
    KOHREE_SERVICE_UUIDS,
    ONBOARDING_MODE_CLOUD,
    TUYA_BLE_SERVICE_UUID,
    TUYA_CLOUD_BOOTSTRAP_KEYS,
    TUYA_LOCK_BONDED_PREFIX,
    TUYA_LOCK_MANUFACTURER_ID,
)
from .tuya_cloud import TuyaCloudAuthError, async_get_qr_code, async_login_and_get_devices

_LOGGER = logging.getLogger(__name__)


def _normalize_address(address: str) -> str:
    """Normalize user-provided Bluetooth address when possible."""
    value = address.strip().upper()
    collapsed = value.replace(":", "")
    if len(collapsed) == 12 and all(c in hexdigits for c in collapsed):
        return ":".join(collapsed[i : i + 2] for i in range(0, 12, 2))
    return value


def _poll_interval_schema(interval_default: int) -> dict:
    """Shared voluptuous field for the poll interval (onboarding + options)."""
    return {
        vol.Required(
            CONF_POLL_INTERVAL_MINUTES, default=interval_default
        ): NumberSelector(
            NumberSelectorConfig(
                min=MIN_POLL_INTERVAL_MINUTES,
                max=MAX_POLL_INTERVAL_MINUTES,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="min",
            )
        ),
    }


def _is_mac_address(value: str) -> bool:
    """Return True when the value matches canonical Bluetooth MAC format."""
    return bool(re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value))


def _is_kohree_device(info: BluetoothServiceInfoBleak) -> bool:
    """Return True when the BLE advertisement matches Kohree signatures."""
    name = (info.name or "").upper()
    if name.startswith(KOHREE_NAME_PREFIX):
        return True

    service_uuids = {_normalize_uuid(uuid) for uuid in info.service_uuids}
    if any(_normalize_uuid(uuid) in service_uuids for uuid in KOHREE_SERVICE_UUIDS):
        return True

    # Tuya app appears to infer category from PID in fd50 service-data.
    # Mirror that behavior safely by allowing only known Kohree lock PID values.
    pid = _extract_tuya_pid(info)
    if pid in KNOWN_TUYA_LOCK_PIDS:
        return True

    # Some paired locks advertise encrypted bonded data (0x49...) instead of PID.
    if _is_tuya_bonded_kohree_signature(info):
        return True

    # Some packets can omit service-data but retain Tuya manufacturer and AFxx name.
    return _is_tuya_manufacturer_name_signature(info)


def _normalize_uuid(value: str) -> str:
    """Normalize UUID values so 16-bit and 128-bit forms compare correctly."""
    uuid = value.lower()
    if len(uuid) == 4 and all(c in hexdigits for c in uuid):
        return f"0000{uuid}-0000-1000-8000-00805f9b34fb"
    return uuid


def _get_tuya_service_payload(info: BluetoothServiceInfoBleak) -> bytes | None:
    """Return raw fd50 service-data payload when present."""
    for uuid, raw in info.service_data.items():
        if _normalize_uuid(uuid) != TUYA_BLE_SERVICE_UUID:
            continue
        return bytes(raw)
    return None


def _extract_tuya_pid(info: BluetoothServiceInfoBleak) -> str | None:
    """Extract Tuya product ID from fd50 service-data when available."""
    payload = _get_tuya_service_payload(info)
    if payload is None or len(payload) < 5:
        return None
    if payload[0] != 0x41:
        return None

    pid = payload[4:].decode("ascii", errors="ignore").strip("\x00").lower()
    return pid or None


def _is_tuya_bonded_kohree_signature(info: BluetoothServiceInfoBleak) -> bool:
    """Detect paired Tuya lock advertising pattern seen on Kohree units."""
    payload = _get_tuya_service_payload(info)
    if payload is None or len(payload) < 9:
        return False

    if payload[0] != TUYA_LOCK_BONDED_PREFIX:
        return False

    if payload[1:4] != b"\x00\x00\x08":
        return False

    if TUYA_LOCK_MANUFACTURER_ID not in info.manufacturer_data:
        return False

    name = (info.name or "").upper()
    return bool(re.fullmatch(r"[A-F0-9]{4}\x00?", name))


def _is_tuya_manufacturer_name_signature(info: BluetoothServiceInfoBleak) -> bool:
    """Fallback for sparse Tuya advertisements without fd50 service-data."""
    if TUYA_LOCK_MANUFACTURER_ID not in info.manufacturer_data:
        return False

    name = (info.name or "").upper()
    return bool(re.fullmatch(r"[A-F0-9]{4}\x00?", name))


class KohreeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Kohree BLE locks."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return KohreeOptionsFlow()

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._onboarding_data: dict[str, Any] = {}
        self._cloud_qr_code: str = ""
        self._cloud_user_code: str = ""
        self._cloud_devices: dict[str, dict[str, str]] = {}

    def _build_entry_data(self, address: str, device_name: str) -> dict[str, Any]:
        """Build the config-entry payload (cloud-assisted onboarding only)."""
        data: dict[str, Any] = {
            CONF_ADDRESS: address,
            CONF_DEVICE_NAME: device_name,
            CONF_ONBOARDING_MODE: ONBOARDING_MODE_CLOUD,
            CONF_POLL_INTERVAL_MINUTES: self._onboarding_data.get(
                CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
            ),
        }
        for key in TUYA_CLOUD_BOOTSTRAP_KEYS:
            value = self._onboarding_data.get(key)
            if value:
                data[key] = value
        return data

    def _apply_cloud_device(self, device: dict[str, str]) -> None:
        """Persist one-time Tuya bootstrap fields from live cloud lookup."""
        self._onboarding_data.update(
            {
                CONF_ONBOARDING_MODE: ONBOARDING_MODE_CLOUD,
                CONF_TUYA_DEV_ID: device.get("id", "").strip(),
                CONF_TUYA_LOCAL_KEY: device.get("local_key", "").strip(),
                CONF_TUYA_UUID: device.get("uuid", "").strip(),
                CONF_TUYA_SEC_KEY: device.get("sec_key", "").strip(),
                CONF_TUYA_PRODUCT_ID: device.get("product_id", "").strip(),
                CONF_UNLOCK_PASSCODE: device.get("unlock_passcode", "").strip(),
            }
        )

        cloud_name = device.get("name", "").strip()
        if cloud_name and not self._onboarding_data.get(CONF_DEVICE_NAME):
            self._onboarding_data[CONF_DEVICE_NAME] = cloud_name

        cloud_address = _normalize_address(device.get("mac", ""))
        discovered_address = self._onboarding_data.get(CONF_ADDRESS, "")
        if _is_mac_address(cloud_address) and not discovered_address:
            self._onboarding_data[CONF_ADDRESS] = cloud_address

    def _select_cloud_device(self) -> dict[str, str] | None:
        """Attempt deterministic auto-selection when cloud login returns many devices."""
        candidates = list(self._cloud_devices.values())
        if not candidates:
            return None

        target_address = self._onboarding_data.get(CONF_ADDRESS, "")
        if target_address:
            address_matches = [
                dev
                for dev in candidates
                if _is_mac_address(_normalize_address(dev.get("mac", "")))
                and _normalize_address(dev.get("mac", "")) == target_address
            ]
            if len(address_matches) == 1:
                return address_matches[0]
            if address_matches:
                candidates = address_matches

        if self._discovery_info is not None:
            discovered_pid = _extract_tuya_pid(self._discovery_info)
            if discovered_pid:
                pid_matches = [
                    dev
                    for dev in candidates
                    if dev.get("product_id", "").strip().lower() == discovered_pid
                ]
                if len(pid_matches) == 1:
                    return pid_matches[0]
                if pid_matches:
                    candidates = pid_matches

        if len(candidates) == 1:
            return candidates[0]
        return None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle BLE auto-discovery."""
        if not _is_kohree_device(discovery_info):
            return self.async_abort(reason="not_supported")

        address = _normalize_address(discovery_info.address)
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or address,
        }

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered lock and choose onboarding mode."""
        if self._discovery_info is None:
            return self.async_abort(reason="no_device")

        address = _normalize_address(self._discovery_info.address)
        device_name = self._discovery_info.name or ""

        if user_input is not None:
            self._onboarding_data = {
                CONF_ONBOARDING_MODE: ONBOARDING_MODE_CLOUD,
                CONF_ADDRESS: address,
                CONF_DEVICE_NAME: device_name,
            }
            return await self.async_step_cloud_live()

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": device_name or "Kohree Lock",
                "address": address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Begin cloud-assisted onboarding (QR login)."""
        self._onboarding_data = {CONF_ONBOARDING_MODE: ONBOARDING_MODE_CLOUD}
        return await self.async_step_cloud_live()

    async def async_step_cloud_live(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start live Tuya cloud auth by generating QR login token."""
        errors: dict[str, str] = {}
        error_text = ""
        default_user_code = self._onboarding_data.get(CONF_TUYA_USER_CODE, "")

        if user_input is not None:
            user_code = user_input[CONF_TUYA_USER_CODE].strip()
            if not user_code:
                errors["base"] = "invalid_user_code"
            else:
                self._onboarding_data[CONF_TUYA_USER_CODE] = user_code
                try:
                    self._cloud_qr_code = await async_get_qr_code(self.hass, user_code)
                except TuyaCloudAuthError as err:
                    errors["base"] = err.error_key
                    error_text = err.message
                    _LOGGER.warning("Tuya QR init failed: %s", err.message)
                else:
                    self._cloud_user_code = user_code
                    return await self.async_step_scan()

        return self.async_show_form(
            step_id="cloud_live",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TUYA_USER_CODE, default=default_user_code): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "name": self._onboarding_data.get(CONF_DEVICE_NAME, "") or "Kohree Lock",
                "error": error_text,
            },
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display QR for Tuya app login, then import cloud bootstrap fields."""
        errors: dict[str, str] = {}
        error_text = ""

        if not self._cloud_qr_code or not self._cloud_user_code:
            return self.async_abort(reason="no_device")

        if user_input is not None:
            try:
                cloud_devices = await async_login_and_get_devices(
                    self.hass,
                    self._cloud_user_code,
                    self._cloud_qr_code,
                )
            except TuyaCloudAuthError as err:
                errors["base"] = err.error_key
                error_text = err.message
                _LOGGER.warning("Tuya cloud login failed: %s", err.message)
            else:
                self._cloud_devices = {}
                for device in cloud_devices:
                    dev_id = device.get("id", "").strip()
                    local_key = device.get("local_key", "").strip()
                    if not dev_id or not local_key:
                        continue
                    self._cloud_devices[dev_id] = device

                if not self._cloud_devices:
                    errors["base"] = "no_cloud_devices"
                else:
                    selected = self._select_cloud_device()
                    if selected is not None:
                        self._apply_cloud_device(selected)
                        return await self.async_step_local()
                    return await self.async_step_choose_cloud_device()

        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr"): QrCodeSelector(
                        config=QrCodeSelectorConfig(
                            data=f"tuyaSmart--qrLogin?token={self._cloud_qr_code}",
                            scale=5,
                            error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "name": self._onboarding_data.get(CONF_DEVICE_NAME, "") or "Kohree Lock",
                "error": error_text,
            },
        )

    async def async_step_choose_cloud_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose one cloud device when login account has multiple candidates."""
        errors: dict[str, str] = {}

        if not self._cloud_devices:
            return self.async_abort(reason="no_device")

        if user_input is not None:
            selected = self._cloud_devices.get(user_input["device_id"])
            if selected is None:
                errors["base"] = "no_cloud_devices"
            else:
                self._apply_cloud_device(selected)
                return await self.async_step_local()

        options: list[SelectOptionDict] = []
        for dev_id, device in self._cloud_devices.items():
            name = device.get("name") or dev_id
            product = device.get("product_name") or device.get("product_id")
            mac = _normalize_address(device.get("mac", ""))
            if not _is_mac_address(mac):
                mac = ""
            details = ", ".join(part for part in (product, mac) if part)
            label = f"{name} ({details})" if details else name
            options.append(SelectOptionDict(value=dev_id, label=label))

        options.sort(key=lambda item: str(item["label"]).lower())

        return self.async_show_form(
            step_id="choose_cloud_device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "name": self._onboarding_data.get(CONF_DEVICE_NAME, "") or "Kohree Lock",
            },
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finalize local BLE control settings."""
        # Build a discovered-device cache for optional title enrichment only.
        # In proxy-only installs, names/service UUIDs can be incomplete, so
        # setup should never depend on discovery filters.
        self._discovered_devices = {}
        for info in async_discovered_service_info(self.hass):
            normalized = _normalize_address(info.address)
            if normalized in self._discovered_devices:
                continue
            self._discovered_devices[normalized] = info

        if self._discovery_info is not None:
            discovered_address = _normalize_address(self._discovery_info.address)
            self._discovered_devices.setdefault(discovered_address, self._discovery_info)

        if user_input is not None:
            address = _normalize_address(user_input[CONF_ADDRESS])
            if self.unique_id is None:
                await self.async_set_unique_id(address)
            elif self.unique_id != address:
                return self.async_abort(reason="address_mismatch")
            self._abort_if_unique_id_configured()

            info = self._discovered_devices.get(address)
            device_name = user_input.get(CONF_DEVICE_NAME, "").strip()
            if not device_name:
                device_name = (info.name if info else "") or self._onboarding_data.get(
                    CONF_DEVICE_NAME,
                    "",
                )

            self._onboarding_data[CONF_POLL_INTERVAL_MINUTES] = int(
                user_input.get(
                    CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
                )
            )

            return self.async_create_entry(
                title=device_name or f"Kohree {address}",
                data=self._build_entry_data(address, device_name),
            )

        suggested_address = self._onboarding_data.get(CONF_ADDRESS, "")
        suggested_name = self._onboarding_data.get(CONF_DEVICE_NAME, "")
        if not suggested_address and self._discovery_info is not None:
            suggested_address = _normalize_address(self._discovery_info.address)
        if not suggested_name and self._discovery_info is not None:
            suggested_name = self._discovery_info.name or ""

        address_key = (
            vol.Required(CONF_ADDRESS, default=suggested_address)
            if suggested_address
            else vol.Required(CONF_ADDRESS)
        )
        name_key = (
            vol.Optional(CONF_DEVICE_NAME, default=suggested_name)
            if suggested_name
            else vol.Optional(CONF_DEVICE_NAME)
        )

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    address_key: str,
                    name_key: str,
                    **_poll_interval_schema(DEFAULT_POLL_INTERVAL_MINUTES),
                }
            ),
            description_placeholders={
                "name": suggested_name or "Kohree Lock",
            },
        )


class KohreeOptionsFlow(OptionsFlow):
    """Options flow: poll interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the poll interval (minutes)."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_POLL_INTERVAL_MINUTES: int(
                        user_input[CONF_POLL_INTERVAL_MINUTES]
                    ),
                },
            )

        interval_default = self.config_entry.options.get(
            CONF_POLL_INTERVAL_MINUTES,
            self.config_entry.data.get(
                CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
            ),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(_poll_interval_schema(interval_default)),
        )
