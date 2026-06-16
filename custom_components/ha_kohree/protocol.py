"""Kohree lock BLE protocol — fd50/0001/0002 GATT profile.

This lock uses the Tuya BLE "fd50" profile.  The protocol is functionally
identical to the ha_tuya_ble (1910/2b11) Tuya BLE protocol:
  - AES-128-CBC encryption with a random IV per packet
  - Varint-framed 20-byte GATT writes (MTP framing)
  - Inner payload: struct.pack(">IIHH", seq, resp_to, code, data_len) + data + CRC16

Key derivation:
  - login_key  = MD5(local_key[:6].encode())          — used for FUN_SENDER_DEVICE_INFO
  - session_key = MD5(local_key[:6].encode() + srand) — used for all subsequent commands
    where srand = device_info_response_data[6:12]

Handshake sequence (CLIENT initiates):
  1. CLIENT → DEVICE: FUN_SENDER_DEVICE_INFO (0x0000), encrypted with login_key
  2. DEVICE → CLIENT: FUN_SENDER_DEVICE_INFO response with device info + srand
  3. CLIENT → DEVICE: FUN_SENDER_PAIR (0x0001), encrypted with session_key
  4. DEVICE → CLIENT: FUN_SENDER_PAIR response (result byte 0x00 = success)
"""

from __future__ import annotations

import hashlib
import secrets
from struct import pack, unpack

from Crypto.Cipher import AES

GATT_MTU = 20

# Security flag byte prefixed to the encrypted payload
_SEC_LOGIN = 4    # v1: encrypted with login_key   = MD5(local_key[:6])
_SEC_SESSION = 5  # v1: encrypted with session_key = MD5(local_key[:6] + srand)
# v2 (secKey scheme — Tuya SDK security levels 14/15)
_SEC_LOGIN_V2 = 14    # MD5((local_key + sec_key).encode())            [no srand]
_SEC_SESSION_V2 = 15  # MD5((local_key + sec_key).encode() + srand)

# Tuya BLE command codes
FUN_SENDER_DEVICE_INFO = 0x0000
FUN_SENDER_PAIR = 0x0001
FUN_SENDER_DEVICE_STATUS = 0x0003
FUN_SENDER_DP = 0x0027        # app→device DP write (lock/unlock commands)
FUN_RECEIVE_DP = 0x8001
FUN_RECEIVE_DP_REPORT = 0x8006  # device→app DP status report
FUN_RECEIVE_TIME1_REQ = 0x8011
FUN_RECEIVE_TIME2_REQ = 0x8012

# Tuya DP datatypes
DP_TYPE_RAW = 0x00
DP_TYPE_BOOL = 0x01
DP_TYPE_INT = 0x02
DP_TYPE_STR = 0x03
DP_TYPE_ENUM = 0x04
DP_TYPE_BITMAP = 0x05

# Default protocol version assumed before the device tells us otherwise
_DEFAULT_PROTOCOL_VERSION = 2


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def make_login_key(local_key: str) -> bytes:
    """Return the 16-byte login key: MD5(first-6-chars-of-local_key)."""
    return hashlib.md5(local_key[:6].encode()).digest()


def make_session_key(local_key: str, srand: bytes) -> bytes:
    """Return session key: MD5(first-6-chars-of-local_key + srand)."""
    return hashlib.md5(local_key[:6].encode() + srand).digest()


def make_login_key_v2(local_key: str, sec_key: str) -> bytes:
    """v2 level-14 key: MD5((local_key + sec_key).encode()).  No srand."""
    return hashlib.md5((local_key + sec_key).encode()).digest()


def make_session_key_v2(local_key: str, sec_key: str, srand: bytes) -> bytes:
    """v2 level-15 key: MD5((local_key + sec_key).encode() + srand)."""
    return hashlib.md5((local_key + sec_key).encode() + srand).digest()


# ---------------------------------------------------------------------------
# CRC-16 (Modbus / 0xA001 polynomial)
# ---------------------------------------------------------------------------

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte & 0xFF
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# ---------------------------------------------------------------------------
# Varint encode/decode
# ---------------------------------------------------------------------------

def _pack_int(value: int) -> bytes:
    result = bytearray()
    while True:
        curr = value & 0x7F
        value >>= 7
        if value:
            curr |= 0x80
        result.append(curr)
        if not value:
            break
    return bytes(result)


def _unpack_int(data: bytes, pos: int) -> tuple[int, int]:
    """Return (value, new_pos)."""
    result = 0
    for offset in range(5):
        p = pos + offset
        if p >= len(data):
            raise ValueError("truncated varint")
        b = data[p]
        result |= (b & 0x7F) << (offset * 7)
        if not (b & 0x80):
            return result, p + 1
    raise ValueError("varint overflow")


