# Kohree Smart RV Lock — Technical Specification

- Status: Stable (v1.0.0)
- Scope: Home Assistant HACS custom integration for Kohree Smart RV door locks (Tuya BLE, product `6m47tkja`)
- Transport: Bluetooth Low Energy, Tuya BLE `fd50` profile (AES-128-CBC, varint MTP framing)

This document describes the protocol and architecture as implemented. The Tuya
BLE protocol described here follows the publicly documented behavior used by the
open-source `tuya-ble` Home Assistant projects.

---

## 1. GATT Profile

| Role | UUID |
|------|------|
| Service | `0000fd50-0000-1000-8000-00805f9b34fb` |
| Write (write-without-response) | `00000001-0000-1001-8001-00805f9b07d0` |
| Notify | `00000002-0000-1001-8001-00805f9b07d0` |
| Read | `00000003-0000-1001-8001-00805f9b07d0` |

The notify characteristic carries a single CCCD descriptor (`00002902`).
The connection works over either a local Bluetooth adapter or an ESPHome
Bluetooth proxy — the integration does not force an adapter; Home Assistant's
Bluetooth manager selects the best available connectable source. (The lock
authenticates at the application layer with no OS-level bonding, and
multi-packet notify reassembly relays correctly through proxies.)

## 2. Cryptography & Framing

### 2.1 Key derivation
```python
login_key   = MD5(local_key[:6])           # device-info exchange
session_key = MD5(local_key[:6] + srand)    # all subsequent commands
# srand = device_info response data[6:12]
```

### 2.2 Inner packet (before encryption)
```python
struct.pack(">IIHH", seq_num, response_to, code, data_len) + data + CRC16
# CRC-16 Modbus (poly 0xA001, init 0xFFFF, big-endian output)
# then null-padded to a multiple of 16 bytes
```

### 2.3 Encryption
- AES-128-CBC, random 16-byte IV per packet
- Encrypted payload: `[security_flag][iv:16][ciphertext]`
- `security_flag = 4` for the device-info exchange (uses `login_key`)
- `security_flag = 5` for all other commands (uses `session_key`)

### 2.4 MTP framing (20-byte GATT packets)
- Frame 0: `[varint(0)][varint(total_encrypted_len)][protocol_version << 4][chunk]`
- Frame N: `[varint(N)][chunk]`
- `protocol_version = 2` (TX frame-version byte `0x20`)

A receiver reassembles frames by leading varint until `total_encrypted_len`
bytes are collected, then decrypts.

## 3. Command Codes

| Constant | Code | Direction | Description |
|----------|------|-----------|-------------|
| FUN_SENDER_DEVICE_INFO | `0x0000` | C→D | Initiate handshake (data `00 f3`) |
| FUN_SENDER_PAIR | `0x0001` | C→D | Bound-device reconnect auth |
| FUN_SENDER_DEVICE_STATUS | `0x0003` | C→D | queryDps |
| FUN_SENDER_DP | `0x0027` | C→D | Write a datapoint (lock/unlock/check) |
| FUN_RECEIVE_DP | `0x8001` | D→C | Datapoint update |
| FUN_RECEIVE_DP_REPORT | `0x8006` | D→C | Datapoint status report (state, battery) |
| FUN_RECEIVE_TIME1_REQ | `0x8011` | D→C | Device requests time (ms + tz) |
| FUN_RECEIVE_TIME2_REQ | `0x8012` | D→C | Device requests time (struct) |

**Time response** (to `0x8011`): `ascii(ms_timestamp) + pack(">h", -timezone // 36)`.

## 4. Handshake (bound-device reconnect)

The integration only ever *reconnects* to an already-bound lock, so it needs
only the `local_key` (no first-pairing/activation secrets).

1. Client connects and subscribes to notify (`00000002`).
2. Client → device: `FUN_SENDER_DEVICE_INFO`, `security_flag=4`, data `00 f3`.
3. Device → client: device-info response; `srand = data[6:12]`.
4. Client derives `session_key = MD5(local_key[:6] + srand)`.
5. Client → device: `FUN_SENDER_PAIR`, `security_flag=5`, data
   `uuid + local_key[:6] + device_id`, zero-padded, trailing `01` (46 bytes).
6. Device → client: pair response — `data[0]` of `0x00` or `0x02` = success.
7. Device sends a time request (`0x8011`); client answers (§3). The device will
   not emit datapoint reports until its clock is set.
8. Connection is authenticated; datapoint traffic follows.

## 5. Datapoints (product `6m47tkja`)

