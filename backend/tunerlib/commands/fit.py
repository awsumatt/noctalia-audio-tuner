"""fit — fit a biquad chain to a target curve (port of fit-eq.py).

  fit [--rate INT] [--layout NAME] [--restarts INT] [--out FILE] TARGET.txt

TARGET.txt is '<freq> <db>' per line (delta output is compatible). Sample rate
defaults to 48000 — pass --rate (or derive it from the capture that produced
the target) rather than relying on the default; it is a parameter, never a
hardcode. --layout selects a profile from tunerlib/layouts/ (default: the
upstream 'small laptop drivers' prior).

Data: {"target","rate","layout","restarts","n_points",
       "weighted_rms_error_db","magnitude_rms_db","bass_group_delay_swing_ms",
       "global_gain_db","linear_gain","sections":[{"kind","freq","q","gain_db"}],
       "fit_text", "out"}
"""
import math
import os

from .. import fit as fitmod
from ..progress import progress


def run(args):
    fs = args.rate
    if fs <= 0:
        raise fitmod.TunerError("bad_rate", f"--rate must be a positive int, got {fs}")
    progress(args, 10, f"fitting {args.target} at {fs} Hz, layout={args.layout}")
    payload = fitmod.fit_to_payload(args.target, fs=fs, layout_name=args.layout,
                                    restarts=args.restarts)
    out = None
    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "w") as fh:
            fh.write(payload["fit_text"] + "\n")
        out = os.path.abspath(args.out)
    progress(args, 100, f"weighted RMS {payload['weighted_rms_error_db']:.2f} dB")
    return {**payload, "out": out}


def register(sub):
    p = sub.add_parser("fit", help="fit a biquad chain to a target curve "
                                   "(<freq> <db> lines)")
    p.add_argument("target", help="target file: '<freq> <db>' lines "
                                  "(delta output is compatible)")
    p.add_argument("--rate", type=int, default=fitmod.DEFAULT_RATE,
                   help="sample rate in Hz (default 48000; use the capture's "
                        "actual rate)")
    p.add_argument("--layout", default="default",
                   help="layout profile name from tunerlib/layouts/ "
                        "(default: small laptop drivers prior)")
    p.add_argument("--restarts", type=int, default=12,
                   help="multi-seed restart count (default 12)")
    p.add_argument("--out", help="also write the upstream fit.txt form here")
    p.set_defaults(func=run)
