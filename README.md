# Kohree Smart RV Lock — Home Assistant HACS Integration

Local Home Assistant integration for **Kohree Smart RV door locks** (Tuya BLE, product `6m47tkja`).

Controls the lock directly over Bluetooth Low Energy. A one-time cloud sign-in is used during setup to fetch the lock's local credentials; after that, all control is local — no cloud, no MQTT bridge, and no internet required at runtime.

> **Disclaimer:** This is an independent community integration and is not affiliated with, endorsed by, or supported by Kohree or Tuya. Use it at your own risk.

## Features

| Entity | Type | Notes |
|--------|------|-------|
| Lock / Unlock | `lock` | Deadbolt control over BLE. Uses optimistic state (see notes). |
| Battery | `sensor` | Battery percentage; persists across restarts. |
| Connected | `binary_sensor` | BLE connection status (diagnostic). |
| Reconnect | `button` | Force a reconnect/refresh (diagnostic). |
| Disconnect | `button` | Release the BLE link so the Kohree / Smart Life app can be used to manage PINs, fingerprints, etc. (diagnostic). |

## Requirements

- Home Assistant 2024.1+ with the Bluetooth integration
- A Bluetooth adapter on the Home Assistant host within range of the lock. A **local adapter is recommended** — this lock's handshake is not reliable through ESPHome Bluetooth proxies.
- A Tuya / Smart Life (Kohree) account with the lock already added, used **once** during setup to retrieve local credentials.

## Installation (HACS)

1. In Home Assistant, open **HACS → Integrations → ⋮ (top right) → Custom repositories**.
2. Add `https://github.com/phurth/ha-kohree` with category **Integration**.
3. Install **Kohree Smart RV Lock**.
4. Restart Home Assistant.

## Configuration

> **Prerequisite — set the lock up in the Tuya / Smart Life app first.** This is a required step: pairing the lock in the app adds it to your Tuya account, which is how this integration retrieves the lock's local credentials during setup. A lock that has never been added to the app cannot be configured here.

1. Go to **Settings → Devices & Services → Add Integration → Kohree Smart RV Lock**
   (the lock may also auto-discover and appear as a "Discovered" device).
2. When prompted, enter your **Tuya / Smart Life user code** (Me > Top Right Settings Menu > Account and Security > User Code)
3. Scan the generated **QR code** with the Smart Life (or Kohree) app to authorize a one-time login (scan icon at the top of the Tyua app main screen).
4. The integration retrieves the lock's local key and unlock passcode from the cloud, then selects the lock and finalizes the local BLE connection.
5. Set the **poll interval** (default 5 minutes) — see below.
6. **Force-quit the Tuya / Smart Life app** once setup is complete. The lock allows only one Bluetooth connection at a time, so an app left running in the background can hold the link and prevent Home Assistant from connecting.

Credentials are fetched only once at setup and stored locally; the cloud is not contacted during normal operation.

### Poll interval

This lock does not hold an idle Bluetooth connection — it connects, reports its state, and disconnects itself to save power. The integration therefore **polls**: every interval it briefly connects, refreshes lock state and battery, and lets the lock drop the link.

The poll interval is configurable (1–120 minutes, default 5) both during setup and afterward via **Settings → Devices & Services → Kohree Smart RV Lock → Configure**. A shorter interval gives fresher state but wakes the lock's radio (and lights its connect LED) more often.

## Notes & Limitations

- **Local key rotates on re-pairing.** If you remove and re-add the lock in the Smart Life app, re-run the integration setup to fetch the new key.
- **Manual thumb-turn is not reported.** The lock only reports state changes for *electronic* actuation (app, keypad, fingerprint) — not a physical turn of the thumb-turn. This is a hardware limitation (the Kohree / Smart Life app can't see it either), so the lock entity uses assumed state.
- **Command latency.** Because the lock is asleep between polls, each lock/unlock waits for a brief Bluetooth connect + handshake (typically a few seconds) before the bolt moves.
- Use the **Disconnect** button to free the lock for the Smart Life app (e.g. to add a PIN or fingerprint); **Reconnect** resumes Home Assistant control.

## License

MIT
