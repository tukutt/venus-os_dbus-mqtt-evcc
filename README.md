# dbus-mqtt-evcc - Bridge an evcc-controlled EV charger to Victron Venus OS

<small>GitHub repository: [tukutt/venus-os_dbus-mqtt-evcc](https://github.com/tukutt/venus-os_dbus-mqtt-evcc)</small>

## Index

1. [Disclaimer](#disclaimer)
1. [Purpose](#purpose)
1. [How it works](#how-it-works)
1. [Config](#config)
1. [Topic mapping](#topic-mapping-evcc--victron-dbus)
1. [Install / Update](#install--update)
1. [Uninstall](#uninstall)
1. [Restart](#restart)
1. [Debugging](#debugging)
1. [Tests](#tests)
1. [Compatibility](#compatibility)
1. [Credits](#credits)


## Disclaimer

I wrote this script for myself. I'm not responsible if you damage something using my script.


## Purpose

This driver subscribes directly to the **native [evcc MQTT API](https://docs.evcc.io/en/docs/integrations/mqtt-api)**
and exposes the charger that evcc controls as a real EV charger
(`com.victronenergy.evcharger`) on the Venus OS dbus, visible in the Remote
Console and on VRM.

There is **no reformatting intermediary** (no Node-RED, no automations, no
external script): the evcc parser lives inside the driver. It works with **any
charger driven by evcc**, since it consumes evcc's load point topics rather than
talking to the hardware.

> ⚠️ **Read/display only.** evcc remains the sole master of the charger. Values
> written on the dbus paths are **not** propagated back to evcc.


## How it works

evcc publishes **one raw value per sub-topic** (not a single JSON document),
under `<topic_prefix>/loadpoints/<loadpoint_id>/<key>`. The driver:

1. Subscribes to `<topic_prefix>/loadpoints/<loadpoint_id>/#`.
2. Keeps the last known value of every key in an internal state dict. Invalid
   payloads (`nil`, `null`, `none`, `-`, empty, …) are ignored so a glitch never
   wipes a good value.
3. Recomputes and republishes the Victron dbus paths on every message.

The dbus service registers immediately at start-up with idle/disconnected
defaults and is then populated as evcc messages arrive — **no "minimum message"
is required to start**.


## Config

Copy or rename `config.sample.ini` to `config.ini` in the `dbus-mqtt-evcc`
folder and adapt it. Key settings:

| Section | Key | Default | Description |
| --- | --- | --- | --- |
| `[MQTT]` | `broker_address` | `IP_ADDR_OR_FQDN` | Broker evcc publishes to |
| `[MQTT]` | `broker_port` | `1883` | Broker port |
| `[MQTT]` | `username` / `password` | – | Optional credentials |
| `[MQTT]` | `topic_prefix` | `evcc` | evcc MQTT topic prefix |
| `[MQTT]` | `loadpoint_id` | `1` | Load point to bridge |
| `[DEFAULT]` | `device_name` | `evcc Charger` | Name in Remote Console / VRM |
| `[DEFAULT]` | `device_instance` | `11` | VRM device instance |
| `[DEFAULT]` | `phases` | `1` | `1` = single phase, `3` = three phase |
| `[DEFAULT]` | `max_current` | `32` | Published on `/MaxCurrent` (A) |
| `[DEFAULT]` | `timeout` | `120` | Watchdog (s), `0` to disable |
| `[DEFAULT]` | `logging` | `WARNING` | `ERROR` / `WARNING` / `INFO` / `DEBUG` |

### Single vs. three phase

- `phases = 1`: all `chargePower` goes on `/Ac/L1/Power`, L2/L3 = 0. (Default,
  matches the Lektrico 1P7K which is single phase.)
- `phases = 3`: if evcc publishes per-phase currents (`chargeCurrents/l1..l3`)
  the power is distributed proportionally to those currents; otherwise it is
  split equally over `phasesActive`.

### Robustness

- **Watchdog**: if no evcc message is received for `timeout` seconds, `/Status`
  is forced to `0` (Disconnected) and `/Ac/Power` to `0`, so stale values are
  never left on the bus.
- **Energy fallback**: if evcc does not publish `chargeTotalImport`, the driver
  accumulates the positive deltas of `chargedEnergy` into a persistent
  `energy_state.json` (next to the driver) so `/Ac/Energy/Forward` survives
  session resets and reboots.


## Topic mapping (evcc → Victron dbus)

All topics are relative to `<topic_prefix>/loadpoints/<loadpoint_id>/`
(default `evcc/loadpoints/1/`).

| evcc topic | Example payload | Victron dbus path | Conversion |
| --- | --- | --- | --- |
| `chargePower` | `7360` (W) | `/Ac/Power` | — |
| `chargePower` | | `/Ac/L1/Power`, `/Ac/L2/Power`, `/Ac/L3/Power` | split per `phases` / `phasesActive` / `chargeCurrents` |
| `chargeCurrent` | `16` (A) | `/Current`, `/SetCurrent` | — |
| *(config `max_current`)* | `32` | `/MaxCurrent` | — |
| `chargeTotalImport` | `176920` (Wh) | `/Ac/Energy/Forward` | **Wh → kWh** (÷ 1000) |
| `chargedEnergy` *(fallback)* | `5210.4` (Wh, session) | `/Ac/Energy/Forward` | accumulated → kWh when `chargeTotalImport` absent |
| `chargeDuration` | `1340000000000` (ns) | `/ChargingTime` | **ns → s** (÷ 1e9, rounded) |
| `connected` | `true` / `false` | `/Connected` | `true → 1`, `false → 0` |
| `enabled` | `true` / `false` | `/StartStop` | `true → 1`, `false → 0` |
| `mode` | `off` / `now` / `minpv` / `pv` | `/Mode` | `pv`,`minpv → 1` (Auto), else `0` (Manual) |
| `charging` | `true` / `false` | `/Status` | see below |
| `connected` | | `/Status` | see below |
| `phasesActive` | `1` / `2` / `3` | *(phase split)* | used to distribute power in 3-phase mode |

### `/Status` logic

| Condition | `/Status` |
| --- | --- |
| `connected` and `charging` | `2` (Charging) |
| `connected`, not `charging`, `mode` in `pv`/`minpv` | `4` (Waiting for sun) |
| `connected`, not `charging` | `1` (Connected) |
| not `connected` | `0` (Disconnected) |


## Install / Update

1. Login to your Venus OS device via SSH. See [Venus OS: Root Access](https://www.victronenergy.com/live/ccgx:root_access#root_access) for more details.

2. Execute these commands to download and copy the files:

    ```bash
    wget -O /tmp/download_dbus-mqtt-evcc.sh https://raw.githubusercontent.com/tukutt/venus-os_dbus-mqtt-evcc/master/download.sh

    bash /tmp/download_dbus-mqtt-evcc.sh
    ```

3. Select the version you want to install.

4. Press enter for a single instance. For multiple instances, enter a number and press enter.

    Example:

    - Pressing enter or entering `1` will install the driver to `/data/etc/dbus-mqtt-evcc`.
    - Entering `2` will install the driver to `/data/etc/dbus-mqtt-evcc-2`.

### Extra steps for your first installation

5. Edit the config file to fit your needs. The correct command for your installation is shown after the installation.

    ```bash
    nano /data/etc/dbus-mqtt-evcc/config.ini
    ```

6. Install the driver as a service:

    ```bash
    bash /data/etc/dbus-mqtt-evcc/install.sh
    ```

    The daemon-tools should start this service automatically within seconds.


## Uninstall

⚠️ If you have multiple instances, ensure you choose the correct one.

```bash
bash /data/etc/dbus-mqtt-evcc/uninstall.sh
```


## Restart

```bash
bash /data/etc/dbus-mqtt-evcc/restart.sh
```


## Debugging

⚠️ If you have multiple instances, ensure you choose the correct one.

Check the logs:

```bash
tail -n 100 -F /data/log/dbus-mqtt-evcc/current | tai64nlocal
```

The service status can be checked with `svstat /service/dbus-mqtt-evcc`.

This will output something like `/service/dbus-mqtt-evcc: up (pid 5845) 185 seconds`.

If the seconds are under 5 then the service crashes and gets restarted all the
time. If you do not see anything in the logs, increase the log level in
`/data/etc/dbus-mqtt-evcc/config.ini` by setting `logging = INFO` or
`logging = DEBUG`.

If the script stops with the message
`dbus.exceptions.NameExistsException: Bus name already exists: com.victronenergy.evcharger.mqtt_evcc_11`
it means the service is still running or another service is using that bus name.

### Read settings

Changed settings can be found on this MQTT path of the Venus OS broker:

```
N/<vrm_id>/evcharger/11/...
```


## Tests

The evcc → dbus translation logic lives in the dependency-free module
`evcc_parser.py` and is covered by unit tests (no dbus/GLib/paho required):

```bash
python3 dbus-mqtt-evcc/test/test_parser.py
```


## Compatibility

This software supports the latest three stable versions of Venus OS. It may also
work on older versions, but this is not guaranteed. It only uses the same
dependencies as the original driver (`paho-mqtt`, bundled, and GLib/dbus from
Venus OS).


## Credits

Based on [mr-manuel/venus-os_dbus-mqtt-ev-charger](https://github.com/mr-manuel/venus-os_dbus-mqtt-ev-charger)
(MIT licence, retained). The dbus registration via `vedbus`/`VeDbusService`, the
VRM instance handling, the daemon-tools service structure and the install
scripts are kept from that project. The evcc MQTT parser replaces the original
single-JSON-topic callback.
