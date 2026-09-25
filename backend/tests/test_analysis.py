"""Analyse recovers known fixture amplitudes; delta math."""
import math
import os
import unittest

from .helpers import BACKEND, FIXTURES
from tunerlib import analysis, multitone


class AnalysisRecovery(unittest.TestCase):
    def check(self, path, tones, fs):
        levels = analysis.analyse_response(path, [f for f, _ in tones],
                                           start_s=0.0)
        for lv, (f, amp) in zip(levels, tones):
            expected = 20 * math.log10(amp)  # float 1.0 == 0 dBFS
            self.assertAlmostEqual(lv["freq"], f)
            self.assertAlmostEqual(lv["dbfs"], expected, delta=0.1,
                                   msg=f"{f} Hz @ {fs}: {lv['dbfs']} "
                                       f"vs {expected:.2f}")

    def test_known_amplitudes_48000(self):
        self.check(os.path.join(FIXTURES, "audio", "mix-48000.wav"),
                   [[220, 0.3], [1000, 0.2], [5000, 0.1]], 48000)

    def test_known_amplitudes_44100(self):
        self.check(os.path.join(FIXTURES, "audio", "mix-44100.wav"),
                   [[441, 0.25], [3000, 0.15]], 44100)

    def test_fixture_manifest_matches(self):
        import json
        manifest = json.load(open(os.path.join(FIXTURES, "audio",
                                               "manifest.json")))
        for name, spec in manifest.items():
            data, fs = analysis.read_mono(os.path.join(FIXTURES, "audio",
                                                       name))
            self.assertEqual(fs, spec["fs"])
            self.assertEqual(len(data), int(spec["fs"] * spec["seconds"]))

    def test_rate_read_from_header_not_hardcoded(self):
        for name, fs in (("mix-48000.wav", 48000), ("mix-44100.wav", 44100)):
            _, got = analysis.read_mono(os.path.join(FIXTURES, "audio", name))
            self.assertEqual(got, fs)

    def test_bin_alignment_enforced(self):
        data, fs = analysis.read_mono(os.path.join(FIXTURES, "audio",
                                                   "mix-48000.wav"))
        with self.assertRaises(Exception):
            # 1000.5 Hz over fs samples is not bin-aligned
            analysis.goertzel_amplitude(data, fs, 1000.5)


class DeltaMath(unittest.TestCase):
    def test_reference_minus_raw(self):
        raw = {100: -30.0, 1000: -20.0, 8000: -40.0}
        ref = {100: -27.0, 1000: -24.0, 8000: -39.5, 12000: -50.0}
        out = analysis.delta(raw, ref)
        self.assertEqual([d["freq"] for d in out], [100, 1000, 8000])
        self.assertEqual([d["db"] for d in out], [3.0, -4.0, 0.5])

    def test_no_overlap_raises(self):
        with self.assertRaises(Exception):
            analysis.delta({100: -30.0}, {200: -30.0})

    def test_text_roundtrip(self):
        levels = analysis.analyse_response(
            os.path.join(FIXTURES, "audio", "mix-48000.wav"),
            [220, 1000, 5000], start_s=0.0)
        parsed = analysis.parse_response_text(analysis.response_text(levels))
        for lv in levels:
            self.assertAlmostEqual(parsed[lv["freq"]], lv["dbfs"], places=2)


class ToneAnalysis(unittest.TestCase):
    def test_pure_tone_thd_near_zero(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "tone.wav")
        multitone.make_tone(path, 200, fs=48000, seconds=1.0, amp=0.25)
        data = analysis.tone_analysis(path, 200)
        self.assertEqual(data["fs"], 48000)
        self.assertAlmostEqual(data["fundamental_dbfs"],
                               20 * math.log10(0.25), delta=0.1)
        self.assertLess(data["thd_pct"], 0.1)


if __name__ == "__main__":
    unittest.main()
