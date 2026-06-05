#!/usr/bin/env python

from gi.repository import GLib  # pyright: ignore[reportMissingImports]
import platform
import logging
import sys
import os
from time import sleep, time
import json
import configparser  # for config/ini file
import _thread

# import external packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext"))
import paho.mqtt.client as mqtt

# import Victron Energy packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService  # noqa: E402
from ve_utils import get_vrm_portal_id  # noqa: E402

# import the (dependency-free) evcc translation logic
from evcc_parser import (  # noqa: E402
    topic_suffix,
    update_state,
    compute_dbus_values,
    parse_float,
    EnergyAccumulator,
    STATUS_DISCONNECTED,
)


# get values from config.ini file
try:
    config_file = (os.path.dirname(os.path.realpath(__file__))) + "/config.ini"
    if os.path.exists(config_file):
        config = configparser.ConfigParser()
        config.read(config_file)
        if config["MQTT"]["broker_address"] == "IP_ADDR_OR_FQDN":
            print('ERROR:The "config.ini" is using invalid default values like IP_ADDR_OR_FQDN. The driver restarts in 60 seconds.')
            sleep(60)
            sys.exit()
    else:
        print('ERROR:The "' + config_file + '" is not found. Did you copy or rename the "config.sample.ini" to "config.ini"? The driver restarts in 60 seconds.')
        sleep(60)
        sys.exit()

