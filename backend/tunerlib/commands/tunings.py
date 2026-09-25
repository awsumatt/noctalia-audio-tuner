"""tunings — list installed tuning sinks/configs (read-only).

  tunings

Scans (never mutates) the PipeWire config drop-ins and the local tuning state
directories for filter-chain modules / tuning trees, and reports what is
installed with its target sink.

Data: {"searched": [dirs], "tunings": [{"path","kind","prefix","target_sink",
"report": {"magnitude_rms_db": float|None, "bass_group_delay_swing_ms": float|None,
"limiter_headroom_db": float|"not_measured", "dynamic_range_delta_lu": float|"not_measured"}|None}]}
"""
import os
import re

from .. import TunerError

_MARKER = "not_measured"


def _xdg(env, default):
    return os.environ.get(env) or os.path.join(os.path.expanduser("~"), default)


def search_dirs():
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(_xdg("XDG_CONFIG_HOME", ".config"),
                     "pipewire", "pipewire.conf.d"),
        os.path.join(_xdg("XDG_STATE_HOME", ".local/state"),
                     "noctalia-audio-tuner", "tunings"),
        os.path.join(home, ".config", "pipewire", "pipewire.conf.d"),
    ]
    return dirs


def _parse_tuning_conf(path):
    report = {}
    found = False
    try:
        with open(path) as fh:
            for line in fh:
                m = re.match(r"\s*([\w]+)\s*=\s*(.+?)\s*$", line)
                if not m or m.group(1).startswith("#"):
                    continue
                key, val = m.groups()
                if val.strip() == _MARKER:
                    report[key] = _MARKER
                else:
                    try:
                        report[key] = float(val)
                    except ValueError:
                        continue
                found = True
    except OSError:
        return None
    return report if found else None


def _scan_file(path):
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return None
    if "libpipewire-module-filter-chain" not in text:
        return None
    prefix_m = re.search(r'node\.name\s*=\s*"([^"]+)"\s*\n\s*media\.class',
                         text)
    target_m = re.search(r'target\.object\s*=\s*"([^"]+)"', text)
    prefix = prefix_m.group(1) if prefix_m else os.path.splitext(
        os.path.basename(path))[0]
    report = None
    tconf = os.path.join(os.path.dirname(path), "tuning.conf")
    if os.path.exists(tconf):
        report = _parse_tuning_conf(tconf)
    return {"path": os.path.abspath(path), "kind": "pipewire-filter-chain",
            "prefix": prefix,
            "target_sink": target_m.group(1) if target_m else None,
            "report": report}


def run(args):
    if not os.path.isdir("/etc/pipewire") and not os.path.isdir(
            _xdg("XDG_CONFIG_HOME", ".config")):
        # Still fine: an empty result is valid; dirs are only scanned.
        pass
    tunings, searched = [], []
    for d in search_dirs():
        searched.append(d)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in sorted(files):
                if not f.endswith(".conf"):
                    continue
                entry = _scan_file(os.path.join(root, f))
                if entry:
                    tunings.append(entry)
    return {"searched": searched, "tunings": tunings}


def register(sub):
    p = sub.add_parser("tunings", help="list installed tuning sinks/configs "
                                       "(read-only)")
    p.set_defaults(func=run)
