"""delta — target curve = reference - raw (port of response-delta.py).

  delta RAW.txt REFERENCE.txt

Both files are analyse output: '<freq> <dbfs>' per line. The mic's own
response cancels when both captures used the same mic in the same position.

Data: {"raw","reference","delta":[{"freq","db"}],"n"}
"""
import os

from .. import analysis, TunerError
from ..progress import progress


def _read(path):
    if not os.path.exists(path):
        raise TunerError("no_response_file", f"no such response file: {path}")
    try:
        with open(path) as fh:
            return analysis.parse_response_text(fh.read())
    except OSError as e:
        raise TunerError("no_response_file",
                         f"cannot read {path}: {e}") from e


def run(args):
    raw, reference = _read(args.raw), _read(args.reference)
    progress(args, 50, "computing reference - raw")
    delta = analysis.delta(raw, reference)
    progress(args, 100, f"{len(delta)} frequencies")
    out_path = None
    if getattr(args, "out", None):
        out_path = os.path.abspath(args.out)
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(out_path, "w") as fh:
            for row in delta:
                fh.write(f"{row['freq']} {row['db']:.2f}\n")
    payload = {"raw": os.path.abspath(args.raw),
               "reference": os.path.abspath(args.reference),
               "delta": delta, "n": len(delta)}
    if out_path:
        payload["out"] = out_path
    return payload


def register(sub):
    p = sub.add_parser("delta", help="target curve = REFERENCE - RAW "
                                     "per shared frequency")
    p.add_argument("raw", help="response file of the raw path")
    p.add_argument("reference", help="response file of the reference path")
    p.add_argument("--out", default=None,
                   help="also write the target curve as '<freq> <db>' text "
                        "to this file (the format `fit` consumes)")
    p.set_defaults(func=run)
