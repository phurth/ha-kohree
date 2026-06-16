"""BLE coordinator for Kohree lock MVP discovery and connection checks."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from bleak import BleakClient, BleakError, BleakGATTCharacteristic
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_DEVICE_NAME,
    CONF_TUYA_DEV_ID,
    CONF_TUYA_LOCAL_KEY,
    CONF_TUYA_SEC_KEY,
    CONF_TUYA_UUID,
    CONF_UNLOCK_PASSCODE,
    COMMAND_SETTLE_SECONDS,
    CONF_POLL_INTERVAL_MINUTES,
    CONNECT_SETTLE_DELAY_SECONDS,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DEFAULT_UNLOCK_PASSCODE,
    DP_BATTERY,
    DP_BLE_CHECK,
    DP_BLE_UNLOCK,
    DP_LOCK,
    DP_MOTOR_STATE,
    DP_UNLOCK_BLE_COUNT,
    KOHREE_RW_CHAR_UUID,
    KOHREE_SERVICE_UUID,
    STATUS_SYNC_QUERY_BYTE,
    TUYA_BLE_SERVICE_UUID,
    TUYA_NOTIFY_CHAR_UUID,
    TUYA_READ_CHAR_UUID,
    TUYA_WRITE_CHAR_UUID,
)
from .protocol import (
    DP_TYPE_BOOL,
    DP_TYPE_RAW,
    FUN_RECEIVE_DP,
    FUN_RECEIVE_DP_REPORT,
    FUN_RECEIVE_TIME1_REQ,
    FUN_RECEIVE_TIME2_REQ,
    FUN_SENDER_DEVICE_INFO,
    FUN_SENDER_DEVICE_STATUS,
    FUN_SENDER_DP,
    FUN_SENDER_PAIR,
    PacketAssembler,
    build_ble_check_value,
    build_ble_unlock_value,
    build_device_info_request,
    build_dp_command,
    build_pairing_request,
    build_status_query,
    build_time_response,
    decrypt_payload,
    make_login_key,
    make_session_key,
    parse_device_info_response,
    parse_dp_report,
)

_LOGGER = logging.getLogger(__name__)


def _entry_opt(entry: ConfigEntry, key: str, default):
    """Resolve a setting from options (live-editable), then data, then default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


