"""capture — play the probe into a sink and record the result (port of
measure/capture.sh, generalized).

  capture [--from SOURCE|monitor] [--seconds N] [--rate FS] [--probe WAV]
          [--peak-db DB] SINK OUT.wav

Recording from a sink monitor measures the DSP electrically (independent of
room/level); from a microphone source measures what the speakers produce.
With --from monitor (default) the tap is the internal-speaker sink's monitor —
the physical sink both paths end at, upstream of its volume control — and the
command refuses if no speaker sink is detected.

Landing verification: mpv silently falls back to the default sink and
`pactl move-sink-input` pins silently, so the mpv sink-input is located and
checked against the TARGET SINK ID (index). Exit codes are never trusted.

Data: {"out","sink","sink_index","source","landed_sink_index","rate",
       "seconds","probe"}
"""
import os
import subprocess
import sys
import time

from .. import discovery, multitone
from .. import TunerError
from ..progress import progress

_SETTLE_S = 3.0          # upstream lets the stream settle before recording
_LANDING_TIMEOUT_S = 10.0


def _probe_path(args, sink):
    probe = os.path.abspath(args.probe) if args.probe else \
        os.path.join(discovery.cache_dir(), "dense.wav")
    if not os.path.exists(probe):
        print(f"generating the probe signal first -> {probe}", file=sys.stderr)
        rate = args.rate or discovery.sink_rate(sink)
        multitone.gen(probe, fs=rate, peak_db=args.peak_db)
    return probe


def _monitor_source():
    dev = discovery.devices()
    speaker = dev["speaker_sink"]
    if not speaker:
        raise TunerError(
            "no_speaker_sink",
            "no internal-speaker sink detected; pass --from <source> "
            "explicitly (a source name, or a <sink>.monitor tap)")
    sink = next(s for s in dev["sinks"] if s["name"] == speaker)
    if not sink.get("monitor_source"):
        raise TunerError("no_monitor", f"sink '{speaker}' has no monitor")
    return sink["monitor_source"]


def _verify_landing(sink_index, args):
    """Find the mpv sink-input and confirm it landed on `sink_index`."""
    deadline = time.time() + _LANDING_TIMEOUT_S
    landed = None
    while time.time() < deadline:
        for si in discovery.list_sink_inputs():
            props = si.get("properties", {}) or {}
            if props.get("application.name") == "mpv":
                landed = si.get("sink")
                if landed == sink_index:
                    return landed
        time.sleep(0.5)
    raise TunerError(
        "landing_mismatch",
        f"probe landed on sink {landed if landed is not None else 'none'}, "
        f"expected sink id {sink_index}; mpv may have fallen back to the "
        f"default sink")


def run(args):
    sink = args.sink
    out = os.path.abspath(args.out)
    rate = args.rate or discovery.sink_rate(sink)
    sink_index = discovery.sink_index(sink)
    source = args.source
    if not source or source == "monitor":
        source = _monitor_source()
    probe = _probe_path(args, sink)
    progress(args, 10, f"playing {probe} into '{sink}' (sink id {sink_index})")

    player = subprocess.Popen(
        ["mpv", "--no-video", "--no-terminal", "--really-quiet",
         "--volume=100", "--loop-file=inf", "--ao=pulse",
         f"--audio-device=pulse/{sink}", probe],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(_SETTLE_S)
        landed = _verify_landing(sink_index, args)
        progress(args, 40, f"landed on sink id {landed}; recording "
                           f"{args.seconds}s from '{source}'")
        rec = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "pulse", "-i", source, "-t", str(args.seconds),
             "-ac", "2", "-ar", str(rate), "-acodec", "pcm_s16le", out],
            capture_output=True, text=True, timeout=args.seconds + 60)
        if rec.returncode != 0:
            raise TunerError("record_failed",
                             f"ffmpeg failed: {rec.stderr.strip()[-300:]}")
    finally:
        player.terminate()
        try:
            player.wait(timeout=5)
        except subprocess.TimeoutExpired:
            player.kill()
    progress(args, 100, f"wrote {out}")
    print(f"wrote {out} (from {source}; verified on sink id {landed})",
          file=sys.stderr)
    return {"out": out, "sink": sink, "sink_index": sink_index,
            "source": source, "landed_sink_index": landed,
            "rate": rate, "seconds": args.seconds, "probe": probe}


def register(sub):
    p = sub.add_parser("capture", help="play the probe into SINK and record "
                                       "the result to OUT.wav")
    p.add_argument("sink")
    p.add_argument("out")
    p.add_argument("--from", dest="source", default="monitor",
                   help="source to record from: 'monitor' (default: the "
                        "speaker sink's monitor) or a source name")
    p.add_argument("--seconds", type=int, default=2)
    p.add_argument("--rate", type=int, default=None,
                   help="capture rate; default: the sink's actual rate")
    p.add_argument("--probe", default=None,
                   help="probe wav (default: the cached dense.wav; generated "
                        "if missing)")
    p.add_argument("--peak-db", type=float, default=-6.0, dest="peak_db",
                   help="probe peak used only when the probe is generated")
    p.set_defaults(func=run)
