"""analyse — level of every probe tone in a capture (leakage-free Goertzel).

  analyse [--freqs FILE] [--start S] CAPTURE.wav

--freqs: tone list (default: the probe's dense-freqs.txt in the cache dir).
The window is EXACTLY one second of the wav's own rate (read from the header,
never hardcoded), so integer-Hz tones stay bin-aligned at any rate.

Data: {"wav","rate","start","freqs_path","levels":[{"freq","dbfs"}]}
"""
import os

from .. import analysis, discovery, multitone
from ..progress import progress


def run(args):
    wav = os.path.abspath(args.wav)
    freqs_path = args.freqs or os.path.join(discovery.cache_dir(),
                                            "dense-freqs.txt")
    if not os.path.exists(freqs_path):
        raise analysis.TunerError(
            "no_freqs", f"no tone list at {freqs_path}; run `probe` first "
                        f"or pass --freqs")
    freqs = multitone.read_freqs(freqs_path)
    levels = analysis.analyse_response(
        wav, freqs, start_s=args.start,
        progress_cb=lambda i, n: progress(args, 10 + 85 * i // max(n, 1),
                                          f"analysed {i}/{n} tones"))
    progress(args, 100, f"{len(levels)} tones analysed")
    return {"wav": wav, "rate": analysis.read_mono(wav)[1], "start": args.start,
            "freqs_path": os.path.abspath(freqs_path), "levels": levels}


def register(sub):
    p = sub.add_parser("analyse", help="measure every probe tone in a capture")
    p.add_argument("wav")
    p.add_argument("--freqs", default=None,
                   help="tone list file (default: cache dense-freqs.txt)")
    p.add_argument("--start", type=float, default=0.3,
                   help="analysis window start in seconds (default 0.3)")
    p.set_defaults(func=run)