class KohreeCoordinator(DataUpdateCoordinator[None]):
    """Manage BLE connection state for one Kohree lock."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        minutes = _entry_opt(
            entry, CONF_POLL_INTERVAL_MINUTES, DEFAULT_POLL_INTERVAL_MINUTES
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"Kohree {entry.data[CONF_ADDRESS]}",
            update_interval=timedelta(minutes=minutes),
        )
        self._entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.device_name: str = entry.data.get(CONF_DEVICE_NAME, "")

        self._client: BleakClient | None = None
        self._connected = False
        self._notify_enabled = False
        self._write_char_uuid: str | None = None
        self._notify_char_uuid: str | None = None
        self._read_char_uuid: str | None = None
        self._write_with_response = False
        self._profile_name: str | None = None

        # Tuya BLE protocol session state
        self._login_key: bytes | None = None
        self._session_key: bytes | None = None
        self._protocol_version: int = 3
        self._is_bound: bool = False
        self._is_paired: bool = False
        self._seq_num: int = 1
        self._assembler: PacketAssembler = PacketAssembler()

        self._last_seen_monotonic: float | None = None
        # Dedup: BlueZ/bleak occasionally delivers the same GATT notification
        # several times.  Identical raw bytes within a short window are dropped
        # so we don't act on a frame (e.g. a time request) more than once.
        self._last_notify_raw: bytes | None = None
        self._last_notify_mono: float = 0.0
        self._last_packet_hex: str | None = None
        self._last_response_hex: str | None = None
        self._last_write_hex: str | None = None
        self._last_error: str | None = None

        # Lock state (from DP reports)
        self._lock_state: bool | None = None   # True = locked (motor)
        self._battery: int | None = None
        self._dp_counter: int = 0

        # Command delivery / persistent connection
        self._paired_event = asyncio.Event()
        self._pending_dp: tuple[int, int, bytes, str] | None = None
        self._want_connected = False  # whether polling should (re)connect
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()
        # Guards the actual GATT write loop so multi-packet MTP frames are never
        # interleaved on the wire.  Held only for the brief duration of one
        # frame's packets — never across awaits like the pairing wait — so it
        # cannot deadlock with _command_lock (which the pair/device-info writes
        # run underneath).  Concurrent writers exist: post-auth sync, the async
        # time-response, and pair_req all race otherwise and corrupt frames.
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Return True when BLE GATT is connected."""
        return self._connected

    @property
    def notify_enabled(self) -> bool:
        """Return True when FFF1 notify subscription is active."""
        return self._notify_enabled

    @property
    def is_paired(self) -> bool:
        """Return True when Tuya BLE auth handshake is complete."""
        return self._is_paired

    @property
    def lock_state(self) -> bool | None:
        """True = locked, False = unlocked, None = unknown (from DP reports)."""
        return self._lock_state

    @property
    def battery(self) -> int | None:
        """Battery percentage from the most recent DP report."""
        return self._battery

    @property
    def last_seen_age(self) -> float | None:
        """Seconds since last received notification, if any."""
        if self._last_seen_monotonic is None:
            return None
        return time.monotonic() - self._last_seen_monotonic

    @property
    def last_packet_hex(self) -> str | None:
        """Most recent raw packet in uppercase hex."""
        return self._last_packet_hex

    @property
    def last_response_hex(self) -> str | None:
        """Most recent decrypted protocol response in uppercase hex."""
        return self._last_response_hex

    @property
    def last_write_hex(self) -> str | None:
        """Most recent outbound protocol packet in uppercase hex."""
        return self._last_write_hex

    @property
    def profile_name(self) -> str | None:
        """Resolved GATT profile name in active session."""
        return self._profile_name

    @property
    def last_error(self) -> str | None:
        """Most recent connection/setup error string."""
        return self._last_error

    @property
    def rssi(self) -> int | None:
        """Return last observed RSSI from BLE advertisements."""
        info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if info is None:
            info = bluetooth.async_last_service_info(self.hass, self.address, connectable=False)
        return info.rssi if info else None

    def _reset_session(self) -> None:
        """Reset all protocol session state (called on connect/disconnect)."""
        self._login_key = None
        self._session_key = None
        self._protocol_version = 2
        self._is_bound = False
        self._is_paired = False
        self._seq_num = 1
        self._dp_counter = 0
        self._paired_event.clear()
        self._assembler = PacketAssembler()
        self._last_response_hex = None
        self._last_write_hex = None

    async def async_start(self) -> None:
        """Start coordinator: do an initial connect + sync.

        The lock connects, dumps its DPs (state + battery), and disconnects
        itself, so this is a one-shot connect; the periodic poll
        (_async_update_data) refreshes on the configured interval.  ``_want_connected``
        gates whether polling reconnects (cleared by the Disconnect "pause" button).
        """
        self._want_connected = True
        await self._async_ensure_connected()

    async def async_stop(self) -> None:
        """Stop coordinator and disconnect BLE client."""
        self._want_connected = False
        await self._async_disconnect()

    async def async_reconnect(self) -> None:
        """Force a fresh connect cycle and (re)arm the persistent link.

        Used to resume after the Disconnect button "pauses" the integration
        (which clears ``_want_connected`` to free the lock for the Tuya app).
        Re-arming the flag here ensures the watchdog and drop-reconnect resume,
        and routing through ``_async_ensure_connected`` re-runs the post-auth
        sync so state/battery refresh on resume.
        """
        self._want_connected = True
        await self._async_disconnect()
        await self._async_ensure_connected()

    async def async_lock(self) -> None:
        """Lock the deadbolt (DP46 bool = 1)."""
        await self.async_send_dp(DP_LOCK, DP_TYPE_BOOL, b"\x01", "lock", optimistic_locked=True)

    async def async_unlock(self) -> None:
        """Unlock via ble_unlock_check (DP71) with a fresh timestamp."""
        passcode = self._entry.data.get(CONF_UNLOCK_PASSCODE, DEFAULT_UNLOCK_PASSCODE)
        value = build_ble_unlock_value(passcode, int(time.time()))
        await self.async_send_dp(
            DP_BLE_UNLOCK, DP_TYPE_RAW, value, "unlock", optimistic_locked=False
        )

    async def async_send_dp(
        self,
        dp_id: int,
        dp_type: int,
        value: bytes,
        desc: str,
        optimistic_locked: bool | None = None,
    ) -> None:
        """Authenticate (fresh session) then write one DP command.

        ``optimistic_locked`` (if given) is applied to the lock state the instant
        the write succeeds — so the UI reflects the new state as soon as the bolt
        actuates, not after waiting for the confirming report.  Any DP report that
        follows is still handled by the notification callback while the connection
        lingers, correcting the state if the command didn't take.
        """
        async with self._command_lock:
            await self._async_connect_and_auth(desc)
            self._dp_counter += 1
            try:
                packets = build_dp_command(
                    self._seq_num, self._session_key, self._dp_counter,
                    dp_id, dp_type, value,
                )
                self._seq_num += 1
            except Exception as err:  # noqa: BLE001
                raise HomeAssistantError(
                    f"Kohree {self.address}: build {desc} failed: {err}"
                ) from err
            await self._async_write_packets(packets, desc)
            if optimistic_locked is not None:
                self._lock_state = optimistic_locked
                self.async_set_updated_data(None)
            # Brief settle so the confirming report can land within this command's
            # lock; late reports are still processed by the notification callback.
            await asyncio.sleep(COMMAND_SETTLE_SECONDS)

    async def _async_ensure_connected(self) -> None:
        """Connect + authenticate + run the on-connect DP sync (best-effort).

        After auth, replays the app's sync so the lock pushes its DP reports
        (state + battery).  The lock then disconnects itself shortly after; we do
        not hold the link (see poll model).
        """
        if not self._want_connected:
            return
        async with self._command_lock:
            if self._is_paired and self._client is not None and self._client.is_connected:
                return
            try:
                await self._async_connect_and_auth("poll")
            except HomeAssistantError as err:
                _LOGGER.info(
                    "Kohree %s: connect failed, will retry next poll: %s",
                    self.address, err,
                )
                return
            await self._async_post_auth_sync()

    async def _async_post_auth_sync(self) -> None:
        """Replay the app's on-connect DP-sync sequence to elicit a full report.

        From the reconnect HCI capture, after pairing the app sends, in order:
          1. queryDps with an empty list,
          2. a dp69 (0x45) ble-check write (suffix 00, non-actuating),
          3. queryDps with the single byte 0x55.
        The lock then (once its clock is synced via the time response) pushes its
        full DP set as 0x8006 reports — including dp8 (battery).  Sending only the
        empty queryDps did NOT elicit dp8, so the whole sequence is replayed.

        Caller holds ``_command_lock``.
        """
        if self._session_key is None:
            return
        passcode = self._entry.data.get(CONF_UNLOCK_PASSCODE, DEFAULT_UNLOCK_PASSCODE)
        try:
            # 1. queryDps (empty list).
            packets = build_status_query(self._seq_num, self._session_key, [])
            self._seq_num += 1
            await self._async_write_packets(packets, "sync_query_all")

            # 2. dp69 ble-check (non-actuating status/verify variant).
            self._dp_counter += 1
            packets = build_dp_command(
                self._seq_num, self._session_key, self._dp_counter,
                DP_BLE_CHECK, DP_TYPE_RAW, build_ble_check_value(passcode),
            )
            self._seq_num += 1
            await self._async_write_packets(packets, "sync_ble_check")

            # 3. queryDps (single byte 0x55).
            packets = build_status_query(
                self._seq_num, self._session_key, [STATUS_SYNC_QUERY_BYTE],
            )
            self._seq_num += 1
            await self._async_write_packets(packets, "sync_query_55")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Kohree %s: post-auth sync failed: %s", self.address, err)

    async def _async_connect_and_auth(self, desc: str) -> None:
        """Ensure a fresh authenticated session (clean reconnect + handshake)."""
        if self._is_paired and self._client is not None and self._client.is_connected:
            return
        _LOGGER.info("Kohree %s: %s — connecting", self.address, desc)
        await self._async_disconnect()
        self._paired_event.clear()
        await self._async_connect()
        try:
            await asyncio.wait_for(self._paired_event.wait(), timeout=15)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(
                f"Kohree {self.address}: {desc} — lock did not authenticate"
            ) from err
        if self._session_key is None:
            raise HomeAssistantError(
                f"Kohree {self.address}: {desc} — no session key after auth"
            )

    async def _async_update_data(self) -> None:
        """Periodic poll (every configured interval) — connect + sync.

        This lock does not hold an idle connection — after a fresh connect + DP
        sync it dumps its state (incl. battery) and disconnects itself.  We do
        NOT auto-reconnect on that drop; the next tick handles it, keeping the
        lock's radio asleep between polls and bounding the connect-LED to once
        per interval.
        """
        await self._async_ensure_connected()
        return None

    async def _async_connect(self) -> None:
        """Connect to lock and subscribe to notify characteristic."""
        async with self._connect_lock:
            if self._client is not None and self._client.is_connected:
                self._connected = True
                self._last_error = None
                self.async_set_updated_data(None)
                return

            # Resolve via HA's Bluetooth manager, which routes through the best
            # available connectable source — a local adapter or an ESPHome proxy.
            # No adapter forcing is needed: the lock authenticates at the Tuya
            # application layer (no OS-level bonding), and the handshake — incl.
            # multi-packet notify reassembly — works over proxies (validated).
            ble_device = async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                ble_device = async_ble_device_from_address(
                    self.hass, self.address, connectable=False
                )
            if ble_device is None:
                self._connected = False
                self._notify_enabled = False
                self._last_error = "Lock not found in Bluetooth scanner cache"
                _LOGGER.warning("Kohree %s: %s", self.address, self._last_error)
                self.async_set_updated_data(None)
                return

            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.address,
                    disconnected_callback=self._on_disconnected,
                )
            except (BleakError, TimeoutError, OSError) as err:
                self._connected = False
                self._notify_enabled = False
                self._last_error = f"Connect failed: {err}"
                _LOGGER.warning("Kohree %s: connect failed: %s", self.address, err)
                self.async_set_updated_data(None)
                return

            self._client = client
            self._connected = True
            self._notify_enabled = False
            self._last_error = None
            self._write_char_uuid = None
            self._notify_char_uuid = None
            self._read_char_uuid = None
            self._write_with_response = False
            self._profile_name = None
            self._reset_session()

            _LOGGER.info(
                "Kohree %s: establish_connection complete, resolving GATT profile",
                self.address,
            )
            await asyncio.sleep(CONNECT_SETTLE_DELAY_SECONDS)
            try:
                profile = self._resolve_gatt_profile(client)
            except (BleakError, OSError) as err:
                # This lock self-disconnects ~2s after connect; occasionally it
                # drops within milliseconds of establish_connection returning,
                # before service discovery is readable. bleak then raises
                # "Service Discovery has not been performed yet" on
                # client.services. Treat it as a transient connect miss (same
                # graceful path as an establish_connection failure) so the poll
                # retries next interval instead of bubbling up to the
                # coordinator and flipping entities to unavailable.
                self._last_error = f"GATT profile resolve failed: {err}"
                _LOGGER.warning("Kohree %s: %s", self.address, self._last_error)
                await self._safe_disconnect(client)
                self._client = None
                self._connected = False
                self._notify_enabled = False
                self.async_set_updated_data(None)
                return
            if profile is None:
                self._last_error = (
                    "No supported GATT profile found "
                    f"({KOHREE_SERVICE_UUID}/{KOHREE_RW_CHAR_UUID} or "
                    f"{TUYA_BLE_SERVICE_UUID}/{TUYA_WRITE_CHAR_UUID}+{TUYA_NOTIFY_CHAR_UUID})"
                )
                _LOGGER.warning("Kohree %s: %s", self.address, self._last_error)
                self._log_gatt_map(client, "missing supported profile")
                await self._safe_disconnect(client)
                self._client = None
                self._connected = False
                self.async_set_updated_data(None)
                return

            write_char, notify_char, read_char, profile_name, write_with_response = profile
            self._write_char_uuid = write_char.uuid
            self._notify_char_uuid = notify_char.uuid
            self._read_char_uuid = read_char.uuid if read_char is not None else None
            self._write_with_response = write_with_response
            self._profile_name = profile_name
            _LOGGER.info(
                "Kohree %s: using GATT profile %s (write=%s notify=%s read=%s response=%s)",
                self.address,
                profile_name,
                self._write_char_uuid,
                self._notify_char_uuid,
                self._read_char_uuid,
                self._write_with_response,
            )
            self._log_gatt_map(client, "profile resolved")

            # NOTE: We intentionally do NOT call client.pair() here.  OS-level
            # bonding (createBond) is a separate, capability-gated subsystem and
            # is never tied to the local_key.  Any bond this lock needs is
            # device-initiated SMP, which BlueZ handles itself when the device
            # is trusted — a proactive pair() returns AuthenticationCanceled.
            try:
                await client.start_notify(notify_char, self._on_notification)
            except (BleakError, TimeoutError, OSError) as err:
                self._last_error = f"start_notify failed: {err}"
                _LOGGER.warning("Kohree %s: %s", self.address, self._last_error)
                await self._safe_disconnect(client)
                self._client = None
                self._connected = False
                self._notify_enabled = False
                self.async_set_updated_data(None)
                return

            self._notify_enabled = True

            # Verify CCCD is actually enabled after start_notify.
            # Note: BlueZ often returns empty bytes on CCCD reads (it manages the
            # descriptor locally and may not expose the value via ReadValue).
            # Empty is ambiguous — log it but don't treat it as a failure.
            cccd_found = False
            for desc in notify_char.descriptors:
                if "2902" in desc.uuid.lower():
                    cccd_found = True
                    try:
                        cccd_val = bytes(await client.read_gatt_descriptor(desc.handle))
                        _LOGGER.info(
                            "Kohree %s: CCCD[%s] after start_notify: %s (len=%d)",
                            self.address, desc.uuid, cccd_val.hex().upper(), len(cccd_val),
                        )
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.info("Kohree %s: CCCD read failed: %s", self.address, exc)
            if not cccd_found:
                _LOGGER.warning(
                    "Kohree %s: notify char has no CCCD descriptor — start_notify may not work",
                    self.address,
                )

            # Send FUN_SENDER_DEVICE_INFO immediately — Tuya BLE handshake
            # initiator. Awaited directly (not via async_create_task) so the
            # write happens before releasing the connect lock, minimising the
            # window between start_notify and the first write.
            local_key = self._entry.data.get(CONF_TUYA_LOCAL_KEY, "")
            sec_key = self._entry.data.get(CONF_TUYA_SEC_KEY, "")
            _LOGGER.info(
                "Kohree %s: local_key len=%d prefix=%s sec_key len=%d",
                self.address, len(local_key), local_key[:3] if local_key else "(empty)",
                len(sec_key),
            )
            if local_key:
                try:
                    # Reconnect scheme (verified from HCI capture): device_info
                    # uses security flag 4 = MD5(local_key[:6]); session uses
                    # flag 5 = MD5(local_key[:6] + srand).  No secKey needed for
                    # an already-bound device.
                    login_key = make_login_key(local_key)
                    self._login_key = login_key
                    _LOGGER.info(
                        "Kohree %s: login_key(flag4)=%s", self.address, login_key.hex().upper()
                    )
                    packets = build_device_info_request(
                        self._seq_num, login_key, self._protocol_version,
                    )
                    self._seq_num += 1
                except Exception as err:  # noqa: BLE001
                    self._last_error = f"Build device_info request failed: {err}"
                    _LOGGER.warning("Kohree %s: %s", self.address, self._last_error)
                else:
                    await self._async_write_and_read(packets)

            _LOGGER.info(
                "Kohree %s: connected (profile=%s), sent FUN_SENDER_DEVICE_INFO",
                self.address,
                self._profile_name,
            )
            self.async_set_updated_data(None)

    def _log_gatt_map(self, client: BleakClient, reason: str) -> None:
        """Log a compact GATT map to help identify runtime UUID differences."""
        services = client.services
        if services is None:
            _LOGGER.warning("Kohree %s: no GATT services available (%s)", self.address, reason)
            return

        lines: list[str] = []
        for service in services:
            lines.append(f"service {service.uuid}")
            for char in service.characteristics:
                properties = ",".join(sorted(char.properties)) if char.properties else "-"
                lines.append(
                    f"  char {char.uuid} props={properties} descriptors={len(char.descriptors)}"
                )

        _LOGGER.info(
            "Kohree %s: GATT map (%s)\n%s",
            self.address,
            reason,
            "\n".join(lines),
        )

    async def _async_disconnect(self) -> None:
        """Disconnect active client."""
        async with self._connect_lock:
            client = self._client
            self._client = None
            self._write_char_uuid = None
            self._notify_char_uuid = None
            self._read_char_uuid = None
            self._write_with_response = False
            self._profile_name = None
            self._reset_session()

            if client is not None:
                await self._safe_disconnect(client)

            self._connected = False
            self._notify_enabled = False
            self.async_set_updated_data(None)

    async def _safe_disconnect(self, client: BleakClient) -> None:
        """Best-effort notify stop and disconnect."""
        if self._notify_char_uuid and client.is_connected:
            try:
                await client.stop_notify(self._notify_char_uuid)
            except Exception:  # noqa: BLE001
                pass

        if client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    @callback
    def _on_disconnected(self, _client: BleakClient) -> None:
        """Handle unexpected disconnect callback from Bleak."""
        _LOGGER.info(
            "Kohree %s: disconnected (paired=%s last_packet=%s last_error=%s)",
            self.address,
            self._is_paired,
            self._last_packet_hex,
            self._last_error,
        )
        self._client = None
        self._connected = False
        self._notify_enabled = False
        self._write_char_uuid = None
        self._notify_char_uuid = None
        self._read_char_uuid = None
        self._write_with_response = False
        self._profile_name = None
        # Do NOT call _reset_session() here — login_key/session_key must survive
        # long enough for any in-flight GATT notifications to be decrypted.
        # Session is reset at the top of the next _async_connect().
        self._is_paired = False
        self._is_bound = False
        self._paired_event.clear()
        self.async_set_updated_data(None)
        # Do NOT auto-reconnect — this lock disconnects itself right after each
        # sync dump, so reconnecting on drop produces a tight LED loop.  The
        # periodic poll (_async_update_data) re-establishes on its own cadence.

    def _resolve_gatt_profile(
        self, client: BleakClient
    ) -> (
        tuple[
            BleakGATTCharacteristic,
            BleakGATTCharacteristic,
            BleakGATTCharacteristic | None,
            str,
            bool,
        ]
        | None
    ):
        """Find supported write/notify characteristic pair for this lock."""
        services = client.services

        legacy_service = services.get_service(KOHREE_SERVICE_UUID)
        if legacy_service is not None:
            legacy_char = legacy_service.get_characteristic(KOHREE_RW_CHAR_UUID)
            if legacy_char is not None:
                write_props = set(legacy_char.properties or [])
                write_with_response = (
                    "write" in write_props and "write-without-response" not in write_props
                )
                return (
                    legacy_char,
                    legacy_char,
                    legacy_char,
                    "legacy_fff0_fff1",
                    write_with_response,
                )

        tuya_service = services.get_service(TUYA_BLE_SERVICE_UUID)
        if tuya_service is not None:
            tuya_write = tuya_service.get_characteristic(TUYA_WRITE_CHAR_UUID)
            tuya_notify = tuya_service.get_characteristic(TUYA_NOTIFY_CHAR_UUID)
            tuya_read = tuya_service.get_characteristic(TUYA_READ_CHAR_UUID)
            if tuya_write is not None and tuya_notify is not None:
                write_props = set(tuya_write.properties or [])
                write_with_response = (
                    "write" in write_props and "write-without-response" not in write_props
                )
                return (
                    tuya_write,
                    tuya_notify,
                    tuya_read,
                    "tuya_fd50_0001_0002",
                    write_with_response,
                )

        # Last resort for older firmware variants where FFF1 is exposed globally.
        global_legacy = services.get_characteristic(KOHREE_RW_CHAR_UUID)
        if global_legacy is not None:
            write_props = set(global_legacy.properties or [])
            write_with_response = (
                "write" in write_props and "write-without-response" not in write_props
            )
            return (
                global_legacy,
                global_legacy,
                global_legacy,
                "legacy_global_fff1",
                write_with_response,
            )

        return None

    async def _async_write_and_read(self, packets: list[bytes]) -> None:
        """Write device_info_req, then wait up to 5 seconds for a GATT notification.

        After 2 seconds with no notification, also try reading chars 00000002 and
        00000003 directly — some Tuya fd50 firmware returns the response via read
        rather than notification.
        """
        await self._async_write_packets(packets, "device_info_req", settle_ms=0)
        _LOGGER.info("Kohree %s: waiting up to 5s for notification...", self.address)
        read_attempted = False
        for tick in range(50):
            await asyncio.sleep(0.1)
            client = self._client
            if client is None or not client.is_connected:
                _LOGGER.info(
                    "Kohree %s: disconnected at tick %d, last_packet=%s",
                    self.address, tick, self._last_packet_hex,
                )
                return
            if self._last_packet_hex is not None:
                _LOGGER.info(
                    "Kohree %s: notification arrived at tick %d: %s",
                    self.address, tick, self._last_packet_hex,
                )
                return
            # After 2s with no notification, try direct reads on both chars.
            if tick == 20 and not read_attempted:
                read_attempted = True
                for char_uuid in (self._notify_char_uuid, self._read_char_uuid):
                    if char_uuid is None:
                        continue
                    try:
                        payload = bytes(await client.read_gatt_char(char_uuid))
                        _LOGGER.info(
                            "Kohree %s: direct read %s → %s",
                            self.address, char_uuid[-8:], payload.hex().upper() if payload else "(empty)",
                        )
                        if payload:
                            self._last_seen_monotonic = time.monotonic()
                            self._last_packet_hex = payload.hex().upper()
                            assembled = self._assembler.feed(payload)
                            if assembled is not None:
                                self._handle_notification_payload(assembled)
                                return
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.info(
                            "Kohree %s: direct read %s failed: %s",
                            self.address, char_uuid[-8:], exc,
                        )
        _LOGGER.info(
            "Kohree %s: 5s elapsed, no notification received (last_packet=%s)",
            self.address, self._last_packet_hex,
        )

    async def _async_write_packets(self, packets: list[bytes], reason: str, settle_ms: int = 0) -> None:
        """Write a list of 20-byte Tuya BLE MTP packets to the write characteristic."""
        if settle_ms > 0:
            await asyncio.sleep(settle_ms / 1000.0)
        client = self._client
        if client is None or not client.is_connected:
            self._last_error = f"Write skipped ({reason}): client not connected"
            _LOGGER.debug("Kohree %s: %s", self.address, self._last_error)
            return
        if self._write_char_uuid is None:
            self._last_error = f"Write skipped ({reason}): no write characteristic"
            _LOGGER.debug("Kohree %s: %s", self.address, self._last_error)
            return

        # Serialize the packet loop so a concurrent writer (e.g. the async time
        # response) cannot interleave its packets into the middle of this frame
        # — interleaved packets break MTP reassembly and the lock drops both.
        async with self._write_lock:
            for i, packet in enumerate(packets):
                try:
                    await client.write_gatt_char(
                        self._write_char_uuid,
                        packet,
                        response=self._write_with_response,
                    )
                except (BleakError, TimeoutError, OSError, ValueError) as err:
                    self._last_error = f"Write {reason}[{i}] failed: {err}"
                    _LOGGER.warning("Kohree %s: %s", self.address, self._last_error)
                    self.async_set_updated_data(None)
                    return

        self._last_write_hex = packets[-1].hex().upper() if packets else ""
        _LOGGER.info(
            "Kohree %s: wrote %s (%d packet(s)) pkt0=%s last=%s",
            self.address,
            reason,
            len(packets),
            packets[0].hex().upper() if packets else "",
            self._last_write_hex,
        )
        self.async_set_updated_data(None)

    def _handle_notification_payload(self, payload: bytes) -> None:
        """Process a fully reassembled + decrypted Tuya BLE payload."""
        try:
            result = decrypt_payload(payload, self._login_key, self._session_key)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Kohree %s: decrypt_payload error: %s", self.address, err)
            return

        if result is None:
            _LOGGER.warning(
                "Kohree %s: decrypt_payload returned None for %s",
                self.address,
                payload.hex().upper(),
            )
            return

        seq, resp_to, code, data = result
        self._last_response_hex = data.hex().upper() if data else ""
        _LOGGER.info(
            "Kohree %s: rx seq=%d resp_to=%d code=0x%04x data=%s",
            self.address,
            seq,
            resp_to,
            code,
            self._last_response_hex,
        )

        if code == FUN_SENDER_DEVICE_INFO:
            self._handle_device_info(seq, data)
        elif code == FUN_SENDER_PAIR:
            self._handle_pair_response(data)
        elif code in (FUN_RECEIVE_TIME1_REQ, FUN_RECEIVE_TIME2_REQ):
            self._handle_time_request(seq, code, data)
        elif code in (FUN_RECEIVE_DP_REPORT, FUN_RECEIVE_DP):
            self._handle_dp_report(data)
        elif code in (FUN_SENDER_DP, FUN_SENDER_DEVICE_STATUS):
            # Command/query acknowledgement (e.g. 0x0027/0x0003 -> ...0X00).
            _LOGGER.debug("Kohree %s: ack code=0x%04x data=%s", self.address, code, data.hex())
        else:
            _LOGGER.info(
                "Kohree %s: unhandled code=0x%04x data=%s",
                self.address,
                code,
                self._last_response_hex,
            )

    def _handle_dp_report(self, data: bytes) -> None:
        """Parse a DP status report and update lock/battery state."""
        for dp_id, dp_type, value in parse_dp_report(data):
            ivalue = int.from_bytes(value, "big") if value else 0
            if dp_id == DP_MOTOR_STATE:
                # lock_motor_state (authoritative bolt position): 0=locked, 1=unlocked
                self._lock_state = ivalue == 0
                _LOGGER.info(
                    "Kohree %s: dp47 motor_state=%d -> locked=%s",
                    self.address, ivalue, self._lock_state,
                )
            elif dp_id == DP_LOCK:
                # manual_lock command/state: 1=locked, 0=unlocked
                self._lock_state = ivalue == 1
                _LOGGER.info(
                    "Kohree %s: dp46 manual_lock=%d -> locked=%s",
                    self.address, ivalue, self._lock_state,
                )
            elif dp_id == DP_BATTERY:
                self._battery = ivalue
                _LOGGER.info("Kohree %s: battery=%s%%", self.address, self._battery)
            elif dp_id == DP_UNLOCK_BLE_COUNT:
                _LOGGER.info("Kohree %s: unlock_ble event (count=%s)", self.address, ivalue)
            else:
                _LOGGER.info(
                    "Kohree %s: dp%d type=%d value=%s",
                    self.address, dp_id, dp_type, value.hex(),
                )
        self.async_set_updated_data(None)

    def _handle_device_info(self, resp_to: int, data: bytes) -> None:
        """Handle FUN_SENDER_DEVICE_INFO response — derive session_key, send pair."""
        # Process device_info only once per session.  The lock can emit it more
        # than once; re-deriving the session key and re-sending the pair request
        # confuses the lock (multiple pair seqs) and the handshake never lands.
        if self._session_key is not None:
            _LOGGER.debug("Kohree %s: ignoring duplicate device_info", self.address)
            return
        info = parse_device_info_response(data)
        if info is None:
            _LOGGER.warning(
                "Kohree %s: parse_device_info_response failed data=%s",
                self.address,
                data.hex().upper(),
            )
            return

        _LOGGER.info("Kohree %s: device_info=%s", self.address, info)

        srand: bytes = info.get("srand", b"")
        if not srand:
            _LOGGER.warning("Kohree %s: device_info missing srand", self.address)
            return

        local_key = self._entry.data.get(CONF_TUYA_LOCAL_KEY, "")
        sec_key = self._entry.data.get(CONF_TUYA_SEC_KEY, "")
        if not local_key:
            _LOGGER.warning("Kohree %s: no local_key in config entry", self.address)
            return

        try:
            # flag-5 session key = MD5(local_key[:6] + srand)
            self._session_key = make_session_key(local_key, srand)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Kohree %s: make_session_key failed: %s", self.address, err)
            return

        _LOGGER.info("Kohree %s: session_key(flag5) derived, sending FUN_SENDER_PAIR", self.address)

        uuid = self._entry.data.get(CONF_TUYA_UUID, "")
        dev_id = self._entry.data.get(CONF_TUYA_DEV_ID, "")

        try:
            packets = build_pairing_request(
                self._seq_num,
                uuid,
                local_key,
                dev_id,
                self._session_key,
            )
            self._seq_num += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Kohree %s: build_pairing_request failed: %s", self.address, err)
            return

        self._entry.async_create_background_task(
            self.hass,
            self._async_write_packets(packets, "pair_req"),
            name=f"kohree_pair_{self.address.replace(':', '').lower()}",
        )

    def _handle_pair_response(self, data: bytes) -> None:
        """Handle FUN_SENDER_PAIR response.

        Observed reconnect success codes: 0x00 (fresh) and 0x02 (already bound).
        Ignore further 0x0001 frames once authenticated — the lock reuses this
        code for post-auth acknowledgements (e.g. after a time response).
        """
        if self._is_paired:
            _LOGGER.debug(
                "Kohree %s: ignoring post-auth 0x0001 data=%s",
                self.address, data.hex() if data else "",
            )
            return
        if data and data[0] in (0x00, 0x02):
            self._is_paired = True
            self._paired_event.set()
            _LOGGER.info(
                "Kohree %s: pairing SUCCESS (0x%02x) — lock authenticated",
                self.address, data[0],
            )
        else:
            result_code = data[0] if data else 0xFF
            _LOGGER.warning(
                "Kohree %s: pairing FAILED result=0x%02x", self.address, result_code
            )
        self.async_set_updated_data(None)

    def _handle_time_request(self, resp_to: int, code: int, _data: bytes) -> None:
        """Handle FUN_RECEIVE_TIME1_REQ / FUN_RECEIVE_TIME2_REQ."""
        if self._session_key is None:
            return
        time_type = 1 if code == FUN_RECEIVE_TIME1_REQ else 2
        try:
            packets = build_time_response(
                self._seq_num,
                resp_to,
                self._session_key,
                time_type,
            )
            self._seq_num += 1
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Kohree %s: build_time_response failed: %s", self.address, err)
            return

        self._entry.async_create_background_task(
            self.hass,
            self._async_write_packets(packets, f"time_resp_{time_type}"),
            name=f"kohree_time_{self.address.replace(':', '').lower()}",
        )

    @callback
    def _on_notification(
        self,
        _characteristic: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        """Handle BLE notification — feed 20-byte chunks into the packet assembler."""
        raw = bytes(data)
        now = time.monotonic()

        # Drop duplicate deliveries of the same frame (identical bytes within 1s).
        # Protocol frames carry a seq number + random IV, so legitimate frames are
        # never byte-identical; only BlueZ/bleak re-delivery produces a repeat.
        if raw == self._last_notify_raw and (now - self._last_notify_mono) < 1.0:
            _LOGGER.debug("Kohree %s: dropping duplicate rx chunk", self.address)
            return
        self._last_notify_raw = raw
        self._last_notify_mono = now

        self._last_seen_monotonic = now
        self._last_packet_hex = raw.hex().upper()

        _LOGGER.info("Kohree %s: rx chunk %s", self.address, self._last_packet_hex)

        try:
            assembled = self._assembler.feed(raw)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Kohree %s: assembler error: %s", self.address, err)
            self._assembler = PacketAssembler()
            return

        if assembled is not None:
            self._handle_notification_payload(assembled)
