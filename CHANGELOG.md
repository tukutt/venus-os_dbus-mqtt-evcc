# Changelog

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
