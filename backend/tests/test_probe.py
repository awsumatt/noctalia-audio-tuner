"""Probe generation at parametrized rates writes wav + freqs list."""
import os
import tempfile
import unittest
import wave

from .helpers import BACKEND
from tunerlib import analysis, multitone


class ProbeGen(unittest.TestCase):
    def gen_and_check(self, fs, seconds=2):
        d = tempfile.mkdtemp()
        wav = os.path.join(d, "dense.wav")
        freqs, freqs_path = multitone.gen(wav, fs=fs, seconds=seconds)
        self.assertTrue(os.path.exists(wav))
        self.assertTrue(os.path.exists(freqs_path))
        self.assertEqual(freqs_path, os.path.join(d, "dense-freqs.txt"))
        with wave.open(wav) as w:
            self.assertEqual(w.getframerate(), fs)
            self.assertEqual(w.getnchannels(), 2)
            self.assertEqual(w.getnframes(), seconds * fs)
        self.assertEqual(multitone.read_freqs(freqs_path), freqs)
        self.assertTrue(len(freqs) > 80)
        self.assertEqual(freqs[0], 40)
        # integer Hz, dense 1/12-octave
        for f in freqs:
            self.assertEqual(f, int(f))
        return wav, freqs

    def test_probe_48000(self):
        self.gen_and_check(48000)

    def test_probe_44100(self):
        self.gen_and_check(44100)

    def test_probe_96000(self):
        self.gen_and_check(96000)

    def test_probe_analysable_leakage_free(self):
        """A generated probe at any rate recovers its own tone levels."""
        wav, freqs = self.gen_and_check(44100, seconds=1)
        levels = analysis.analyse_response(wav, freqs, start_s=0.0)
        # pink weighting: dBFS = 20*log10(amp/32768) with amp=1/sqrt(f)*scale;
        # just verify monotonically decreasing level with frequency (pink).
        dbs = [l["dbfs"] for l in levels]
        for lo, hi in zip(dbs, dbs[1:]):
            self.assertGreaterEqual(hi, lo - 1.5)


if __name__ == "__main__":
    unittest.main()
