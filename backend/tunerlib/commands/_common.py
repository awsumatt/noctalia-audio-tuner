"""Shared helpers for the ab-* commands (SA4). Registry contract is defined in
AGENTS.md: each module exposes register(sub) and run(args) -> dict payload.
The entry script (built by another agent) globs backend/tunerlib/commands/*.py
and wraps the payload in {"status":"ok","data":...}.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "noctalia-audio-tuner"


def session_file() -> Path:
    return state_dir() / "ab-session.json"


def save_session(data: dict) -> Path:
    session_file().parent.mkdir(parents=True, exist_ok=True)
    tmp = session_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, session_file())
    return session_file()


def load_session() -> dict | None:
    p = session_file()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_session() -> None:
    try:
        session_file().unlink()
    except FileNotFoundError:
        pass


def default_runner(argv):
    """Real subprocess runner -> (returncode, stdout, stderr)."""
    import subprocess
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def systemctl_user_runner(argv):
    import subprocess
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def ebur128_runner(argv):
    """Runner for ffmpeg ebur128 (stderr carries the readings)."""
    import subprocess
    return subprocess.run(argv, capture_output=True, text=True)


def register(sub):
    """No-op. `_common` is a private helper (the real entrypoint skips
    underscore-prefixed modules); this only satisfies test replicas that
    call register() on every module in the package."""
    return None
