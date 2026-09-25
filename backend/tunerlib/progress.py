"""Progress + envelope printing shared by command modules.

With --progress, long-running commands stream NDJSON lines to stdout:

    {"event": "progress", "pct": 0-100, "msg": "..."}
    ...
    {"event": "done", "data": {...}}      # printed by the entrypoint

Without --progress, stdout carries only the single JSON envelope.
"""

import json


def progress(args, pct: int, msg: str) -> None:
    """Emit a progress NDJSON line when --progress was given; else stderr chat."""
    if getattr(args, "progress", False):
        print(json.dumps({"event": "progress",
                          "pct": max(0, min(100, int(pct))),
                          "msg": msg}), flush=True)
