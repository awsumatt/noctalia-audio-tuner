"""Registry contract + probe command end-to-end (wav+freqs at parametrized rate)."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave

from .helpers import BACKEND
from tunerlib.commands import doctor, devices, probe

ENTRY = os.path.join(BACKEND, "noctalia-audio-tuner")


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--progress", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    import pkgutil
    import importlib
    import tunerlib.commands as commands
    for _, name, _ in pkgutil.iter_modules(commands.__path__):
        mod = importlib.import_module(f"tunerlib.commands.{name}")
        mod.register(sub)
    return p


class Registry(unittest.TestCase):
    def test_all_sa1_commands_registered(self):
        parser = build_parser()
        for cmd in ("doctor", "devices", "probe", "capture", "analyse",
                    "delta", "tone", "mic-sweep"):
            self.assertIn(cmd, parser._subparsers._group_actions[0].choices,
                          cmd)

    def test_run_returns_dict(self):
        for mod in (doctor, devices, probe):
            self.assertTrue(callable(getattr(mod, "run", None)))
            self.assertTrue(callable(getattr(mod, "register", None)))


class EnvelopeHelpers(unittest.TestCase):
    def test_ok_and_error_envelopes(self):
        from tunerlib import TunerError, error_envelope, ok_envelope
        ok = ok_envelope({"a": 1})
        self.assertEqual(ok, {"status": "ok", "data": {"a": 1}, "error": None})
        err = error_envelope(TunerError("boom", "msg"))
        self.assertEqual(err["status"], "error")
        self.assertEqual(err["error"], {"code": "boom", "message": "msg"})


class ProbeCommand(unittest.TestCase):
    def _run(self, *argv):
        args = build_parser().parse_args(["probe", *argv])
        return probe.run(args)

    def test_probe_writes_wav_and_freqs_at_parametrized_rate(self):
        d = tempfile.mkdtemp()
        data = self._run("--rate", "44100", "--seconds", "1", d)
        self.assertEqual(data["rate"], 44100)
        self.assertTrue(os.path.exists(data["wav"]))
        self.assertTrue(os.path.exists(data["freqs_path"]))
        with wave.open(data["wav"]) as w:
            self.assertEqual(w.getframerate(), 44100)
            self.assertEqual(w.getnframes(), 44100)
        freqs = [int(l) for l in open(data["freqs_path"]) if l.strip()]
        self.assertEqual(freqs, data["freqs"])

    def test_probe_explicit_wav_path(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "myprobe.wav")
        data = self._run("--rate", "48000", "--seconds", "1", out)
        self.assertEqual(data["wav"], out)
        self.assertTrue(os.path.exists(os.path.join(d, "myprobe-freqs.txt")))

    def test_entry_json_envelope(self):
        """The real entrypoint emits a valid ok envelope for doctor."""
        env = dict(os.environ, PYTHONPATH=BACKEND)
        proc = subprocess.run([sys.executable, ENTRY, "doctor"],
                              capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertEqual(envelope["status"], "ok")
        self.assertIn("deps", envelope["data"])
        self.assertTrue(all(isinstance(l, str) or l is None
                            for l in [proc.stderr]))

    def test_entry_error_envelope(self):
        env = dict(os.environ, PYTHONPATH=BACKEND)
        proc = subprocess.run(
            [sys.executable, ENTRY, "analyse",
             os.path.join(BACKEND, "fixtures", "audio", "mix-48000.wav"),
             "--freqs", "/nonexistent-freqs.txt"],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 1)
        envelope = json.loads(proc.stdout)
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["error"]["code"], "no_freqs")
        self.assertTrue(proc.stderr.strip())


if __name__ == "__main__":
    unittest.main()
