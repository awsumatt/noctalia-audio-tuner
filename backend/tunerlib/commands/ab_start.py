"""ab-start: begin an A/B audition session (switch or calibrated-compare mode).

Registry contract: register(sub) + run(args) -> dict payload.
"""

from __future__ import annotations

from .. import TunerError
from .. import ab as ablib
from . import _common


def find_raw_hardware_sink(sess: "ablib.ABSession") -> str | None:
    """Generalized raw-hardware-sink discovery (banned vendor regexes do not
    survive the port). Heuristic over sink names only: an `alsa_output.*`
    sink (never a .monitor) that looks like a speaker path. If ambiguous or
    absent, --sink must be given by the caller / UI."""
    sinks = sess.list_sinks()
    alsa = [s["name"] for s in sinks if ablib.classify_raw_hardware_sink(s["name"])]
    if len(alsa) == 1:
        return alsa[0]
    return None


def register(sub) -> None:
    p = sub.add_parser(
        "ab-start",
        help="Start an A/B audition session over tuning candidates",
        description="Discover candidates (installed tunings, EasyEffects if "
        "running, raw hardware control) and select the first one. In switch "
        "mode a running EasyEffects user service is stopped for the session "
        "and restored on ab-stop.",
    )
    p.add_argument("--mode", choices=["switch", "compare"], default="switch",
                   help="switch: move your own playing stream; compare: "
                        "calibrated level-matched loop (default: %(default)s)")
    p.add_argument("--sink", default=None,
                   help="Raw hardware sink name (auto-detected when unambiguous)")
    p.add_argument("--trim", type=float, default=None,
                   help="Raw-path trim in dB (default: -7.4, the documented upstream default)")
    p.add_argument("--prefix", default=ablib.DEFAULT_TUNING_PREFIX,
                   help="Tuning sink-name prefix (default: %(default)s)")
    p.set_defaults(func=run)


def run(args) -> dict:
    pactl = getattr(args, "runner", None) or _common.default_runner
    systemctl = getattr(args, "systemctl_runner", None) or _common.systemctl_user_runner
    sess = ablib.ABSession(pactl=pactl, systemctl=systemctl, tuning_prefix=getattr(args, "prefix", ablib.DEFAULT_TUNING_PREFIX))
    raw = getattr(args, "sink", None) or find_raw_hardware_sink(sess)
    if not raw:
        raise TunerError(
            "no_raw_sink",
            "Could not identify the raw hardware speaker sink; pass --sink "
            "(see `devices` for classified sinks).",
        )
    trim = getattr(args, "trim", None)
    payload = sess.start(raw_hardware_sink=raw, mode=getattr(args, "mode", "switch"), raw_trim_db=trim)
    _common.save_session(sess.to_dict())
    payload["mode"] = getattr(args, "mode", "switch")
    payload["raw_hardware_sink"] = raw
    return payload
