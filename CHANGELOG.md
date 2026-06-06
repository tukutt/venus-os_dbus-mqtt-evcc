# Changelog

## 0.1.5
* Changed: register the dbus service **before** connecting to MQTT, so the charger
  always appears even if the broker is unreachable
* Fixed: MQTT reconnection no longer blocks — use `connect_async` +
  `reconnect_delay_set` and a non-blocking `on_disconnect` (the old blocking
  reconnect loop could wedge the driver)
* Reverted: `python -u` in the run script (its synchronous writes could block the
  driver if the log service stalled; `logging` already flushes each record)
* Fixed: `restart.sh` now restarts via `svc -t` instead of a fragile `pgrep`/`pkill`
  on the exact command line

## 0.1.4
* Fixed: `/Current` stayed at 0 — evcc has no scalar `chargeCurrent`; now read
  from `chargeCurrents/l1` (fallback: scalar `chargeCurrents`, then `offeredCurrent`)
* Fixed: `/ChargingTime` stayed at 0 — evcc publishes `chargeDuration` in seconds
  over MQTT, not nanoseconds (legacy ns values are still auto-detected)
* Added: `/Session/Energy` (from `sessionEnergy`) and `/Session/Time`
* Changed: run the driver with `python -u` so DEBUG logs are not buffered by multilog

## 0.1.3
* Fixed: "Enable Charging" toggle and "Charge Current" slider still shown in
  Venus OS / VRM despite read-only operation. `/StartStop` and `/SetCurrent`
  are no longer registered, so the GUI hides the non-functional controls
  (`/IsGenericEnergyMeter` alone did not hide them). Actual current stays on
  `/Current`.

## 0.1.2
* Added: `/IsGenericEnergyMeter = 1` so Venus OS hides the non-functional
  "Enable Charging" toggle and "Charge Current" slider (driver is read-only)

## 0.1.1
* Fixed: single-phase charger (`phases = 1`) was shown as three-phase in Venus OS;
  `/Ac/L2/Power` and `/Ac/L3/Power` are now only exposed when `phases = 3`

## 0.1.0
* Forked from `dbus-mqtt-ev-charger` as `dbus-mqtt-evcc`
* Added: native evcc MQTT API parser (one raw value per sub-topic, no JSON)
* Added: subscribe to `<topic_prefix>/loadpoints/<loadpoint_id>/#`
* Added: single/three phase power distribution (`phases`, `phasesActive`, `chargeCurrents/l1..l3`)
* Added: watchdog forcing Status=Disconnected / Power=0 on evcc silence
* Added: persistent `chargedEnergy` accumulator as `chargeTotalImport` fallback
* Added: unit tests for the evcc -> dbus translation (`test/test_parser.py`)
* Changed: service registers immediately with idle defaults (no minimum message)
* Note: read/display only, evcc stays the master of the charger

## 0.0.5
* Added: New dbus paths
* Changed: Fix restart issue
* Removed: Deprecated /ChargingTime value

## 0.0.4
⚠️ This version is required for Venus OS v3.60~27 or later, but it is also compatible with older versions.
* Added: paho-mqtt module to driver
* Changed: Broker port missing on reconnect
* Changed: Default device instance is now `100`
* Changed: Fixed service not starting sometimes

## v0.0.3
* Changed: Add VRM ID to MQTT client name
* Changed: Fix registration to dbus https://github.com/victronenergy/velib_python/commit/494f9aef38f46d6cfcddd8b1242336a0a3a79563

## v0.0.2
* Changed: Fixed problems when timeout was set to `0`.
* Changed: Fixed units for forwarded energy.
* Changed: Other smaller fixes.

## v0.0.1
Initial release
