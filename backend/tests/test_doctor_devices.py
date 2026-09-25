"""doctor/devices output shapes — real tools for doctor, faked pactl for devices."""
import json
import os
import unittest
from unittest import mock

from .helpers import BACKEND, FIXTURES
from tunerlib import discovery
from tunerlib.commands import doctor, devices


def fake_run_tool_factory(sinks_json, sources_json, defaults=("s1", "mic1")):
    def fake_run_tool(argv, code="tool_failed", context=""):
        cmd = argv[1:]
        if cmd == ["-f", "json", "list", "sinks"]:
            return sinks_json
        if cmd == ["-f", "json", "list", "sources"]:
            return sources_json
        if argv[0] == "pactl" and argv[1] == "get-default-sink":
            return defaults[0] + "\n"
        if argv[0] == "pactl" and argv[1] == "get-default-source":
            return defaults[1] + "\n"
        raise AssertionError(f"unexpected argv {argv}")
    return fake_run_tool


class DoctorShape(unittest.TestCase):
    def test_real_environment_shape(self):
        data = doctor.run(type("A", (), {"progress": False})())
        self.assertIsInstance(data["ok"], bool)
        self.assertIsInstance(data["missing"], list)
        names = [d["name"] for d in data["deps"]]
        for expected in ("python3", "ffmpeg", "mpv", "pactl or wpctl",
                         "lsp-plugins-lv2"):
            self.assertIn(expected, names)
        for d in data["deps"]:
            self.assertIsInstance(d["ok"], bool)
            self.assertIsInstance(d["hint"], str)
            self.assertIn(d["required"], (True, False))
        # ok must be consistent with missing; mpv is genuinely absent here
        self.assertEqual(data["ok"], not data["missing"])
        if not data["ok"]:
            self.assertIn("mpv", data["missing"])

    def test_missing_required_reported(self):
        with mock.patch.object(doctor, "_which", return_value=None):
            data = doctor.run(type("A", (), {"progress": False})())
        self.assertFalse(data["ok"])
        self.assertIn("ffmpeg", data["missing"])

    def test_wpctl_satisfies_audio_control(self):
        real_which = doctor._which
        def which(name):
            if name == "pactl":
                return None
            if name == "wpctl":
                return "/usr/bin/wpctl"
            return real_which(name)
        with mock.patch.object(doctor, "_which", side_effect=which):
            data = doctor.run(type("A", (), {"progress": False})())
        entry = next(d for d in data["deps"] if d["name"] == "pactl or wpctl")
        self.assertTrue(entry["ok"])


class DevicesShape(unittest.TestCase):
    def load(self, profile_name, default_sink=None, default_source=None):
        prof = json.load(open(os.path.join(FIXTURES, "devices",
                                           profile_name)))
        sink = json.dumps([prof["pactl_sink"]])
        source = json.dumps([prof["pactl_source"]])
        dfl = (default_sink or prof["pactl_sink"]["name"],
               default_source or prof["pactl_source"]["name"])
        with mock.patch.object(discovery, "run_tool",
                               fake_run_tool_factory(sink, source, dfl)):
            return discovery.devices()

    def test_laptop_profile(self):
        dev = self.load("laptop-speakers-48k.json")
        self.assertEqual(len(dev["sinks"]), 1)
        s = dev["sinks"][0]
        self.assertEqual(s["rate"], 48000)
        self.assertEqual(s["channels"], 2)
        self.assertEqual(s["kind"], "hardware")
        self.assertTrue(s["default"])
        self.assertTrue(s["speaker_candidate"])
        self.assertEqual(dev["speaker_sink"], s["name"])
        self.assertEqual(dev["default_sink"], s["name"])

    def test_usb_profile_different_rate_channels(self):
        dev = self.load("usb-interface-6ch-44100.json")
        s = dev["sinks"][0]
        self.assertEqual(s["rate"], 44100)
        self.assertEqual(s["channels"], 6)
        self.assertTrue(s["kind"] == "hardware")
        self.assertFalse(s["speaker_candidate"], "Line port is not a speaker")
        self.assertIsNone(dev["speaker_sink"])

    def test_not_default_when_other_default(self):
        dev = self.load("laptop-speakers-48k.json",
                        default_sink="something-else")
        self.assertFalse(dev["sinks"][0]["default"])
        self.assertEqual(dev["default_sink"], "something-else")

    def test_monitor_source_classification(self):
        src = json.dumps([{
            "index": 9, "name": "x.monitor", "description": "Monitor",
            "monitor_of_sink": "alsa_output.x",
            "properties": {"device.class": "monitor"}}])
        with mock.patch.object(
                discovery, "run_tool",
                fake_run_tool_factory("[]", src, ("s", "mic"))):
            dev = discovery.devices()
        self.assertEqual(dev["sources"][0]["kind"], "monitor")
        self.assertEqual(dev["sources"][0]["monitor_of"], "alsa_output.x")

    def test_no_vendor_regex_anywhere(self):
        import inspect
        src = inspect.getsource(discovery)
        self.assertNotIn("import re", src)
        self.assertNotIn(".search(", src)
        self.assertNotIn(".match(", src)
        self.assertNotIn(".findall(", src)


class SinkRate(unittest.TestCase):
    def test_rate_from_sample_spec(self):
        sink = json.dumps([{"index": 1, "name": "s1",
                            "sample_specification": "float32le 6ch 44100Hz"}])
        with mock.patch.object(
                discovery, "run_tool",
                fake_run_tool_factory(sink, "[]")):
            self.assertEqual(discovery.sink_rate("s1"), 44100)

    def test_unknown_sink_raises(self):
        with mock.patch.object(
                discovery, "run_tool",
                fake_run_tool_factory("[]", "[]")):
            with self.assertRaises(Exception):
                discovery.sink_rate("nope")


if __name__ == "__main__":
    unittest.main()
