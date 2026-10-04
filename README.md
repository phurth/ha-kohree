# Kohree Smart RV Lock — Home Assistant HACS Integration

Local Home Assistant integration for **Kohree Smart RV door locks** (Tuya BLE, product `6m47tkja`).

Controls the lock directly over Bluetooth Low Energy. A one-time cloud sign-in is used during setup to fetch the lock's local credentials; after that, all control is local — no cloud, no MQTT bridge, and no internet required at runtime.

> **Disclaimer:** This is an independent community integration and is not affiliated with, endorsed by, or supported by Kohree or Tuya. Use it at your own risk.

## Features

| Entity | Type | Notes |
|--------|------|-------|
| Lock / Unlock | `lock` | Deadbolt control over BLE. Uses optimistic state (see notes). |
| Lock | `button` | Drives the bolt to locked unconditionally, regardless of the displayed state. |
| Unlock | `button` | Drives the bolt to unlocked unconditionally, regardless of the displayed state. |
| Battery | `sensor` | Battery percentage; persists across restarts. |
| Connected | `binary_sensor` | BLE connection status (diagnostic). |
| Reconnect | `button` | Force a reconnect/refresh (diagnostic). |
| Disconnect | `button` | Release the BLE link so the Kohree / Smart Life app can be used to manage PINs, fingerprints, etc. (diagnostic). |

## Requirements

- Home Assistant 2024.1+ with the Bluetooth integration
- Bluetooth coverage of the lock — either a local adapter on the Home Assistant host or an ESPHome Bluetooth proxy in range. Home Assistant routes automatically; no adapter is "forced."
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
5. Set the **battery refresh** interval (default 12 hours) — see below.
6. **Force-quit the Tuya / Smart Life app** once setup is complete. The lock allows only one Bluetooth connection at a time, so an app left running in the background can hold the link and prevent Home Assistant from connecting.

Credentials are fetched only once at setup and stored locally; the cloud is not contacted during normal operation.

### Connection model

This lock does not hold an idle Bluetooth connection — it connects, reports its state, and disconnects itself to save power. The integration therefore connects **only when there is something to do**: a lock or unlock command opens its own brief session, sends the command, and releases the link straight away.

Nothing is polled. The lock cannot report a manual thumb-turn, and it is not connected between commands, so periodic connecting bought very little state while waking the radio (and lighting the unit's LED) around the clock.

### Battery refresh

The one exception is the battery reading, which the lock only reports while connected. A slow background refresh wakes it just for that, configurable during setup and afterward via **Settings → Devices & Services → Kohree Smart RV Lock → Configure**: 1–168 hours, default 12, or **0 to disable it entirely**. Every lock/unlock also refreshes the battery as a side effect, so on a lock you use regularly you can turn the periodic refresh off.

## Notes & Limitations

- **Local key rotates on re-pairing.** If you remove and re-add the lock in the Smart Life app, re-run the integration setup to fetch the new key.
- **Manual thumb-turn is not reported.** The lock only reports state changes for *electronic* actuation (app, keypad, fingerprint) — not a physical turn of the thumb-turn. This is a hardware limitation (the Kohree / Smart Life app can't see it either), so the lock entity uses assumed state.
- **Command latency.** Because the lock is asleep between commands, each lock/unlock waits for a brief Bluetooth connect + handshake (typically a few seconds) before the bolt moves.
- **Last press wins.** Each lock/unlock opens its own Bluetooth connection, which takes a few seconds. If you press again while that is happening, the newer command replaces the older one rather than both running in turn — so a second press can't actuate the bolt twice in opposite directions.
- **Dedicated Lock / Unlock buttons.** Because state is assumed, the `lock` entity's toggle can disagree with the bolt's true position. The separate **Lock** and **Unlock** buttons each send the absolute command directly, so you can force either action without first correcting the displayed state.
- Use the **Disconnect** button to free the lock for the Smart Life app (e.g. to add a PIN or fingerprint); it also suspends the battery refresh. **Reconnect** resumes Home Assistant control.

## License

MIT