except Exception:
    exception_type, exception_object, exception_traceback = sys.exc_info()
    file = exception_traceback.tb_frame.f_code.co_filename
    line = exception_traceback.tb_lineno
    print(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
    print("ERROR:The driver restarts in 60 seconds.")
    sleep(60)
    sys.exit()


# Get logging level from config.ini
# ERROR = shows errors only
# WARNING = shows ERROR and warnings
# INFO = shows WARNING and running functions
# DEBUG = shows INFO and data/values
if "DEFAULT" in config and "logging" in config["DEFAULT"]:
    if config["DEFAULT"]["logging"] == "DEBUG":
        logging.basicConfig(level=logging.DEBUG)
    elif config["DEFAULT"]["logging"] == "INFO":
        logging.basicConfig(level=logging.INFO)
    elif config["DEFAULT"]["logging"] == "ERROR":
        logging.basicConfig(level=logging.ERROR)
    else:
        logging.basicConfig(level=logging.WARNING)
else:
    logging.basicConfig(level=logging.WARNING)


# ----- read evcc / driver specific settings from config.ini -----

def config_get(section, key, default):
    if section in config and key in config[section]:
        return config[section][key]
    return default


# watchdog timeout in seconds (0 = disabled)
timeout = int(config_get("DEFAULT", "timeout", "120"))

# evcc topic structure: <topic_prefix>/loadpoints/<loadpoint_id>/<key>
topic_prefix = config_get("MQTT", "topic_prefix", "evcc")
loadpoint_id = config_get("MQTT", "loadpoint_id", "1")

# wiring: 1 = single phase (default, Lektrico 1P7K), 3 = three phase
phases = int(config_get("DEFAULT", "phases", "1"))

# hardware max current (A), used for /MaxCurrent
max_current = float(config_get("DEFAULT", "max_current", "32"))


# persistent energy fallback (used when evcc does not publish chargeTotalImport)
energy_state_file = (os.path.dirname(os.path.realpath(__file__))) + "/energy_state.json"


def load_energy_accumulator():
    try:
        if os.path.exists(energy_state_file):
            with open(energy_state_file, "r") as f:
                return EnergyAccumulator.from_dict(json.load(f))
    except Exception as e:
        logging.warning("Could not read energy state file, starting from zero: %s" % e)
    return EnergyAccumulator()


def save_energy_accumulator(acc):
    try:
        tmp = energy_state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(acc.to_dict(), f)
        os.rename(tmp, energy_state_file)
    except Exception as e:
        logging.warning("Could not persist energy state file: %s" % e)


# ----- shared runtime state -----
mqtt_connected = 0
last_changed = 0
last_updated = 0

# accumulated last-known value per evcc key (suffix of the topic)
evcc_state = {}

# lifetime energy fallback accumulator
energy_acc = load_energy_accumulator()


# formatting
def _a(p, v):
    return str("%.1f" % v) + "A"


def _n(p, v):
    return str("%i" % v)


def _s(p, v):
    return str("%s" % v)


def _w(p, v):
    return str("%i" % v) + "W"


def _kwh(p, v):
    return str("%.2f" % v) + "kWh"


# dbus paths with their initial (idle / disconnected) values and text formatters.
# The service registers immediately with these defaults and gets populated as
# evcc messages arrive - no "minimum message" is required to start.
ev_charger_dict = {
    "/Ac/Power": {"value": 0, "textformat": _w},
    "/Ac/L1/Power": {"value": 0, "textformat": _w},
    "/Ac/L2/Power": {"value": 0, "textformat": _w},
    "/Ac/L3/Power": {"value": 0, "textformat": _w},
    "/Ac/Energy/Forward": {"value": round(energy_acc.total_kwh, 2), "textformat": _kwh},
    "/Current": {"value": 0, "textformat": _a},
    "/SetCurrent": {"value": 0, "textformat": _a},
    "/MaxCurrent": {"value": max_current, "textformat": _a},
    "/ChargingTime": {"value": 0, "textformat": _n},
    "/Connected": {"value": 0, "textformat": _n},
    "/StartStop": {"value": 0, "textformat": _n},
    "/Mode": {"value": 0, "textformat": _n},
    "/Status": {"value": STATUS_DISCONNECTED, "textformat": _n},
}

# A single-phase charger has no L2/L3. Venus OS infers the number of phases from
# which /Ac/Lx/Power paths exist (not their value), so only expose the ones in
# use - otherwise the GUI shows the charger as three-phase.
if phases != 3:
    del ev_charger_dict["/Ac/L2/Power"]
    del ev_charger_dict["/Ac/L3/Power"]


"""
com.victronenergy.evcharger -- relevant paths used by this driver

/Ac/Power                  <-- AC Power (W)
/Ac/L1/Power               <-- L1 Power used (W)
/Ac/L2/Power               <-- L2 Power used (W)
/Ac/L3/Power               <-- L3 Power used (W)
/Ac/Energy/Forward         <-- Charged energy, lifetime counter (kWh)
/Current                   <-- Actual charging current (A)
/SetCurrent                <-- Charging current set point (A)
/MaxCurrent                <-- Max charging current (A)
/ChargingTime              <-- Session charging time (seconds)
/Connected                 <-- 0 = Disconnected, 1 = EV connected
/StartStop                 <-- 0 = charging disabled, 1 = charging enabled
/Mode                      <-- 0 = Manual, 1 = Automatic
/Status                    <-- 0 = Disconnected, 1 = Connected, 2 = Charging,
                               4 = Waiting for sun

NOTE: This driver is read/display only. evcc remains the sole master of the
charger; values written on dbus are NOT propagated back to evcc.
"""


def recompute_dbus_values():
    """Recompute every dbus value from the accumulated evcc state."""
    values = compute_dbus_values(
        evcc_state,
        phases=phases,
        max_current=max_current,
        fallback_energy_kwh=energy_acc.total_kwh,
    )
    for path, value in values.items():
        if path in ev_charger_dict:
            ev_charger_dict[path]["value"] = value


# MQTT requests
def on_disconnect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    logging.warning("MQTT client: Got disconnected")
    if reason_code != 0:
        logging.warning("MQTT client: Unexpected MQTT disconnection. Will auto-reconnect")
    else:
        logging.warning("MQTT client: reason_code value:" + str(reason_code))

    while mqtt_connected == 0:
        try:
            logging.warning(f"MQTT client: Trying to reconnect to broker {config['MQTT']['broker_address']} on port {config['MQTT']['broker_port']}")
            client.connect(host=config["MQTT"]["broker_address"], port=int(config["MQTT"]["broker_port"]))
            mqtt_connected = 1
        except Exception as err:
            logging.error(f"MQTT client: Error in retrying to connect with broker ({config['MQTT']['broker_address']}:{config['MQTT']['broker_port']}): {err}")
            logging.error("MQTT client: Retrying in 15 seconds")
            mqtt_connected = 0
            sleep(15)


def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    if reason_code == 0:
        logging.info("MQTT client: Connected to MQTT broker!")
        mqtt_connected = 1
        subscribe_topic = "%s/loadpoints/%s/#" % (topic_prefix, loadpoint_id)
        logging.info('MQTT client: Subscribing to "%s"' % subscribe_topic)
        client.subscribe(subscribe_topic)
    else:
        logging.error("MQTT client: Failed to connect, return code %d\n", reason_code)


def on_message(client, userdata, msg):
    try:
        global evcc_state, last_changed, energy_acc

        key = topic_suffix(msg.topic, topic_prefix, loadpoint_id)
        if key is None:
            return

        payload = msg.payload.decode("utf-8") if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload)
        payload = payload.strip()

        # store/keep the value; invalid payloads (nil/null/empty/...) are ignored
        if not update_state(evcc_state, key, payload):
            logging.debug('Ignored evcc key "%s" with invalid value "%s"' % (key, payload))
            return

        logging.debug('evcc "%s" = %s' % (key, evcc_state[key]))

        # energy fallback: accumulate session chargedEnergy into a lifetime total
        # only useful when evcc does not publish chargeTotalImport
        if key == "chargedEnergy" and "chargeTotalImport" not in evcc_state:
            value = parse_float(payload)
            if value is not None:
                energy_acc.update(value)
                save_energy_accumulator(energy_acc)

        recompute_dbus_values()
        last_changed = int(time())

    except Exception:
        exception_type, exception_object, exception_traceback = sys.exc_info()
        file = exception_traceback.tb_frame.f_code.co_filename
        line = exception_traceback.tb_lineno
        logging.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
        logging.debug("MQTT topic: %s payload: %s" % (msg.topic, str(msg.payload)))


