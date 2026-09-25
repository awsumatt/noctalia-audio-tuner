"""ab-switch: move the playing app streams to another A/B candidate.

Registry contract: register(sub) + run(args) -> dict payload.

Moves every real application stream to the selected candidate (a tuning sink,
EasyEffects, or the raw hardware control), verifying where the streams actually
LANDED by sink id — `pactl move-sink-input` reports success while leaving
pinned streams where they are, so exit codes are never trusted — and applies
the candidate's volume trim at the hardware sink (calibrated offsets win when
present, otherwise the static trim).

Data: {"selected","label","index","move","trim_db","volume","n_candidates"}
"""
from __future__ import annotations

from pathlib import Path

from .. import TunerError
from .. import ab as ablib
from ..ab import read_offsets
from . import _common


def register(sub) -> None:
    p = sub.add_parser(
        "ab-switch",
        help="Switch the A/B session to another candidate",
        description="Move the playing application streams to the selected "
        "A/B candidate (default: the next one) and apply its trim. Streams "
        "are moved with landing verification by sink id.",
    )
    p.add_argument("candidate", nargs="?", type=int, default=None,
                   help="1-based candidate number (default: next candidate)")
    p.add_argument("--name", default=None,
                   help="Switch to this sink name instead of a number")
    p.add_argument("--offsets-file", default=None,
                   help="Per-track offsets file to load (from calibration); "
                   "when given, level-matching uses the cached LUFS values")
    p.set_defaults(func=run)


def run(args) -> dict:
    data = _common.load_session()
    if data is None:
        raise TunerError("no_session", "No active A/B session; run ab-start first.")
    pactl = _common.default_runner
    systemctl = _common.systemctl_user_runner
    sess = ablib.ABSession.from_dict(data, pactl=pactl, systemctl=systemctl)
    if not sess.active:
        raise TunerError("no_session", "No active A/B session; run ab-start first.")

    if args.offsets_file:
        sess.offsets.update(read_offsets(Path(args.offsets_file)))

    if args.name is not None:
        names = [c.name for c in sess.candidates]
        if args.name not in names:
            raise TunerError(
                "no_candidate",
                f"{args.name} is not an A/B candidate (one of: {', '.join(names)}).",
            )
        payload = sess.select(names.index(args.name))
    else:
        index = (args.candidate - 1) if args.candidate is not None else None
        if index is not None and not (0 <= index < len(sess.candidates)):
            raise TunerError(
                "no_candidate",
                f"Candidate {args.candidate} out of range 1..{len(sess.candidates)}.",
            )
        payload = sess.switch(index)

    sess.active = True
    _common.save_session(sess.to_dict())
    payload["volume"] = sess.volume_for_candidate(sess.candidates[sess.index])
    payload["n_candidates"] = len(sess.candidates)
    return payload
