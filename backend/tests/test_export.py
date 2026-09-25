"""SA2 tests: exporters — golden diff vs upstream, limiter invariants, tree."""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tunerlib import export as E  # noqa: E402
from tunerlib import fit as F  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "..", "fixtures", "golden",
                      "filter-chain.expected.conf")
UPSTREAM = os.path.join(os.path.expanduser("~"),
                        "Projects/omarchy-audio-tuner/generate/gen-filter-chain.py")

FIXED_FIT = """# weighted RMS error: 0.42 dB
# global gain: -2.100 dB  (linear 0.7842)
highpass   Freq=   55.0 Q=0.700
peaking    Freq=  100.0 Q=1.200 Gain=+3.00
peaking    Freq=  3000.0 Q=1.500 Gain=-4.00
highshelf  Freq=  8000.0 Q=0.700 Gain=+2.00
"""


def _strip_diff(text):
    """Lines that must match upstream: drop comment lines, normalize the
    node-name prefix (sink name is allowed to differ)."""
    out = []
    sinkre = re.compile(r"omarchy_speaker_tuning|noctalia_audio_tuning")
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        out.append(sinkre.sub("SINK", line))
    return out


class TestGoldenPipeWire(unittest.TestCase):
    """Golden guarantee: our exporter is byte-comparable (modulo comment
    lines and the sink-name prefix) to upstream gen-filter-chain.py.

    The expected output was generated from upstream
    omarchy-audio-tuner/generate/gen-filter-chain.py with the fixture
    backend/fixtures/golden/fit.txt (== FIXED_FIT below) and vendored at
    backend/fixtures/golden/filter-chain.expected.conf, so the invariant is
    enforced in CI, where the upstream checkout does not exist. Regenerate:
    python3 <upstream>/generate/gen-filter-chain.py \\
        backend/fixtures/golden/fit.txt \\
        > backend/fixtures/golden/filter-chain.expected.conf
    """

    def _ours(self):
        return E.pipewire_filter_chain(
            *F.parse_fit_text(FIXED_FIT)[:2],
            prefix="omarchy_speaker_tuning")

    def _assert_golden(self, expected_text):
        exp_lines = _strip_diff(expected_text)
        our_lines = _strip_diff(self._ours())
        self.assertEqual(len(exp_lines), len(our_lines),
                         "line count mismatch (comments excluded)")
        for i, (a, b) in enumerate(zip(exp_lines, our_lines)):
            self.assertEqual(a, b, f"line {i} differs:\n  expected: {a!r}\n"
                                   f"  ours:     {b!r}")
        self.assertGreater(len(exp_lines), 40)

    def test_byte_comparable_to_vendored_golden(self):
        with open(GOLDEN) as fh:
            self._assert_golden(fh.read())

    @unittest.skipUnless(os.path.exists(UPSTREAM),
                         "upstream omarchy-audio-tuner checkout not present")
    def test_byte_comparable_to_live_upstream_when_available(self):
        with tempfile.TemporaryDirectory() as d:
            fit_path = os.path.join(d, "fit.txt")
            with open(fit_path, "w") as fh:
                fh.write(FIXED_FIT)
            up = subprocess.run([sys.executable, UPSTREAM, fit_path],
                                capture_output=True, text=True)
            self.assertEqual(up.returncode, 0, up.stderr)
            self._assert_golden(up.stdout)


class TestExporterInvariants(unittest.TestCase):
    def setUp(self):
        self.conf = E.pipewire_filter_chain(
            *F.parse_fit_text(FIXED_FIT)[:2])

    def test_limiter_present_last_with_alr0_boost0(self):
        self.assertIn("limiter_stereo", self.conf)
        lim_at = self.conf.index("limiter_stereo")
        self.assertGreater(lim_at, self.conf.index('"Gain"'),
                           "limiter must come after the biquads")
        self.assertIn('"alr"   = 0', self.conf)
        self.assertIn('"boost" = 0', self.conf)
        self.assertIn('"th"    = 0.891', self.conf)
        # g_in carries the fit's linear gain
        self.assertIn('"g_in"  = 0.7842', self.conf)
        # explicit per-channel wiring, not a duplicated mono graph
        self.assertIn('"s0_l:In" "s0_r:In"', self.conf)
        self.assertIn("limiter:in_l", self.conf)
        self.assertIn("limiter:in_r", self.conf)
        self.assertIn("limiter:out_l", self.conf)
        self.assertIn("limiter:out_r", self.conf)
        # outputs listed last, after the limiter node
        self.assertGreater(self.conf.index("limiter:out_l"),
                           self.conf.index("limiter_stereo"))

    def test_sink_placeholder_and_default_prefix(self):
        self.assertIn("@SPEAKER_SINK@", self.conf)
        self.assertIn("noctalia_audio_tuning", self.conf)

    def test_custom_prefix_and_sink(self):
        conf = E.pipewire_filter_chain(*F.parse_fit_text(FIXED_FIT)[:2],
                                       prefix="my_tune", sink="alsa_out.X")
        self.assertIn("my_tune", conf)
        self.assertIn("my_tune_output", conf)
        self.assertIn('target.object = "alsa_out.X"', conf)
        self.assertNotIn("@SPEAKER_SINK@", conf)