| DP | id | type | Meaning |
|----|----|------|---------|
| dp8 | `0x08` | int | Battery percentage (residual electricity) |
| dp19 | `0x13` | int | `unlock_ble` counter |
| dp46 | `0x2e` | bool | `manual_lock` — `1` = locked |
| dp47 | `0x2f` | bool | `lock_motor_state` (authoritative bolt position) — `0` = locked, `1` = unlocked (inverted vs dp46) |
| dp69 | `0x45` | raw | BLE check/sync — `ffff0001` + ascii(passcode) + `00` (suffix-00, **non-actuating**) |
| dp71 | `0x47` | raw | `ble_unlock_check` (real unlock) — `ffff0001` + ascii(passcode) + `01` + ts(4B) + `0001` |

**Write (`0x0027`) inner data:** `[sn:4=0][counter:1][dp_id:1][dp_type:1][dp_len:2][value]`.
**Report (`0x8006`) inner data:** `[sn:4][counter:1][rsv:2=0000][dp_id:1][dp_type:1][dp_len:2][value]`.

### 5.1 On-connect status sync
After authentication and time sync, the client replays the sequence that causes
the lock to dump its full datapoint set (including battery):

1. `queryDps` with an empty DP-id list,
2. a dp69 BLE-check write (suffix `00`, non-actuating),
3. `queryDps` with the single byte `0x55`.

The lock then emits `0x8006` reports for its datapoints. An empty `queryDps`
alone does **not** elicit the battery report on this firmware.

## 6. Connection Model

This lock does **not** hold an idle BLE connection — after a fresh connect +
status sync it reports its datapoints and disconnects itself (~2s later,
graceful). Holding the link open or auto-reconnecting on every drop produces a
tight connect loop (and constantly lights the unit's connect LED); a keepalive
does not prevent the drop, and this behavior is unchanged on external USB-C
power. Therefore the integration uses a **poll** model:

- Every poll interval: connect → authenticate → status sync (refreshes lock
  state + battery) → let the lock drop the link. No auto-reconnect on drop.
- Poll interval is user-configurable (1–120 minutes, default 5), in both the
  setup flow and the options flow.
- Because the lock is disconnected between polls, real-time push of out-of-band
  electronic actuation is not available; state is refreshed each poll and set
  optimistically on Home Assistant lock/unlock commands.
- Lock/unlock commands pay a cold connect + handshake (typically a few seconds)
  before the bolt actuates.

### 6.1 Reliability details
- **Serialized writes:** all multi-packet MTP frames are written under a single
  lock so a concurrently-sent frame (e.g. the time response) cannot interleave
  its packets and corrupt reassembly.
- **Notification de-duplication:** identical notification frames delivered more
  than once within a short window are dropped.
- **Assumed state:** a manual thumb-turn is never reported by the lock (a
  hardware limitation — the vendor app cannot see it either), so the lock entity
  uses assumed state plus optimistic updates.
- **Battery persistence:** the battery sensor restores its last value across
  restarts, since the lock reports battery only on a fresh connect.

## 7. Credentials

| Field | Description |
|-------|-------------|
| `tuya_local_key` | 16-char ASCII local key (**rotates on every re-pair**) |
| `tuya_uuid` | Device UUID |
| `tuya_dev_id` | Device ID |
| `tuya_product_id` | Tuya product id (e.g. `6m47tkja`) |
| `tuya_unlock_passcode` | 8-digit BLE unlock passcode |

All fields are fetched **once** during the config flow via a Tuya cloud QR
login (account device enumeration), then stored locally in the Home Assistant
config entry. The unlock passcode is extracted from the cloud `ble_unlock_check`
datapoint. The cloud is not contacted during normal operation. If the lock is
re-paired in the vendor app, the local key changes and setup must be re-run.

## 8. Integration Architecture

```
custom_components/ha_kohree/
  __init__.py        — setup, coordinator wiring, options-reload listener
  manifest.json
  const.py           — UUIDs, config keys, DP map, timing constants
  config_flow.py     — Tuya cloud QR login → device selection → finalize; options flow (poll interval)
  coordinator.py     — BLE connection, handshake state machine, DP dispatch, poll loop
  protocol.py        — packet build/parse (AES-CBC, varint MTP, CRC-16, DP encode/decode)
  lock.py            — LockEntity (assumed state, optimistic)
  sensor.py          — battery (RestoreSensor)
  binary_sensor.py   — connection state (diagnostic)
  button.py          — reconnect / disconnect (diagnostic)
  diagnostics.py     — redacted diagnostics
  strings.json + translations/en.json
```

### 8.1 Entities
- `lock` — deadbolt control.
- `sensor` — battery percentage (diagnostic).
- `binary_sensor` — Connected (diagnostic).
- `button` — Reconnect (force refresh) and Disconnect (release the link so the
  vendor app can manage PINs/fingerprints; Reconnect resumes polling).

## 9. Security

- `local_key`, `login_key`, and `session_key` are never logged at INFO level.
- Credentials are stored in the Home Assistant config entry.
- `diagnostics.py` redacts all key material and the unlock passcode.
