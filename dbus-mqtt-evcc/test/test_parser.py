#!/usr/bin/env python3

"""
Unit tests for the evcc -> Victron dbus translation logic.

These tests simulate evcc MQTT messages (topic/payload pairs, including invalid
payloads and the nanosecond chargeDuration) and verify the computed dbus path
values: /Status, unit conversions and the phase power distribution.

Run with:  python3 test/test_parser.py
(no external dependencies, no dbus/GLib/paho needed)
"""

import os
import sys
import unittest

# make the driver's evcc_parser importable when run from anywhere
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from evcc_parser import (  # noqa: E402
    parse_float,
    parse_bool,
    topic_suffix,
    update_state,
    compute_dbus_values,
    compute_status,
    EnergyAccumulator,
    STATUS_DISCONNECTED,
    STATUS_CONNECTED,
    STATUS_CHARGING,
    STATUS_WAITING_FOR_SUN,
    MODE_MANUAL,
    MODE_AUTO,
)


def feed(state, prefix, loadpoint, topic_payload_pairs):
    """Helper: replay a list of (topic, payload) evcc messages into state."""
    for topic, payload in topic_payload_pairs:
        key = topic_suffix(topic, prefix, loadpoint)
        if key is None:
            continue
        update_state(state, key, payload)


class TestValueParsing(unittest.TestCase):
    def test_parse_float_valid(self):
        self.assertEqual(parse_float("7360"), 7360.0)
        self.assertEqual(parse_float("5210.4"), 5210.4)
        self.assertEqual(parse_float(" 16 "), 16.0)

    def test_parse_float_invalid(self):
        for bad in ("nil", "null", "none", "None", "NULL", "-", "", "n/a", None, "abc"):
            self.assertIsNone(parse_float(bad), "expected None for %r" % bad)

    def test_parse_bool(self):
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("True"))
        self.assertFalse(parse_bool("false"))
        self.assertIsNone(parse_bool("nil"))
        self.assertIsNone(parse_bool(""))


class TestTopicSuffix(unittest.TestCase):
    def test_simple_key(self):
        self.assertEqual(topic_suffix("evcc/loadpoints/1/chargePower", "evcc", "1"), "chargePower")

    def test_nested_key(self):
        self.assertEqual(topic_suffix("evcc/loadpoints/1/chargeCurrents/l1", "evcc", "1"), "chargeCurrents/l1")

    def test_other_loadpoint_ignored(self):
        self.assertIsNone(topic_suffix("evcc/loadpoints/2/chargePower", "evcc", "1"))

    def test_other_prefix_ignored(self):
        self.assertIsNone(topic_suffix("foo/loadpoints/1/chargePower", "evcc", "1"))


class TestUpdateStateIgnoresInvalid(unittest.TestCase):
    def test_invalid_keeps_previous_value(self):
        state = {}
        self.assertTrue(update_state(state, "chargePower", "7360"))
        self.assertEqual(state["chargePower"], 7360.0)
        # a "nil" must NOT overwrite the previously known good value
        self.assertFalse(update_state(state, "chargePower", "nil"))
        self.assertEqual(state["chargePower"], 7360.0)
        # empty payload is also ignored
        self.assertFalse(update_state(state, "chargePower", ""))
        self.assertEqual(state["chargePower"], 7360.0)


class TestUnitConversions(unittest.TestCase):
    def test_charge_duration_ns_to_seconds(self):
        state = {}
        # 1340000000000 ns -> 1340 s
        update_state(state, "chargeDuration", "1340000000000")
        values = compute_dbus_values(state)
        self.assertEqual(values["/ChargingTime"], 1340)

    def test_total_import_wh_to_kwh(self):
        state = {}
        update_state(state, "chargeTotalImport", "176920")
        values = compute_dbus_values(state)
        self.assertAlmostEqual(values["/Ac/Energy/Forward"], 176.920)

    def test_energy_fallback_used_when_total_import_absent(self):
        state = {}
        values = compute_dbus_values(state, fallback_energy_kwh=12.5)
        self.assertAlmostEqual(values["/Ac/Energy/Forward"], 12.5)


