"""mic-sweep — sweep speaker level and measure acoustic distortion at a fixed
tone (port of measure/mic-sweep.sh, generalized).

  mic-sweep [--mic SOURCE] [--out-dir DIR] [--rate FS]
            [--levels "40 55 70 85 100"] SINK TONE.wav FREQ LABEL

Routing switches the DEFAULT sink (mpv --audio-device silently falls back to
the default), then verifies where the tone landed BY SINK ID before recording.
The previous default sink and the speaker sink's volume are restored on exit.

Data: {"sink","mic","freq","label","rows":[{"pct","wav","fundamental_dbfs",
       "harmonics_dbfs","thd_pct"}],"landed_sink_index"}
Ported but NOT live-tested (requires mutable audio state).
"""
import os
import subprocess
import sys
import time

from .. import analysis, discovery, multitone
from .. import TunerError
from ..progress import progress

VOLUME_SCALE = 65536      # PulseAudio 0..65536 volume scale (documented)
_SETTLE_S = 2.0
_REC_S = 1.5


def _find_mpv_input(sink_index, timeout=10.0):
    deadline = time.time() + timeout
    landed = None
    while time.time() < deadline:
        for si in discovery.list_sink_inputs():
            props = si.get("properties", {}) or {}
            if props.get("application.name") == "mpv":
                landed = si.get("sink")
                if landed == sink_index:
                    return landed
        time.sleep(0.5)
    raise TunerError("landing_mismatch",
                     f"tone landed on sink {landed}, expected {sink_index}")


def run(args):
    sink, tone, freq, label = args.sink, args.tone, args.freq, args.label
    mic = args.mic or discovery.defaults()["source"]
    rate = args.rate or discovery.sink_rate(sink)
    sink_index = discovery.sink_index(sink)
    speaker = discovery.devices()["speaker_sink"]
    if not speaker:
        raise TunerError("no_speaker_sink", "no internal-speaker sink detected")

    prev_default = discovery.defaults()["sink"]
    vol_out = subprocess.run(["pactl", "get-sink-volume", speaker],
                             capture_output=True, text=True).stdout
    vol_lines = [l for l in vol_out.splitlines() if l.strip()]
    prev_volume = vol_lines[0].split()[-1] if vol_lines else "100%"

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    safe = f"mic-{label}-{int(freq)}"
    player = None
    try:
        subprocess.run(["pactl", "set-default-sink", sink], check=True,
                       capture_output=True)
        subprocess.run(["pactl", "set-source-mute", mic, "0"],
                       capture_output=True)
        subprocess.run(["pactl", "set-source-volume", mic, "100%"],
                       capture_output=True)
        progress(args, 5, f"playing {tone} into '{sink}' (id {sink_index})")
        player = subprocess.Popen(
            ["mpv", "--no-video", "--no-terminal", "--really-quiet",
             "--loop-file=inf", "--ao=pulse", f"--audio-device=pulse/{sink}",
             tone],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(_SETTLE_S)
        landed = _find_mpv_input(sink_index)

        rows = []
        for idx, pct in enumerate(args.levels):
            cap = os.path.join(out_dir, f"{safe}-{pct}.wav")
            subprocess.run(
                ["pactl", "set-sink-volume", speaker,
                 str(VOLUME_SCALE * pct // 100)], capture_output=True)
            time.sleep(1.2)
            progress(args, 10 + 80 * idx // len(args.levels),
                     f"recording at {pct}%")
            rec = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "pulse", "-i", mic, "-t", str(_REC_S), "-ac", "1",
                 "-ar", str(rate), "-acodec", "pcm_s16le", cap],
                capture_output=True, text=True, timeout=60)
            if rec.returncode != 0:
                raise TunerError("record_failed",
                                 f"ffmpeg failed: {rec.stderr.strip()[-300:]}")
            data = analysis.tone_analysis(cap, freq,
                                          n_harmonics=args.harmonics)
            print(f"{pct:4d}%  fund={data['fundamental_dbfs']:7.1f} dBFS  "
                  f"THD={data['thd_pct']:6.2f}%", file=sys.stderr)
            rows.append({"pct": pct, "wav": cap,
                         "fundamental_dbfs": data["fundamental_dbfs"],
                         "harmonics_dbfs": data["harmonics_dbfs"],
                         "thd_pct": data["thd_pct"]})
    finally:
        if player is not None:
            player.terminate()
            try:
                player.wait(timeout=5)
            except subprocess.TimeoutExpired:
                player.kill()
        # restore-on-exit: default sink + speaker volume (backup first)
        subprocess.run(["pactl", "set-default-sink", prev_default],
                       capture_output=True)
        subprocess.run(["pactl", "set-sink-volume", speaker, prev_volume],
                       capture_output=True)
    progress(args, 100, f"{len(rows)} levels swept")
    return {"sink": sink, "sink_index": sink_index, "speaker_sink": speaker,
            "mic": mic, "freq": freq, "label": label, "rows": rows,
            "landed_sink_index": landed}


def register(sub):
    p = sub.add_parser("mic-sweep", help="sweep speaker level; distortion "
                                         "vs level at a fixed tone")
    p.add_argument("sink")
    p.add_argument("tone", help="single-tone wav")
    p.add_argument("freq", type=float, help="tone fundamental in Hz")
    p.add_argument("label", help="short label for the capture filenames")
    p.add_argument("--mic", default=None,
                   help="source to record from (default: the default source; "
                        "upstream MIC= override)")
    p.add_argument("--out-dir", default=None,
                   help="capture directory (default: cache dir/spectral)")
    p.add_argument("--rate", type=int, default=None,
                   help="capture rate; default: the sink's actual rate")
    p.add_argument("--levels", nargs="+", type=int,
                   default=[40, 55, 70, 85, 100],
                   help="volume percentages to sweep")
    p.add_argument("--harmonics", type=int, default=6)
    p.set_defaults(func=run)
