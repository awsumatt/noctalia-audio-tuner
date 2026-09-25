"""--out interop: analyse writes delta-compatible text (wizard pipeline).

Proves the probe -> capture -> analyse --out raw.txt -> delta raw.txt ref.txt
chain end to end without live audio.
"""
import argparse
import math
import os
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tunerlib import analysis  # noqa: E402
from tunerlib.commands import analyse as analyse_cmd  # noqa: E402
from tunerlib.commands import delta as delta_cmd  # noqa: E402

FS = 48000


def write_wav(path, freqs_amps, seconds=2):
    frames = bytearray()
    for n in range(FS * seconds):
        v = sum(a * math.sin(2 * math.pi * f * n / FS) for f, a in freqs_amps)
        s = int(max(-1.0, min(1.0, v)) * 32767)
        frames += struct.pack("<hh", s, s)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(bytes(frames))


class TestAnalyseOutInterop(unittest.TestCase):
    def test_analyse_out_then_delta(self):
        with tempfile.TemporaryDirectory() as td:
            raw_wav = os.path.join(td, "raw.wav")
            ref_wav = os.path.join(td, "ref.wav")
            # Same integer-Hz tones; ref has +3 dB on the second tone.
            write_wav(raw_wav, [(1000, 0.25), (2000, 0.25)])
            write_wav(ref_wav, [(1000, 0.25), (2000, 0.25 * 10 ** (3 / 20))])
            fp = os.path.join(td, "f.txt")
            with open(fp, "w") as fh:
                fh.write("1000\n2000\n")

            raw_args = self._args(analyse_cmd,
                                  ["analyse", "--freqs", fp, "--out",
                                   os.path.join(td, "raw.txt"), raw_wav])
            payload = analyse_cmd.run(raw_args)
            self.assertIn("out", payload)
            levels_txt = analysis.parse_response_text(
                open(payload["out"]).read())
            self.assertEqual(sorted(levels_txt), [1000, 2000])
            for lv in payload["levels"]:
                self.assertAlmostEqual(levels_txt[lv["freq"]],
                                       lv["dbfs"], places=2)

            ref_args = self._args(analyse_cmd,
                                  ["analyse", "--freqs", fp, "--out",
                                   os.path.join(td, "ref.txt"), ref_wav])
            analyse_cmd.run(ref_args)

            d = delta_cmd.run(self._args(
                delta_cmd, ["delta", os.path.join(td, "raw.txt"),
                            os.path.join(td, "ref.txt")]))
            by_freq = {row["freq"]: row["db"] for row in d["delta"]}
            self.assertAlmostEqual(by_freq[1000], 0.0, delta=0.2)
            self.assertAlmostEqual(by_freq[2000], 3.0, delta=0.3)

    @staticmethod
    def _args(cmd, argv):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        cmd.register(sub)
        ns = parser.parse_args(argv)
        if not hasattr(ns, "progress"):
            ns.progress = False
        return ns


if __name__ == "__main__":
    unittest.main()
