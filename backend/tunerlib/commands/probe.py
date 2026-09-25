"""probe — generate the dense pink-weighted multitone probe.

Flags: [--rate FS] [--seconds N] [--peak-db DB] [--seed N] [out]
  out: a directory (dense.wav is created inside it) or a .wav path;
       default is $XDG_CACHE_HOME/noctalia-audio-tuner/dense.wav
  --rate: default = the ACTUAL rate of the default sink (detected via pactl /
          pw-dump); never hardcoded. The tone list is written NEXT TO the
          probe as <stem>-freqs.txt.

Data: {"wav","freqs_path","rate","seconds","peak_db","seed","n_tones","freqs"}
"""
import os
import sys

from .. import discovery, multitone
from ..progress import progress


def _resolve_out(out):
    out = os.path.abspath(out or discovery.cache_dir())
    if os.path.isdir(out) or not out.endswith(".wav"):
        out = os.path.join(out, "dense.wav")
    return out


def detect_rate():
    """Default-sink rate, or 48000 with a stderr warning if undetectable."""
    try:
        dfl = discovery.defaults()["sink"]
        return discovery.sink_rate(dfl), dfl
    except Exception as e:  # noqa: BLE001 - degrade, never crash probe gen
        print(f"warning: could not detect the default sink rate ({e}); "
              f"using 48000", file=sys.stderr)
        return 48000, None


def run(args):
    rate = args.rate or detect_rate()[0]
    wav = _resolve_out(args.out)
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    progress(args, 10, f"generating {len(multitone.frequencies())} tones "
                       f"at {rate} Hz")
    freqs, freqs_path = multitone.gen(wav, fs=rate, seconds=args.seconds,
                                      peak_db=args.peak_db, seed=args.seed)
    progress(args, 100, f"wrote {wav}")
    print(f"probe: {wav} ({args.seconds}s @ {rate} Hz, peak {args.peak_db} dB, "
          f"{len(freqs)} tones); tone list: {freqs_path}", file=sys.stderr)
    return {"wav": wav, "freqs_path": freqs_path, "rate": rate,
            "seconds": args.seconds, "peak_db": args.peak_db,
            "seed": args.seed, "n_tones": len(freqs), "freqs": freqs}


def register(sub):
    p = sub.add_parser("probe", help="generate the multitone probe wav "
                                     "(+ tone list next to it)")
    p.add_argument("out", nargs="?", default=None,
                   help="output dir or .wav path (default: cache dir)")
    p.add_argument("--rate", type=int, default=None,
                   help="sample rate; default: the default sink's actual rate")
    p.add_argument("--seconds", type=int, default=6,
                   help="probe length in seconds (default 6)")
    p.add_argument("--peak-db", type=float, default=-6.0, dest="peak_db",
                   help="composite probe peak in dBFS (default -6)")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(func=run)
