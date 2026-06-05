#!/usr/bin/env python

"""
evcc MQTT -> Victron dbus (com.victronenergy.evcharger) translation logic.

This module is intentionally free of any external dependency (no GLib, no dbus,
no paho). It only contains pure functions and small helper classes so that the
logic can be unit-tested off-device (see test/test_parser.py).

evcc publishes one raw value per sub-topic (NOT a single JSON document), e.g.:

    evcc/loadpoints/1/chargePower       -> "7360"            (W)
    evcc/loadpoints/1/chargeCurrent     -> "16"              (A)
    evcc/loadpoints/1/chargedEnergy     -> "5210.4"          (Wh, session)
    evcc/loadpoints/1/chargeTotalImport -> "176920"          (Wh, lifetime; optional)
    evcc/loadpoints/1/chargeDuration    -> "1340000000000"   (NANOSECONDS)
    evcc/loadpoints/1/connected         -> "true" / "false"
    evcc/loadpoints/1/charging          -> "true" / "false"
    evcc/loadpoints/1/enabled           -> "true" / "false"
    evcc/loadpoints/1/mode              -> "off" | "now" | "minpv" | "pv"
    evcc/loadpoints/1/phasesActive      -> "1" | "2" | "3"
    evcc/loadpoints/1/vehicleSoc        -> "37"              (%)
    evcc/loadpoints/1/chargeCurrents/l1 -> "16"              (A, optional, 3-phase)
"""


# Victron evcharger /Status codes (subset, see com.victronenergy.evcharger)
STATUS_DISCONNECTED = 0
STATUS_CONNECTED = 1
STATUS_CHARGING = 2
STATUS_WAITING_FOR_SUN = 4

# Victron evcharger /Mode codes
MODE_MANUAL = 0
MODE_AUTO = 1

# evcc charge modes that map to Victron "Automatic" (PV controlled)
AUTO_MODES = ("pv", "minpv")

# Raw payloads that must be treated as "no value" and therefore ignored so the
# previously known good value is kept.
INVALID_VALUES = ("", "nil", "null", "none", "-", "nan", "n/a")

# evcc sub-topic keys that carry a boolean
BOOL_KEYS = ("connected", "charging", "enabled")

# evcc sub-topic keys that carry a free-form string we keep as-is
STRING_KEYS = ("mode",)


def _is_invalid(raw):
    return raw is None or str(raw).strip().lower() in INVALID_VALUES


def parse_float(raw):
    """Return raw as float, or None if it is missing/invalid."""
    if _is_invalid(raw):
        return None
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return None


def parse_bool(raw):
    """Return raw as bool, or None if it is missing/invalid."""
    if _is_invalid(raw):
        return None
    s = str(raw).strip().lower()
    if s in ("true", "1", "on", "yes"):
        return True
    if s in ("false", "0", "off", "no"):
        return False
    return None


def parse_string(raw):
    """Return raw as a stripped string, or None if it is missing/invalid."""
    if _is_invalid(raw):
        return None
    return str(raw).strip()


def topic_suffix(topic, topic_prefix, loadpoint_id):
    """
    Return the evcc key (sub-topic suffix) for a full MQTT topic, or None if the
    topic does not belong to the configured load point.

    Example: ("evcc/loadpoints/1/chargePower", "evcc", "1") -> "chargePower"
             ("evcc/loadpoints/1/chargeCurrents/l1", ...)    -> "chargeCurrents/l1"
    """
    prefix = "%s/loadpoints/%s/" % (topic_prefix, loadpoint_id)
    if not topic.startswith(prefix):
        return None
    return topic[len(prefix):]


def update_state(state, key, raw):
    """
    Parse a single evcc value and store it in the state dict under `key`.

    Invalid/empty values are ignored: the previous value (if any) is kept and the
    function returns False. On success it returns True.
    """
    if key in BOOL_KEYS:
        value = parse_bool(raw)
    elif key in STRING_KEYS:
        value = parse_string(raw)
    else:
        value = parse_float(raw)

    if value is None:
        return False

    state[key] = value
    return True


