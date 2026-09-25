"""SA4 tests — A/B compare/switch, apply, and restore paths. MOCKS ONLY.

No live audio, no playback, no config mutation: every external call goes
through fakes (pactl/systemctl/ffmpeg runners, temp dirs for file writes).
Run:  python3 -m unittest discover -s backend/tests -t backend
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tunerlib import TunerError
from tunerlib import ab as ablib
from tunerlib.commands import apply as applymod


class FakePactl:
    """In-memory pactl implementing the calls ABSession makes.

    Emulates the upstream traps: `move-sink-input` returns rc 0 while leaving
    PINNED streams where they are (landing verification must catch this).
    """

    def __init__(self, sinks, inputs=None, default=None, volume=50000, easyeffects=False):
        # sinks: {name: index}; inputs: {id: {"sink": index, "app": str|None, "pinned": bool}}
        self.sinks = dict(sinks)
        self.inputs = {k: dict(v) for k, v in (inputs or {}).items()}
        self.default = default or next(iter(sinks))
        self._flapped = False
        self.stuck_volume_once = False
        self.easyeffects_present = easyeffects
        self.volume = volume
        self.original_volume = volume
        self.easyeffects = easyeffects
        self.calls = []

    # --- pactl surface -----------------------------------------------------
    def __call__(self, argv):
        self.calls.append(list(argv))
        args = argv[1:]
        if args[:2] == ["list", "sinks"] and args[2] == "short":
            names = {n: i for n, i in self.sinks.items()
                     if n != "easyeffects_sink" or self.easyeffects_present}
            lines = [f"{i}\t{name}\tdriver\tfloat" for name, i in
                     sorted(names.items(), key=lambda kv: kv[1])]
            return 0, "\n".join(lines) + "\n", ""
        if args[:2] == ["list", "sink-inputs"]:
            out = []
            for sid, s in sorted(self.inputs.items()):
                out.append(f"Sink Input #{sid}")
                out.append(f"\tSink: {s['sink']}")
                if s["app"] is not None:
                    out.append(f'\t\tapplication.name = "{s["app"]}"')
                out.append("\tMute: no")
            return 0, "\n".join(out) + "\n", ""
        if args[0] == "move-sink-input":
            sid, sink_name = int(args[1]), args[2]
            target = self.sinks.get(sink_name)
            s = self.inputs.get(sid)
            if s is not None and target is not None and not s.get("pinned"):
                s["sink"] = target
            return 0, "", ""  # rc 0 even when pinned — never trust it
        if args[0] == "set-default-sink":
            self.default = args[1]
            return 0, "", ""
        if args[0] == "set-sink-volume":
            if not self.stuck_volume_once:
                self.volume = int(args[2])
            else:
                self.stuck_volume_once = False  # swallow exactly one write
            return 0, "", ""
        if args[0] == "get-default-sink":
            return 0, self.default + "\n", ""
        if args[0] == "get-sink-volume":
            return 0, f"0:  76% 1:  76% ... {self.volume} / 76%", ""
        return 1, "", f"unhandled: {args}"

    def sink_text(self):
        names = {n: i for n, i in self.sinks.items()
                 if n != "easyeffects_sink" or self.easyeffects_present}
        return "\n".join(
            f"{i}\t{name}\tdriver\tfloat" for name, i in
            sorted(names.items(), key=lambda kv: kv[1]))


class FakeSystemctl:
    """systemctl --user fake; when the service stops, the EasyEffects sink
    disappears from the (shared) FakePactl."""

    def __init__(self, pactl=None, active=True):
        self.pactl = pactl
        self.running = active
        self.stopped = False

    def __call__(self, argv):
        if "is-active" in argv:
            return (0, "", "") if self.running else (1, "", "")
        if "stop" in argv:
            self.running = False
            self.stopped = True
            if self.pactl is not None:
                self.pactl.easyeffects_present = False
            return 0, "", ""
        if "start" in argv:
            self.running = True
            self.stopped = False
            if self.pactl is not None:
                self.pactl.easyeffects_present = True
            return 0, "", ""
        return 1, "", ""


class FakeFfmpeg:
    """Returns a fixed ebur128 integrated reading; counts invocations."""

    def __init__(self, lufs=-20.0):
        self.lufs = lufs
        self.n = 0

    def __call__(self, argv):
        self.n += 1
        assert "ebur128" in argv and argv[argv.index("-i") + 1].endswith(".monitor")
        class P:
            returncode = 0
            stderr = (f"    I:        {self.lufs} LUFS\n"
                      "    LRA:       5.00 LU\n")
        return P()


def make_session(pactl, systemctl=None, **kw):
    return ablib.ABSession(pactl=pactl, systemctl=systemctl, **kw)


HW = "alsa_output.platform_speaker.analog-stereo.sink"
TUNED = "noctalia_audio_tuning_shipped"
TUNED2 = "noctalia_audio_tuning_dev"


class TestTrimMath(unittest.TestCase):
    def test_volume_for_trims_to_quietest_never_boosts(self):
        base = 40000
        # loudest candidate (this=-14, quietest=-20): correction -6 -> trim down
        loudest = ablib.volume_for(base, -14.0, -20.0)
        self.assertLess(loudest, base)
        # the quietest one itself: correction 0 -> untouched
        self.assertEqual(ablib.volume_for(base, -20.0, -20.0), base)
        # never boosted: being quieter than the reference clamps at base
        self.assertEqual(ablib.volume_for(base, -26.0, -20.0), base)
        # same order as upstream awk: v = base * 10**(correction/60)
        expect = int(base * 10 ** (-6.0 / 60.0) + 0.5)
        self.assertEqual(loudest, expect)

    def test_volume_for_clamps(self):
        # beyond the 0..65536 scale both ends clamp
        self.assertEqual(ablib.volume_for(70000, 0.0, 0.0), ablib.VOLUME_MAX)
        self.assertEqual(ablib.volume_for(0, -14.0, -20.0), 0)

    def test_parse_ebur128_takes_last_I(self):
        txt = ("    I:     -18.1 LUFS\n    I:     -22.3 LUFS\n"
               "    LRA:     4.0 LU\n")
        self.assertEqual(ablib.parse_ebur128_integrated(txt), -22.3)
        self.assertIsNone(ablib.parse_ebur128_integrated("nothing here"))

    def test_offsets_cache_roundtrip_and_quietest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "song name_.offsets"
            ablib.write_offsets(p, {TUNED: -21.5, HW: -18.0})
            self.assertEqual(ablib.read_offsets(p), {TUNED: -21.5, HW: -18.0})
            self.assertEqual(ablib.quietest_sink(
                ablib.read_offsets(p), [TUNED, HW]), TUNED)


class TestCandidateDiscovery(unittest.TestCase):
    def test_discovery_from_mocked_fixture(self):
        sinks = FakePactl({HW: 41, TUNED: 60, TUNED2: 61}).sink_text()
        cands = ablib.discover_candidates(sinks, raw_hardware_sink=HW)
        names = [c.name for c in cands]
        self.assertIn(TUNED, names)
        self.assertIn(TUNED2, names)
        self.assertIn(HW, names)
        raw = [c for c in cands if c.kind == "raw"][0]
        self.assertEqual(raw.trim_db, -7.4)
        self.assertEqual([c.label for c in cands if c.kind == "tuning"],
                         ["dev", "shipped"])

    def test_discovery_prefix_is_configurable_no_omarchy(self):
        sinks = FakePactl({HW: 41, "omarchy_tuning_x": 60}).sink_text()
        cands = ablib.discover_candidates(sinks, raw_hardware_sink=HW)
        self.assertNotIn("omarchy_tuning_x", [c.name for c in cands])

    def test_easyeffects_present_means_raw_withheld(self):
        p = FakePactl({HW: 41, TUNED: 60, "easyeffects_sink": 70},
                      easyeffects=True)
        cands = ablib.discover_candidates(p.sink_text(), raw_hardware_sink=HW)
        names = [c.name for c in cands]
        self.assertIn("easyeffects_sink", names)
        self.assertNotIn(HW, names)  # cannot be auditioned while EE runs

    def test_start_switch_mode_stops_easyeffects(self):
        p = FakePactl({HW: 41, TUNED: 60, "easyeffects_sink": 70},
                      {1: {"sink": 70, "app": "music", "pinned": False}},
                      default=HW)
        sc = FakeSystemctl(pactl=p, active=True)
        sess = make_session(p, systemctl=sc)
        payload = sess.start(raw_hardware_sink=HW, mode="switch")
        self.assertTrue(sc.stopped)
        self.assertTrue(sess.ee_stopped)
        self.assertNotIn("easyeffects_sink", [c.name for c in sess.candidates])
        self.assertIn(HW, [c.name for c in sess.candidates])
        self.assertEqual(sess.original_default, HW)
        self.assertEqual(payload["selected"], TUNED)

    def test_start_requires_two_candidates(self):
        p = FakePactl({HW: 41}, {1: {"sink": 41, "app": "music", "pinned": False}},
                      default=HW)
        sess = make_session(p, systemctl=FakeSystemctl(active=False))
        with self.assertRaises(TunerError) as ctx:
            sess.start(raw_hardware_sink=HW)
        self.assertEqual(ctx.exception.code, "no_candidates")


class TestLandingVerification(unittest.TestCase):
    def test_pinned_stream_counts_as_stuck_despite_rc0(self):
        p = FakePactl({HW: 41, TUNED: 60},
                      {1: {"sink": 41, "app": "music", "pinned": False},
                       2: {"sink": 41, "app": "pinned-app", "pinned": True}},
                      default=HW)
        sess = make_session(p)
        move = sess.move_streams(TUNED)
        # rc was 0 for both moves; only the unpinned one actually landed
        self.assertEqual(move["moved"], 1)
        self.assertEqual(move["stuck"], 1)
        self.assertEqual(p.inputs[1]["sink"], 60)
        self.assertEqual(p.inputs[2]["sink"], 41)  # stayed put

    def test_move_to_unknown_sink_unverified(self):
        p = FakePactl({HW: 41}, {1: {"sink": 41, "app": "music", "pinned": False}},
                      default=HW)
        sess = make_session(p)
        move = sess.move_streams("ghost_sink")
        self.assertFalse(move["verified"])


class TestRestoreOnEveryExit(unittest.TestCase):
    def _session(self):
        p = FakePactl({HW: 41, TUNED: 60, TUNED2: 61},
                      {1: {"sink": 41, "app": "music", "pinned": False}},
                      default=HW, volume=48000)
        sess = make_session(p, systemctl=FakeSystemctl(active=False))
        sess.start(raw_hardware_sink=HW)
        return p, sess

    def _idx(self, sess, name):
        return [c.name for c in sess.candidates].index(name)

    def test_stop_restores_default_streams_volume(self):
        p, sess = self._session()
        sess.switch(self._idx(sess, TUNED2))
        self.assertEqual(p.default, TUNED2)
        self.assertEqual(p.volume, 48000)  # linear tuning keeps base volume
        sess.stop()
        self.assertEqual(p.default, HW)
        self.assertEqual(p.inputs[1]["sink"], 41)
        self.assertEqual(p.volume, 48000)

    def test_stop_after_raw_selection_restores_trimmed_volume(self):
        p, sess = self._session()
        sess.switch(self._idx(sess, HW))  # raw candidate, -7.4 dB trim
        self.assertLess(p.volume, 48000)
        sess.stop()
        self.assertEqual(p.volume, 48000)  # read-back verified original

    def test_calibrate_restores_on_error_and_caches_offsets(self):
        p, sess = self._session()
        anchor_default = sess.original_default

        class Boom(FakeFfmpeg):
            def __call__(self, argv):
                if self.n >= 1:
                    raise RuntimeError("ffmpeg exploded")
                return super().__call__(argv)

        with tempfile.TemporaryDirectory() as d:
            offs = Path(d) / "t.offsets"
            with self.assertRaises(RuntimeError):
                sess.calibrate(ffmpeg=Boom(), offsets_file=offs)
            # finally-branch restored the default sink + streams even on error
            self.assertEqual(p.default, anchor_default)
            self.assertEqual(p.inputs[1]["sink"], p.sinks[anchor_default])
            # the measurement that succeeded before the boom is cached
            # (never faked as 0 LU); the failing candidate holds no offset
            self.assertEqual(sess.offsets.get(TUNED2), -20.0)
            self.assertNotIn(HW, sess.offsets)

    def test_volume_restore_retry_on_stuck_write(self):
        p, sess = self._session()
        sess.switch(self._idx(sess, HW))  # raw -> volume trimmed below base
        trimmed = p.volume
        self.assertLess(trimmed, 48000)
        # the next restore write silently fails to stick (rc 0, no effect),
        # exactly the trap the read-back verification exists for
        p.stuck_volume_once = True
        payload = sess.stop()
        self.assertTrue(payload.get("volume_retried"))
        self.assertEqual(p.volume, 48000)

    def test_select_without_session_is_tuner_error(self):
        p = FakePactl({HW: 41}, {}, default=HW)
        sess = make_session(p)
        with self.assertRaises(TunerError) as ctx:
            sess.select(0)
        self.assertEqual(ctx.exception.code, "no_session")


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmp.name) / "chain.conf"
        self.conf.write_text(
            'context.modules = [ { name = libpipewire-module-filter-chain\n'
            '  args.nodes[0].target = "@SPEAKER_SINK@" } ]\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_substitute_placeholder(self):
        self.assertEqual(
            applymod.substitute_placeholder("t = @SPEAKER_SINK@", "alsa_x.sink"),
            "t = alsa_x.sink")

    def test_install_writes_and_backs_up_existing(self):
        dest = Path(self.tmp.name) / "dropins"
        target = dest / "noctalia-audio-tuning.conf"
        target.parent.mkdir()
        target.write_text("old\n")
        result = applymod.install(self.conf, dest, "noctalia-audio-tuning",
                                  HW, force=True)
        new = target.read_text()
        self.assertNotIn("@SPEAKER_SINK@", new)
        self.assertIn(HW, new)
        bak = result["written"][0]["backed_up_to"]
        self.assertIsNotNone(bak)
        self.assertEqual(Path(bak).read_text(), "old\n")

    def test_restore_on_failure_leaves_original(self):
        dest = Path(self.tmp.name) / "dropins2"
        dest.mkdir()
        target = dest / "noctalia-audio-tuning.conf"
        target.write_text("previous contents\n")

        # force os.replace to fail after the backup by making the dir read-only
        os.chmod(dest, 0o500)
        try:
            with self.assertRaises(OSError):
                applymod.install(self.conf, dest, "noctalia-audio-tuning",
                                 HW, force=True)
        finally:
            os.chmod(dest, 0o755)
        self.assertEqual(target.read_text(), "previous contents\n")

    def test_run_dry_run_default_touches_nothing_real(self):
        dest = Path(self.tmp.name) / "dropins3"
        import argparse as ap
        args = ap.Namespace(config=str(self.conf), write=False, dest=str(dest),
                            name="noctalia-audio-tuning", sink=HW, force=False)
        payload = applymod.run(args)
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["dry_run"])
        self.assertFalse(dest.exists())  # dry run never wrote to --dest

    def test_run_real_write(self):
        dest = Path(self.tmp.name) / "dropins4"
        import argparse as ap
        args = ap.Namespace(config=str(self.conf), write=True, dest=str(dest),
                            name="noctalia-audio-tuning", sink=HW, force=False)
        payload = applymod.run(args)
        self.assertTrue(payload["applied"])
        self.assertIn(HW, (dest / "noctalia-audio-tuning.conf").read_text())

    def test_missing_sink_for_placeholder_is_tuner_error(self):
        import argparse as ap
        args = ap.Namespace(config=str(self.conf), write=False, dest="",
                            name="x", sink=None, force=False)
        with self.assertRaises(TunerError) as ctx:
            applymod.run(args)
        self.assertEqual(ctx.exception.code, "no_sink")


if __name__ == "__main__":
    unittest.main()