class TestOmarchyTree(unittest.TestCase):
    def _payload_file(self, d):
        payload = {
            "linear_gain": 0.7842, "global_gain_db": -2.1,
            "magnitude_rms_db": 0.42, "bass_group_delay_swing_ms": 15.9,
            "sections": [{"kind": "highpass", "freq": 55.0, "q": 0.7,
                          "gain_db": 0.0},
                         {"kind": "peaking", "freq": 100.0, "q": 1.2,
                          "gain_db": 3.0}]}
        path = os.path.join(d, "fit-result.json")
        with open(path, "w") as fh:
            json.dump(payload, fh)
        return path

    def test_tree_with_not_measured_markers(self):
        with tempfile.TemporaryDirectory() as d:
            fit_json = self._payload_file(d)
            res = E.export_omarchy(fit_json, "noctalia_audio_tuning",
                                   "@SPEAKER_SINK@",
                                   os.path.join(d, "tuning"))
            with open(res["filter_chain_conf"]) as fh:
                conf = fh.read()
            with open(res["tuning_conf"]) as fh:
                tconf = fh.read()
            self.assertIn("libpipewire-module-filter-chain", conf)
            self.assertIn('"alr"   = 0', conf)
            self.assertIn("magnitude_rms_db = 0.42", tconf)
            self.assertIn("bass_group_delay_swing_ms = 15.90", tconf)
            self.assertIn("limiter_headroom_db = not_measured", tconf)
            self.assertIn("dynamic_range_delta_lu = not_measured", tconf)
            self.assertIsNone(
                re.search(r"limiter_headroom_db\s*=\s*[-\d]", tconf),
                "limiter_headroom_db must never carry a fabricated number")
            self.assertIsNone(
                re.search(r"dynamic_range_delta_lu\s*=\s*[-\d]", tconf),
                "dynamic_range_delta_lu must never carry a fabricated number")

    def test_bare_fit_txt_refuses_to_fabricate_report(self):
        with tempfile.TemporaryDirectory() as d:
            fit_txt = os.path.join(d, "fit.txt")
            with open(fit_txt, "w") as fh:
                fh.write(FIXED_FIT)
            with self.assertRaises(Exception) as ctx:
                E.export_omarchy(fit_txt, "p", "@SPEAKER_SINK@",
                                 os.path.join(d, "t"))
            self.assertIn("magnitude_rms_db", str(ctx.exception))


class TestEasyEffects(unittest.TestCase):
    def test_preset_shape_and_gain(self):
        with tempfile.TemporaryDirectory() as d:
            fit_txt = os.path.join(d, "fit.txt")
            with open(fit_txt, "w") as fh:
                fh.write(FIXED_FIT)
            out = os.path.join(d, "preset.json")
            res = E.export_easyeffects(fit_txt, out_path=out)
            with open(out) as fh:
                on_disk = json.load(fh)
            self.assertEqual(on_disk, res["preset"])
            preset = res["preset"]
            self.assertAlmostEqual(preset["output"]["input-gain"],
                                   20 * math.log10(0.7842), places=1)
            filters = [k for k in preset["output"] if k.startswith("filter#")]
            self.assertEqual(len(filters), 4)
            f0 = preset["output"]["filter#0"]
            self.assertEqual(f0["kind"], "highpass")
            self.assertEqual(f0["frequency"], 55.0)
            self.assertNotIn("gain", f0)  # highpass has no gain control
            f1 = preset["output"]["filter#1"]
            self.assertEqual(f1["gain"], 3.0)


class TestCliContract(unittest.TestCase):
    """The SA2 commands must register through the entry's auto-loader."""

    ENTRY = os.path.join(os.path.dirname(__file__), "..",
                         "noctalia-audio-tuner")

    def _run(self, *cli):
        proc = subprocess.run([sys.executable, self.ENTRY, *cli],
                              capture_output=True, text=True)
        return proc

    def test_fit_generate_tunings_through_entry(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "target.txt")
            with open(target, "w") as fh:
                fh.write(FIXED_FREQS_TXT)
            p1 = self._run("fit", "--restarts", "1", target)
            self.assertEqual(p1.returncode, 0, p1.stderr)
            env = json.loads(p1.stdout)
            self.assertEqual(env["status"], "ok")
            fit_json = os.path.join(d, "fit-result.json")
            with open(fit_json, "w") as fh:
                json.dump(env["data"], fh)
            p2 = self._run("generate", "--target", "pipewire", fit_json)
            self.assertEqual(p2.returncode, 0, p2.stderr)
            gen = json.loads(p2.stdout)
            self.assertEqual(gen["status"], "ok")
            self.assertIn("limiter_stereo", gen["data"]["conf"])
            p3 = self._run("tunings")
            self.assertEqual(p3.returncode, 0, p3.stderr)
            tun = json.loads(p3.stdout)
            self.assertEqual(tun["status"], "ok")
            self.assertIn("tunings", tun["data"])

    def test_generate_bad_fit_is_error_envelope(self):
        proc = self._run("generate", "/nonexistent/fit.txt")
        self.assertEqual(proc.returncode, 1)
        env = json.loads(proc.stdout)
        self.assertEqual(env["status"], "error")
        self.assertEqual(env["error"]["code"], "bad_fit")


FIXED_FREQS_TXT = "\n".join(
    f"{f} {v}" for f, v in [(40, 2.0), (100, 3.0), (500, -1.0),
                            (1000, 0.5), (5000, 2.0), (10000, -3.0)])


if __name__ == "__main__":
    unittest.main()
