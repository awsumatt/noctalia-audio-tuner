"""ab-stop: end the A/B session and restore the original audio state.

Registry contract: register(sub) + run(args) -> dict payload.

Full restore: original default sink, playing streams moved back (with landing
verification), original hardware volume (read back and retried if the write
did not land), and EasyEffects restarted when ab-start stopped it. Idempotent:
with no active session it reports that instead of erroring. With --keep the
selected candidate stays active; EasyEffects is then left stopped and the
command says so.

Data: {"restored","reason","...restored fields..."} (see ABSession.stop)
"""
from __future__ import annotations

from .. import ab as ablib
from . import _common


def register(sub) -> None:
    p = sub.add_parser(
        "ab-stop",
        help="End the A/B session and restore the original audio state",
        description="Restore the original default sink, move playing streams "
        "back (verified), restore the original hardware volume, and restart "
        "EasyEffects if it was stopped. Idempotent.",
    )
    p.add_argument("--keep", action="store_true",
                   help="keep the currently selected candidate active "
                        "(default: restore everything)")
    p.set_defaults(func=run)


def run(args) -> dict:
    data = _common.load_session()
    if data is None:
        return {"restored": False, "reason": "no-session"}
    sess = ablib.ABSession.from_dict(
        data, pactl=_common.default_runner, systemctl=_common.systemctl_user_runner)
    payload = sess.stop(keep=bool(getattr(args, "keep", False)))
    if not getattr(args, "keep", False):
        _common.clear_session()
    return payload