class TestStatusLogic(unittest.TestCase):
    def test_disconnected(self):
        self.assertEqual(compute_status(False, False, "pv"), STATUS_DISCONNECTED)

    def test_connected_not_charging_manual(self):
        self.assertEqual(compute_status(True, False, "now"), STATUS_CONNECTED)

    def test_connected_charging(self):
        self.assertEqual(compute_status(True, True, "pv"), STATUS_CHARGING)

    def test_connected_waiting_for_sun(self):
        self.assertEqual(compute_status(True, False, "pv"), STATUS_WAITING_FOR_SUN)
        self.assertEqual(compute_status(True, False, "minpv"), STATUS_WAITING_FOR_SUN)

    def test_status_from_messages(self):
        state = {}
        feed(state, "evcc", "1", [
            ("evcc/loadpoints/1/connected", "true"),
            ("evcc/loadpoints/1/charging", "true"),
            ("evcc/loadpoints/1/mode", "pv"),
        ])
        values = compute_dbus_values(state)
        self.assertEqual(values["/Status"], STATUS_CHARGING)
        self.assertEqual(values["/Connected"], 1)


class TestModeMapping(unittest.TestCase):
    def test_pv_modes_are_auto(self):
        for m in ("pv", "minpv"):
            self.assertEqual(compute_dbus_values({"mode": m})["/Mode"], MODE_AUTO)

    def test_other_modes_are_manual(self):
        for m in ("off", "now", ""):
            self.assertEqual(compute_dbus_values({"mode": m})["/Mode"], MODE_MANUAL)


class TestPhaseDistribution(unittest.TestCase):
    def test_single_phase_all_on_l1(self):
        state = {"chargePower": 7360.0}
        values = compute_dbus_values(state, phases=1)
        self.assertEqual(values["/Ac/L1/Power"], 7360.0)
        self.assertEqual(values["/Ac/L2/Power"], 0.0)
        self.assertEqual(values["/Ac/L3/Power"], 0.0)

    def test_three_phase_equal_split_by_active(self):
        state = {"chargePower": 9000.0, "phasesActive": 3.0}
        values = compute_dbus_values(state, phases=3)
        self.assertAlmostEqual(values["/Ac/L1/Power"], 3000.0)
        self.assertAlmostEqual(values["/Ac/L2/Power"], 3000.0)
        self.assertAlmostEqual(values["/Ac/L3/Power"], 3000.0)

    def test_three_phase_split_by_two_active(self):
        state = {"chargePower": 4000.0, "phasesActive": 2.0}
        values = compute_dbus_values(state, phases=3)
        self.assertAlmostEqual(values["/Ac/L1/Power"], 2000.0)
        self.assertAlmostEqual(values["/Ac/L2/Power"], 2000.0)
        self.assertAlmostEqual(values["/Ac/L3/Power"], 0.0)

    def test_three_phase_ventilate_by_currents(self):
        # currents 10 / 6 / 4 (total 20) -> power split 50/30/20 %
        state = {
            "chargePower": 10000.0,
            "phasesActive": 3.0,
            "chargeCurrents/l1": 10.0,
            "chargeCurrents/l2": 6.0,
            "chargeCurrents/l3": 4.0,
        }
        values = compute_dbus_values(state, phases=3)
        self.assertAlmostEqual(values["/Ac/L1/Power"], 5000.0)
        self.assertAlmostEqual(values["/Ac/L2/Power"], 3000.0)
        self.assertAlmostEqual(values["/Ac/L3/Power"], 2000.0)


