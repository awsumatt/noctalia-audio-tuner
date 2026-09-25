"""SA2 tests: fit (port of fit-eq.py) — synthetic convergence + payload shape."""
import cmath
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tunerlib import TunerError, fit as F  # noqa: E402

FS = 48000
FREQS = [float(f) for f in range(40, 12001, 50)]


def _layout_midpoints():
    _, layout = F.load_layout("default")
    return [[k, math.exp((math.log(a) + math.log(b)) / 2), (qa + qb) / 2,
             0.0 if k == "highpass" else (ga + gb) / 2]
            for (k, (a, b), (qa, qb), (ga, gb)) in layout]


class TestFitConvergence(unittest.TestCase):
    def test_converges_to_known_biquads_under_1db_rms(self):
        truth = _layout_midpoints()
        zs = [cmath.exp(-2j * math.pi * f / FS) for f in FREQS]
        target = [3.5 + r for r in F.chain_response_db(truth, zs, FS)]
        err, secs, gain = F.fit_curve(FREQS, target, FS, F.load_layout(
            "default")[1], restarts=2)
        self.assertLessEqual(math.sqrt(err), 1.0,
                             "fit must reach <= 1 dB weighted RMS on a "
                             "synthetic target built from the same layout")

    def test_rate_is_a_parameter(self):
        # Same chain at a different rate must give a different response and
        # the fit must respect the passed rate.
        truth = _layout_midpoints()
        zs48 = [cmath.exp(-2j * math.pi * f / FS) for f in FREQS]
        zs44 = [cmath.exp(-2j * math.pi * f / 44100) for f in FREQS]
        t48 = F.chain_response_db(truth, zs48, FS)
        t44 = F.chain_response_db(truth, zs44, 44100)
        self.assertNotAlmostEqual(t48[-1], t44[-1], places=3,
                                  msg="rate must change the response")
        err, _secs, _gain = F.fit_curve(FREQS, t44, 44100,
                                        F.load_layout("default")[1],
                                        restarts=2)
        self.assertLessEqual(math.sqrt(err), 1.0)


class TestFitPayloadAndText(unittest.TestCase):
    def _payload(self, tmpdir):
        path = os.path.join(tmpdir, "target.txt")
        truth = _layout_midpoints()
        zs = [cmath.exp(-2j * math.pi * f / FS) for f in FREQS]
        rows = "\n".join(f"{f:.0f} {3.5 + r:.4f}" for f, r in
                         zip(FREQS, F.chain_response_db(truth, zs, FS)))
        with open(path, "w") as fh:
            fh.write(rows + "\n")
        return F.fit_to_payload(path, fs=FS, layout_name="default", restarts=2)

    def test_payload_fields(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self._payload(d)
            for key in ("rate", "weighted_rms_error_db", "magnitude_rms_db",
                        "bass_group_delay_swing_ms", "global_gain_db",
                        "linear_gain", "sections", "fit_text"):
                self.assertIn(key, p)
            self.assertEqual(p["rate"], FS)
            self.assertLessEqual(p["weighted_rms_error_db"], 1.0)
            self.assertEqual(len(p["sections"]), 13)
            self.assertIsInstance(p["bass_group_delay_swing_ms"], float)

    def test_fit_text_round_trips_through_generator_parser(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self._payload(d)
            secs, lin = F.parse_fit_text(p["fit_text"])
            self.assertEqual(len(secs), len(p["sections"]))
            self.assertAlmostEqual(lin, p["linear_gain"], places=4)

    def test_missing_target_raises_tuner_error(self):
        with self.assertRaises(TunerError) as ctx:
            F.fit_to_payload("/nonexistent/target.txt")
        self.assertEqual(ctx.exception.code, "no_response_file")

    def test_unknown_layout_raises_tuner_error(self):
        with self.assertRaises(TunerError) as ctx:
            F.load_layout("does-not-exist")
        self.assertEqual(ctx.exception.code, "no_layout")

    def test_bass_group_delay_zero_for_identity(self):
        # An allpass-free flat chain (gain-only) must have ~0 group delay.
        self.assertAlmostEqual(F.bass_group_delay_swing_ms(
            [], FS), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
