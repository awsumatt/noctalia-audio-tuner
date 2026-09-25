"""tone — fundamental, harmonics and THD of a tone in a wav
(port of analyze-tone.py, rate taken from the wav header).

  tone TONE.wav FREQ

Data: {"freq","fs","fundamental_dbfs","harmonics_dbfs","thd_pct"}
"""
from .. import analysis
from ..progress import progress


def run(args):
    progress(args, 50, f"analysing {args.freq} Hz")
    data = analysis.tone_analysis(args.wav, args.freq,
                                  n_harmonics=args.harmonics)
    progress(args, 100, "done")
    return data


def register(sub):
    p = sub.add_parser("tone", help="fundamental + harmonics + THD at FREQ")
    p.add_argument("wav")
    p.add_argument("freq", type=float, help="fundamental in Hz")
    p.add_argument("--harmonics", type=int, default=6,
                   help="number of harmonics to report (default 6)")
    p.set_defaults(func=run)