def _split_power_by_phase(power, phases, state):
    """
    Return (l1, l2, l3) power distribution.

    phases == 1: everything on L1.
    phases == 3: ventilate using chargeCurrents/l1..l3 if published, otherwise
                 split power equally over the number of active phases.
    """
    if power is None:
        power = 0.0

    if phases != 3:
        return power, 0.0, 0.0

    c1 = state.get("chargeCurrents/l1")
    c2 = state.get("chargeCurrents/l2")
    c3 = state.get("chargeCurrents/l3")

    if None not in (c1, c2, c3) and (c1 + c2 + c3) > 0:
        total = c1 + c2 + c3
        return power * c1 / total, power * c2 / total, power * c3 / total

    # Fallback: split equally over the active phases reported by evcc.
    active = state.get("phasesActive")
    n = int(active) if active in (1, 2, 3) or active in (1.0, 2.0, 3.0) else 3
    if n < 1 or n > 3:
        n = 3
    share = power / n
    distribution = [0.0, 0.0, 0.0]
    for i in range(n):
        distribution[i] = share
    return distribution[0], distribution[1], distribution[2]


def compute_status(connected, charging, mode):
    """Map evcc connected/charging/mode to a Victron /Status code."""
    if not connected:
        return STATUS_DISCONNECTED
    if charging:
        return STATUS_CHARGING
    if mode in AUTO_MODES:
        return STATUS_WAITING_FOR_SUN
    return STATUS_CONNECTED


def compute_dbus_values(state, phases=1, max_current=32, fallback_energy_kwh=0.0):
    """
    Compute the full set of Victron evcharger dbus paths from the accumulated
    evcc state dict. Missing values default to a sane "idle/disconnected" value
    so the service can register and run before any evcc message arrives.

    Returns a dict of {dbus_path: value} with native int/float types only.
    """
    charge_power = state.get("chargePower")
    if charge_power is None:
        charge_power = 0.0

    charge_current = state.get("chargeCurrent")
    if charge_current is None:
        charge_current = 0.0

    connected = bool(state.get("connected", False))
    charging = bool(state.get("charging", False))
    enabled = bool(state.get("enabled", False))
    mode = state.get("mode", "")

    l1, l2, l3 = _split_power_by_phase(charge_power, phases, state)

    # Lifetime energy counter (kWh). Prefer evcc's chargeTotalImport (Wh) if the
    # backing charger exposes it, otherwise use the persisted cumulative value.
    total_import = state.get("chargeTotalImport")
    if total_import is not None:
        energy_forward = total_import / 1000.0
    else:
        energy_forward = fallback_energy_kwh

    # chargeDuration is published in nanoseconds.
    duration_ns = state.get("chargeDuration")
    charging_time = int(round(duration_ns / 1e9)) if duration_ns is not None else 0

    return {
        "/Ac/Power": float(charge_power),
        "/Ac/L1/Power": float(l1),
        "/Ac/L2/Power": float(l2),
        "/Ac/L3/Power": float(l3),
        "/Ac/Energy/Forward": float(energy_forward),
        "/Current": float(charge_current),
        "/SetCurrent": float(charge_current),
        "/MaxCurrent": float(max_current),
        "/ChargingTime": charging_time,
        "/Connected": 1 if connected else 0,
        "/StartStop": 1 if enabled else 0,
        "/Mode": MODE_AUTO if mode in AUTO_MODES else MODE_MANUAL,
        "/Status": compute_status(connected, charging, mode),
    }


class EnergyAccumulator:
    """
    Builds a monotonic lifetime energy counter (Wh) from evcc's per-session
    `chargedEnergy` value, used as a fallback when the charger does not publish
    `chargeTotalImport`.

    evcc's chargedEnergy counts up during a session and resets to 0 when a new
    session starts. We sum the positive deltas so the total survives session
    resets (and, once persisted to disk, driver restarts).
    """

    def __init__(self, total_wh=0.0, last_session_wh=None):
        self.total_wh = float(total_wh)
        self.last_session_wh = last_session_wh

    def update(self, charged_energy_wh):
        """Feed a new chargedEnergy reading (Wh). Returns the running total (Wh)."""
        if charged_energy_wh is None:
            return self.total_wh

        cur = float(charged_energy_wh)
        if self.last_session_wh is None:
            # First reading we ever see: count the energy already in this session.
            delta = cur
        elif cur >= self.last_session_wh:
            delta = cur - self.last_session_wh
        else:
            # Session counter was reset: the new reading is this session's energy.
            delta = cur

        if delta > 0:
            self.total_wh += delta
        self.last_session_wh = cur
        return self.total_wh

    @property
    def total_kwh(self):
        return self.total_wh / 1000.0

    def to_dict(self):
        return {"total_wh": self.total_wh, "last_session_wh": self.last_session_wh}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            return cls()
        return cls(
            total_wh=data.get("total_wh", 0.0),
            last_session_wh=data.get("last_session_wh", None),
        )