class TestDefaultsBeforeAnyMessage(unittest.TestCase):
    def test_empty_state_is_idle_disconnected(self):
        values = compute_dbus_values({}, phases=1, max_current=32)
        self.assertEqual(values["/Status"], STATUS_DISCONNECTED)
        self.assertEqual(values["/Ac/Power"], 0.0)
        self.assertEqual(values["/Connected"], 0)
        self.assertEqual(values["/StartStop"], 0)
        self.assertEqual(values["/MaxCurrent"], 32.0)
        self.assertEqual(values["/ChargingTime"], 0)

    def test_native_types_only(self):
        state = {}
        feed(state, "evcc", "1", [
            ("evcc/loadpoints/1/chargePower", "7360"),
            ("evcc/loadpoints/1/chargeCurrent", "16"),
        ])
        values = compute_dbus_values(state)
        for path, value in values.items():
            self.assertNotIsInstance(value, str, "%s should not be a string" % path)


class TestCurrentAndSetCurrent(unittest.TestCase):
    def test_current_and_setcurrent_track_charge_current(self):
        state = {}
        update_state(state, "chargeCurrent", "16")
        values = compute_dbus_values(state)
        self.assertEqual(values["/Current"], 16.0)
        self.assertEqual(values["/SetCurrent"], 16.0)


class TestEnergyAccumulator(unittest.TestCase):
    def test_accumulates_within_session(self):
        acc = EnergyAccumulator()
        self.assertEqual(acc.update(1000.0), 1000.0)
        self.assertEqual(acc.update(2500.0), 2500.0)
        self.assertEqual(acc.update(5210.4), 5210.4)

    def test_survives_session_reset(self):
        acc = EnergyAccumulator()
        acc.update(0.0)
        acc.update(5000.0)        # session 1: 5 kWh
        # new session starts, counter resets
        acc.update(0.0)
        total = acc.update(3000.0)  # session 2: +3 kWh
        self.assertAlmostEqual(total, 8000.0)
        self.assertAlmostEqual(acc.total_kwh, 8.0)

    def test_persistence_roundtrip(self):
        acc = EnergyAccumulator()
        acc.update(0.0)
        acc.update(4200.0)
        restored = EnergyAccumulator.from_dict(acc.to_dict())
        self.assertAlmostEqual(restored.total_wh, acc.total_wh)
        self.assertEqual(restored.last_session_wh, acc.last_session_wh)
        # continuing after restore keeps accumulating across the "reboot"
        restored.update(0.0)
        total = restored.update(1000.0)
        self.assertAlmostEqual(total, 5200.0)


class TestRealisticScenario(unittest.TestCase):
    def test_full_charging_message_stream(self):
        state = {}
        feed(state, "evcc", "1", [
            ("evcc/loadpoints/1/connected", "true"),
            ("evcc/loadpoints/1/charging", "true"),
            ("evcc/loadpoints/1/enabled", "true"),
            ("evcc/loadpoints/1/mode", "pv"),
            ("evcc/loadpoints/1/chargePower", "7360"),
            ("evcc/loadpoints/1/chargeCurrent", "16"),
            ("evcc/loadpoints/1/chargeDuration", "1340000000000"),
            ("evcc/loadpoints/1/chargeTotalImport", "176920"),
            ("evcc/loadpoints/1/phasesActive", "1"),
            ("evcc/loadpoints/1/vehicleSoc", "37"),
            # a spurious invalid update that must be ignored
            ("evcc/loadpoints/1/chargePower", "nil"),
        ])
        values = compute_dbus_values(state, phases=1, max_current=32)
        self.assertEqual(values["/Status"], STATUS_CHARGING)
        self.assertEqual(values["/Ac/Power"], 7360.0)         # not wiped by "nil"
        self.assertEqual(values["/Ac/L1/Power"], 7360.0)
        self.assertEqual(values["/Current"], 16.0)
        self.assertEqual(values["/SetCurrent"], 16.0)
        self.assertEqual(values["/ChargingTime"], 1340)
        self.assertAlmostEqual(values["/Ac/Energy/Forward"], 176.920)
        self.assertEqual(values["/Mode"], MODE_AUTO)
        self.assertEqual(values["/StartStop"], 1)
        self.assertEqual(values["/Connected"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