# ---------------------------------------------------------------------------
# Packet builder
# ---------------------------------------------------------------------------

def build_packets(
    seq_num: int,
    code: int,
    data: bytes,
    key: bytes,
    security_flag: int,
    response_to: int = 0,
    protocol_version: int = _DEFAULT_PROTOCOL_VERSION,
) -> list[bytes]:
    """Build one or more 20-byte BLE write packets (MTP varint framing, AES-CBC)."""
    iv = secrets.token_bytes(16)

    # Inner raw payload
    raw = bytearray()
    raw += pack(">IIHH", seq_num, response_to, code, len(data))
    raw += data
    raw += pack(">H", _crc16(raw))
    while len(raw) % 16:
        raw += b"\x00"

    # Encrypt
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = bytes([security_flag]) + iv + cipher.encrypt(raw)

    # MTP frame into GATT_MTU-byte chunks
    packets: list[bytes] = []
    packet_num = 0
    pos = 0
    total_len = len(encrypted)
    while pos < total_len:
        header = _pack_int(packet_num)
        if packet_num == 0:
            header += _pack_int(total_len)
            header += bytes([protocol_version << 4])
        chunk = encrypted[pos: pos + GATT_MTU - len(header)]
        packets.append(header + chunk)
        pos += len(chunk)
        packet_num += 1

    return packets


# ---------------------------------------------------------------------------
# Packet receiver / assembler
# ---------------------------------------------------------------------------

