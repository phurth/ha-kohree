"""Constants for the Kohree BLE lock integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "ha_kohree"

CONF_DEVICE_NAME = "device_name"
CONF_ONBOARDING_MODE = "onboarding_mode"
CONF_CLOUD_AUTH_MODE = "tuya_cloud_auth_mode"

ONBOARDING_MODE_LOCAL = "local_only"
ONBOARDING_MODE_CLOUD = "cloud_assisted"

CLOUD_AUTH_MODE_LIVE_QR = "live_qr"
CLOUD_AUTH_MODE_MANUAL = "manual_entry"

CONF_TUYA_DEV_ID = "tuya_dev_id"
CONF_TUYA_LOCAL_KEY = "tuya_local_key"
CONF_TUYA_UUID = "tuya_uuid"
CONF_TUYA_SEC_KEY = "tuya_sec_key"
CONF_TUYA_PRODUCT_ID = "tuya_product_id"
CONF_TUYA_USER_CODE = "tuya_user_code"

CONF_UNLOCK_PASSCODE = "tuya_unlock_passcode"

TUYA_CLOUD_BOOTSTRAP_KEYS = (
	CONF_TUYA_DEV_ID,
	CONF_TUYA_LOCAL_KEY,
	CONF_TUYA_UUID,
	CONF_TUYA_SEC_KEY,
	CONF_TUYA_PRODUCT_ID,
	CONF_UNLOCK_PASSCODE,
)

KOHREE_NAME_PREFIX = "KOHREE"

DEFAULT_LOCK_PASSCODE = "0000"
DEFAULT_PASSCODE_SUFFIX = "0000"

KOHREE_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
KOHREE_RW_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
TUYA_BLE_SERVICE_UUID = "0000fd50-0000-1000-8000-00805f9b34fb"
TUYA_WRITE_CHAR_UUID = "00000001-0000-1001-8001-00805f9b07d0"
TUYA_NOTIFY_CHAR_UUID = "00000002-0000-1001-8001-00805f9b07d0"
TUYA_READ_CHAR_UUID = "00000003-0000-1001-8001-00805f9b07d0"
TUYA_LOCK_MANUFACTURER_ID = 2000
TUYA_LOCK_BONDED_PREFIX = 0x49

KOHREE_SERVICE_UUIDS = (KOHREE_SERVICE_UUID,)

# Tuya product id for the Kohree Smart RV Lock.
# Keep this allowlist strict to avoid overbroad fd50 discovery.
KNOWN_TUYA_LOCK_PIDS = frozenset({"6m47tkja"})

CONNECT_SETTLE_DELAY_SECONDS = 0.2
# How long a lock/unlock command holds the connection after writing, to let the
# confirming DP report land in-band.  State is already set optimistically on
# write, so this is short; late reports are still caught by the notify callback.
COMMAND_SETTLE_SECONDS = 1.0
# Poll cadence.  This lock does not hold an idle BLE connection — it connects,
# dumps its DPs (state + battery), and disconnects itself.  So each poll is a
# brief connect/sync/drop.  The interval bounds how often the unit's connect LED
# lights and how fresh out-of-band state is; tune against the lock's battery.
# User-configurable via the options flow (minutes); this is the fallback default.
CONF_POLL_INTERVAL_MINUTES = "poll_interval_minutes"
DEFAULT_POLL_INTERVAL_MINUTES = 5
MIN_POLL_INTERVAL_MINUTES = 1
MAX_POLL_INTERVAL_MINUTES = 120
RECONNECT_POLL_INTERVAL = timedelta(minutes=DEFAULT_POLL_INTERVAL_MINUTES)

# NOTE: a "continuous" (always-connected) mode was tried and removed — this lock
# drops the BLE link ~2s after each sync regardless of USB power, so a held
# connection is not achievable on this firmware.  Poll is the only viable model.

# Lock DP map (verified from HCI capture + live testing, product 6m47tkja)
DP_BLE_UNLOCK = 71      # 0x47  raw  — ble_unlock_check (dynamic BLE unlock)
DP_BLE_CHECK = 69       # 0x45  raw  — ble check/sync (suffix-00, non-actuating);
                        #              app writes this first after pairing and the
                        #              lock then reports its full DP set (battery).
DP_LOCK = 46            # 0x2e  bool — manual_lock (lock command)
DP_MOTOR_STATE = 47     # 0x2f  bool — lock_motor_state (authoritative bolt state,
                        #              pushed on any change incl. manual/key)
DP_UNLOCK_BLE_COUNT = 19  # 0x13 int — unlock_ble counter
DP_BATTERY = 8          # 0x08  int  — residual_electricity (%)

# Second queryDps the app issues during on-connect sync (data byte 0x55).
# Replayed verbatim from the capture; the lock dumps its DPs after this.
STATUS_SYNC_QUERY_BYTE = 0x55

# BLE unlock passcode (ascii digits inside the ble_unlock_check cloud DP).
# Auto-fetched per-device in the config flow and stored as CONF_UNLOCK_PASSCODE.
# This is only a placeholder fallback for when the cloud value is unavailable;
# it is not a real passcode (the real one is device-specific and auto-fetched).
DEFAULT_UNLOCK_PASSCODE = "00000000"

DEFAULT_LOCK_PASSCODE = "0000"
DEFAULT_PASSCODE_SUFFIX = "0000"

DECODE_XOR_KEY = 0xA6
CMD_HANDSHAKE_REQUEST = 0xA1
CMD_CHALLENGE_STAGE_1 = 0xD1
CMD_CHALLENGE_STAGE_2 = 0xD2
CMD_PAIR_REQUEST = 0xA6
CMD_VERIFY_REQUEST = 0xA7

RESP_PAIR_SUCCESS = "53D600"
RESP_PAIR_FAILURE = "53D601"
RESP_VERIFY_SUCCESS = "53D700"
RESP_VERIFY_FAILURE = "53D701"