class DbusMqttEvccService:
    def __init__(
        self,
        servicename,
        deviceinstance,
        paths,
        productname="evcc Charger",
        customname="evcc Charger",
        connection="evcc MQTT service",
    ):

        self._dbusservice = VeDbusService(servicename, register=False)
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            "Unkown version, and running on Python " + platform.python_version(),
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/FirmwareVersion", "0.1.1 (20260606)")
        # self._dbusservice.add_path('/HardwareVersion', '')

        self._dbusservice.add_path("/Position", int(config_get("DEFAULT", "position", "0")))

        self._dbusservice.add_path("/Latency", None)

        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["value"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        # register VeDbusService after all paths where added
        self._dbusservice.register()

        GLib.timeout_add(1000, self._update)  # pause 1000ms before the next request

    def _update(self):

        global ev_charger_dict, last_changed, last_updated

        now = int(time())

        # watchdog: if evcc/charger went silent, do not keep stale values alive.
        # Force the charger to "disconnected" with no power.
        if timeout != 0 and last_changed != 0 and (now - last_changed) > timeout:
            if ev_charger_dict["/Status"]["value"] != STATUS_DISCONNECTED or ev_charger_dict["/Ac/Power"]["value"] != 0:
                logging.warning("Watchdog: no evcc message for %i seconds, forcing Status=Disconnected and Power=0." % (now - last_changed))
            ev_charger_dict["/Status"]["value"] = STATUS_DISCONNECTED
            ev_charger_dict["/Connected"]["value"] = 0
            ev_charger_dict["/Current"]["value"] = 0
            for power_path in ("/Ac/Power", "/Ac/L1/Power", "/Ac/L2/Power", "/Ac/L3/Power"):
                if power_path in ev_charger_dict:
                    ev_charger_dict[power_path]["value"] = 0
            last_changed = 0  # mark watchdog handled, avoid re-logging every second

        if last_changed != last_updated:

            for setting, data in ev_charger_dict.items():

                try:
                    self._dbusservice[setting] = data["value"]

                except TypeError as e:
                    logging.error('Received key "' + setting + '" with value "' + str(data["value"]) + '" is not valid: ' + str(e))

                except Exception:
                    exception_type, exception_object, exception_traceback = sys.exc_info()
                    file = exception_traceback.tb_frame.f_code.co_filename
                    line = exception_traceback.tb_lineno
                    logging.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")

            logging.info("Data: {:.2f} W, status {}".format(ev_charger_dict["/Ac/Power"]["value"], ev_charger_dict["/Status"]["value"]))

            last_updated = last_changed

        # increment UpdateIndex - to show that new data is available
        index = self._dbusservice["/UpdateIndex"] + 1  # increment index
        if index > 255:  # maximum value of the index
            index = 0  # overflow from 255 to 0
        self._dbusservice["/UpdateIndex"] = index
        return True

    def _handlechangedvalue(self, path, value):
        # Read/display only: evcc stays the master, we do not push changes back.
        logging.debug("someone else updated %s to %s (ignored, driver is read-only)" % (path, value))
        return True  # accept the change locally so the UI stays responsive


def main():
    _thread.daemon = True  # allow the program to quit

    from dbus.mainloop.glib import (
        DBusGMainLoop,
    )  # pyright: ignore[reportMissingImports]

    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    # MQTT setup
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="MqttEvcc_" + get_vrm_portal_id() + "_" + str(config["DEFAULT"]["device_instance"]))
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect
    client.on_message = on_message

    # check tls and use settings, if provided
    if "tls_enabled" in config["MQTT"] and config["MQTT"]["tls_enabled"] == "1":
        logging.info("MQTT client: TLS is enabled")

        if "tls_path_to_ca" in config["MQTT"] and config["MQTT"]["tls_path_to_ca"] != "":
            logging.info('MQTT client: TLS: custom ca "%s" used' % config["MQTT"]["tls_path_to_ca"])
            client.tls_set(config["MQTT"]["tls_path_to_ca"], tls_version=2)
        else:
            client.tls_set(tls_version=2)

        if "tls_insecure" in config["MQTT"] and config["MQTT"]["tls_insecure"] != "":
            logging.info("MQTT client: TLS certificate server hostname verification disabled")
            client.tls_insecure_set(True)

    # check if username and password are set
    if "username" in config["MQTT"] and "password" in config["MQTT"] and config["MQTT"]["username"] != "" and config["MQTT"]["password"] != "":
        logging.info('MQTT client: Using username "%s" and password to connect' % config["MQTT"]["username"])
        client.username_pw_set(username=config["MQTT"]["username"], password=config["MQTT"]["password"])

    # connect to broker
    logging.info(f"MQTT client: Connecting to broker {config['MQTT']['broker_address']} on port {config['MQTT']['broker_port']}")
    client.connect(host=config["MQTT"]["broker_address"], port=int(config["MQTT"]["broker_port"]))
    client.loop_start()

    # The service registers immediately with idle defaults and is populated as
    # evcc messages arrive. No "minimum message" is required to start.
    paths_dbus = {
        "/UpdateIndex": {"value": 0, "textformat": _n},
    }
    paths_dbus.update(ev_charger_dict)

    DbusMqttEvccService(
        servicename="com.victronenergy.evcharger.mqtt_evcc_" + str(config["DEFAULT"]["device_instance"]),
        deviceinstance=int(config["DEFAULT"]["device_instance"]),
        customname=config["DEFAULT"]["device_name"],
        paths=paths_dbus,
    )

    logging.info("Connected to dbus and switching over to GLib.MainLoop() (= event based)")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