class PacketAssembler:
    """Reassembles multi-frame MTP notifications into a single payload buffer."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._buffer: bytearray = bytearray()
        self._expected_num: int = 0
        self._expected_len: int = 0

    def feed(self, data: bytes) -> bytes | None:
        """Feed one 20-byte notification.  Returns assembled payload when complete."""
        pos = 0
        try:
            pkt_num, pos = _unpack_int(data, pos)
        except ValueError:
            self._reset()
            return None

        if pkt_num != self._expected_num:
            self._reset()
            if pkt_num != 0:
                return None
            # Fall through to handle frame 0

        if pkt_num == 0:
            self._buffer = bytearray()
            try:
                self._expected_len, pos = _unpack_int(data, pos)
            except ValueError:
                self._reset()
                return None
            pos += 1  # skip protocol_version byte

        self._buffer += data[pos:]
        self._expected_num += 1

        if len(self._buffer) >= self._expected_len:
            result = bytes(self._buffer[: self._expected_len])
            self._reset()
            return result

        return None


# ---------------------------------------------------------------------------
# Payload decoder
# ---------------------------------------------------------------------------

def decrypt_payload(payload: bytes, login_key: bytes, session_key: bytes | None) -> tuple[int, int, int, bytes] | None:
    """Decrypt an assembled notification payload.

    Returns (seq_num, response_to, code, data) or None on error.
    """
    if not payload:
        return None
    security_flag = payload[0]
    if security_flag in (_SEC_LOGIN, _SEC_LOGIN_V2):
        key = login_key
    elif security_flag in (_SEC_SESSION, _SEC_SESSION_V2) and session_key is not None:
        key = session_key
    else:
        return None

    if len(payload) < 17:
        return None
    iv = payload[1:17]
    ciphertext = payload[17:]
    if len(ciphertext) % 16:
        return None

    cipher = AES.new(key, AES.MODE_CBC, iv)
    raw = cipher.decrypt(ciphertext)

    if len(raw) < 12:
        return None
    seq_num, response_to, code, data_len = unpack(">IIHH", raw[:12])
    if len(raw) < 12 + data_len:
        return None
    data = raw[12: 12 + data_len]
    return seq_num, response_to, code, data


# ---------------------------------------------------------------------------
# High-level packet constructors
# ---------------------------------------------------------------------------

def build_device_info_request(
    seq_num: int,
    login_key: bytes,
    protocol_version: int = _DEFAULT_PROTOCOL_VERSION,
    security_flag: int = _SEC_LOGIN,
) -> list[bytes]:
    """First packet after notify subscription: FUN_SENDER_DEVICE_INFO.

    Data ``00 f3`` matches the Smart Life app's reconnect handshake (observed
    via HCI capture).
    """
    return build_packets(seq_num, FUN_SENDER_DEVICE_INFO, b"\x00\xf3", login_key, security_flag, protocol_version=protocol_version)


def parse_device_info_response(data: bytes) -> dict | None:
    """Parse the data payload from a FUN_SENDER_DEVICE_INFO response.

    Returns dict with keys: protocol_version, is_bound, srand, auth_key.
    """
    if len(data) < 46:
        return None
    return {
        "device_version": f"{data[0]}.{data[1]}",
        "protocol_version": data[2],
        "flags": data[4],
        "is_bound": data[5] != 0,
        "srand": bytes(data[6:12]),
        "auth_key": bytes(data[14:46]),
    }


def build_pairing_request(
    seq_num: int,
    uuid: str,
    local_key: str,
    device_id: str,
    session_key: bytes,
    security_flag: int = _SEC_SESSION,
) -> list[bytes]:
    """FUN_SENDER_PAIR: uuid + local_key[:6] + device_id + 7x00 + 01 (46 bytes).

    Layout verified from the Smart Life app reconnect HCI capture.
    """
    payload = bytearray()
    payload += uuid.encode()
    payload += local_key[:6].encode()
    payload += device_id.encode()
    while len(payload) < 45:
        payload += b"\x00"
    payload += b"\x01"
    return build_packets(seq_num, FUN_SENDER_PAIR, bytes(payload[:46]), session_key, security_flag)


def build_time_response(seq_num: int, response_to: int, session_key: bytes, time_type: int) -> list[bytes]:
    """Respond to a FUN_RECEIVE_TIME1_REQ or FUN_RECEIVE_TIME2_REQ."""
    import time as _time
    from struct import pack as _pack
    if time_type == FUN_RECEIVE_TIME1_REQ:
        ts_ms = int(_time.time() * 1000)
        tz_offset = -int(_time.timezone / 36)
        data = str(ts_ms).encode() + _pack(">h", tz_offset)
    else:
        t = _time.localtime()
        tz_offset = -int(_time.timezone / 36)
        data = _pack(">BBBBBBBh", t.tm_year % 100, t.tm_mon, t.tm_mday,
                     t.tm_hour, t.tm_min, t.tm_sec, t.tm_wday, tz_offset)
    return build_packets(seq_num, time_type, data, session_key, _SEC_SESSION, response_to=response_to)


# ---------------------------------------------------------------------------
# DP commands (lock/unlock) — verified from reconnect HCI capture
# ---------------------------------------------------------------------------

def build_dp_command(
    seq_num: int,
    session_key: bytes,
    counter: int,
    dp_id: int,
    dp_type: int,
    value: bytes,
) -> list[bytes]:
    """FUN_SENDER_DP (0x0027): write one DP.

    Inner data layout (from capture): [sn:4=0][counter:1][dp_id:1][dp_type:1]
    [dp_len:2][value].
    """
    data = pack(">I", 0) + bytes([counter & 0xFF, dp_id & 0xFF, dp_type & 0xFF])
    data += pack(">H", len(value)) + value
    return build_packets(seq_num, FUN_SENDER_DP, data, session_key, _SEC_SESSION)


def build_status_query(seq_num: int, session_key: bytes, dp_ids: list[int]) -> list[bytes]:
    """FUN_SENDER_DEVICE_STATUS / queryDps (0x0003).

    The payload is the list of DP IDs to query (one byte each); the lock replies
    with a DP report (0x8006) for each.
    """
    data = bytes(dp_id & 0xFF for dp_id in dp_ids)
    return build_packets(
        seq_num, FUN_SENDER_DEVICE_STATUS, data, session_key, _SEC_SESSION,
    )


def build_ble_unlock_value(passcode: str, timestamp: int) -> bytes:
    """ble_unlock_check raw DP value: ffff 0001 + ascii(passcode) + 01 + ts + 0001."""
    return (
        b"\xff\xff\x00\x01"
        + passcode.encode()
        + b"\x01"
        + pack(">I", timestamp)
        + b"\x00\x01"
    )


def build_ble_check_value(passcode: str) -> bytes:
    """ble_unlock_check status/sync variant (suffix 00, no timestamp).

    Observed in the reconnect capture as the first DP the app writes after
    pairing (dp69/0x45).  Unlike the real unlock (suffix 01 + timestamp), this
    does NOT actuate the bolt — it acts as a status/verify trigger and the lock
    follows it by reporting its full DP set (battery, motor state, etc.).
    """
    return b"\xff\xff\x00\x01" + passcode.encode() + b"\x00"


def parse_dp_report(data: bytes) -> list[tuple[int, int, bytes]]:
    """Parse a FUN_RECEIVE_DP_REPORT (0x8006) payload into [(dp_id, type, value)].

    Layout: [sn:4][counter:1][rsv:2][dp_id:1][dp_type:1][dp_len:2][value]...
    """
    out: list[tuple[int, int, bytes]] = []
    if len(data) < 5:
        return out
    i = 7  # skip sn(4) + counter(1) + rsv(2)
    while i + 4 <= len(data):
        dp_id = data[i]
        dp_type = data[i + 1]
        dp_len = (data[i + 2] << 8) | data[i + 3]
        value = data[i + 4: i + 4 + dp_len]
        if len(value) != dp_len:
            break
        out.append((dp_id, dp_type, value))
        i += 4 + dp_len
    return out
