"""doctor — dependency checks. Data: {"deps":[{name,ok,hint,required}], "ok":bool}."""
import shutil
import sys

from ..progress import progress


def _which(name):
    return shutil.which(name)


def dependency_checks():
    """The dep table. pactl OR wpctl satisfies the audio-control entry."""
    pactl, wpctl = _which("pactl"), _which("wpctl")
    found = pactl or wpctl
    via = "pactl" if pactl else ("wpctl" if wpctl else None)
    return [
        {"name": "python3", "required": True, "ok": _which("python3") is not None,
         "hint": "runs this backend (any >= 3.10)"},
        {"name": "ffmpeg", "required": True, "ok": _which("ffmpeg") is not None,
         "hint": "capture records via `ffmpeg -f pulse`; install: pacman -S ffmpeg"},
        {"name": "mpv", "required": True, "ok": _which("mpv") is not None,
         "hint": "probe/tone playback; install: pacman -S mpv"},
        {"name": "pactl or wpctl", "required": True, "ok": found is not None,
         "hint": "audio control for discovery (PipeWire: pacman -S libpulse for "
                 "pactl, or wireplumber for wpctl); found: " + (via or "none")},
        {"name": "lsp-plugins-lv2", "required": False,
         "ok": _which("lsp-plugins-lv2") is not None or
               _which("lsp-plugins-lv2.so") is not None,
         "hint": "optional: LSP LV2 plugin suite used by the tuning filter "
                 "chain; install: pacman -S lsp-plugins-lv2"},
    ]


def run(args):
    deps = dependency_checks()
    ok = all(d["ok"] for d in deps if d["required"])
    progress(args, 100, "dependency check complete")
    return {"deps": deps, "ok": ok,
            "missing": [d["name"] for d in deps
                        if d["required"] and not d["ok"]]}


def register(sub):
    p = sub.add_parser("doctor", help="check runtime dependencies")
    p.set_defaults(func=run)
