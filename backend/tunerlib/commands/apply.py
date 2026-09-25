"""apply — install a generated PipeWire filter-chain config.

Registry contract: register(sub) + run(args) -> dict payload.

  apply [--write] [--dest DIR] [--sink NAME] [--name NAME] CONFIG

Copies a filter-chain conf (as produced by `generate`) into the PipeWire
drop-in directory, substituting the `@SPEAKER_SINK@` placeholder with the
target sink name.

Safety (AGENTS.md hard rule 5): dry-run is the DEFAULT. Without --write the
plan is executed into a temporary directory and reported — nothing outside
/tmp is touched. With --write, every file that would be overwritten gets a
timestamped backup first; if any write fails, all backups made in this run
are restored before returning.

Known limitations (also stated in --help):
  - does NOT restart PipeWire; run `systemctl --user restart pipewire`
    (or `wpctl` reload) after a real install for the chain to appear.
  - does not validate the conf's DSP semantics beyond the placeholder;
    limiter/alr/boost invariants are `generate`'s responsibility.
  - refuses to overwrite a drop-in of the same name unless it was written
    by this tool (contains our marker) or --force is given.

Data: {"applied","dry_run","dest","written":[{"path","backed_up_to"|null}],
       "backup_dir","sink","plan":[...],"limitations":[...]}
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .. import TunerError

MARKER = "# noctalia-audio-tuner apply"
DEFAULT_DROPIN_DIR = Path.home() / ".config" / "pipewire" / "pipewire.conf.d"


def substitute_placeholder(text: str, sink: str) -> str:
    return text.replace("@SPEAKER_SINK@", sink)


def build_plan(config: Path, dest_dir: Path, name: str) -> list[str]:
    """Human-readable plan lines (also returned in the payload)."""
    target = dest_dir / f"{name}.conf"
    plan = [f"read      {config}",
            "subst     @SPEAKER_SINK@ -> detected sink",
            f"install   {target}",
            "reload    NOT performed (run: systemctl --user restart pipewire)"]
    return plan


def install(config: Path, dest_dir: Path, name: str, sink: str,
            force: bool = False) -> dict:
    """Real install with per-file timestamped backups and restore-on-failure.

    Returns {"written": [{"path","backed_up_to"}], "backup_dir": str|None}.
    Raises TunerError on refusal or failure (after restoring backups).
    """
    text = config.read_text()
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{name}.conf"

    if target.exists() and not force:
        if MARKER not in target.read_text(errors="replace"):
            raise TunerError(
                "exists",
                f"{target} exists and was not written by this tool; use --force.",
            )

    backup_dir = None
    backups = []  # (original_path, backup_path)
    written = []
    try:
        if target.exists():
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_dir = dest_dir / f".noctalia-audio-tuner-backup-{stamp}"
            backup_dir.mkdir(exist_ok=True)
            bak = backup_dir / f"{name}.conf"
            shutil.copy2(target, bak)
            backups.append((target, bak))
        tmp = target.with_suffix(".conf.tmp")
        tmp.write_text(f"{MARKER}\n" + substitute_placeholder(text, sink))
        os.replace(tmp, target)
        written.append({"path": str(target), "backed_up_to":
                        str(backups[-1][1]) if backups else None})
    except Exception:
        for original, bak in backups:
            try:
                shutil.copy2(bak, original)
            except OSError:
                pass
        raise

    return {"written": written, "backup_dir": str(backup_dir) if backup_dir else None}


def register(sub) -> None:
    p = sub.add_parser(
        "apply",
        help="Install a generated PipeWire filter-chain (dry-run by default)",
        description="Install a filter-chain conf into the PipeWire drop-in "
        "directory, substituting @SPEAKER_SINK@. DRY-RUN IS THE DEFAULT: it "
        "executes the plan into a temporary directory and reports it. Use "
        "--write for a real install (timestamped backups, restore on "
        "failure). This tool does NOT restart PipeWire — run `systemctl "
        "--user restart pipewire` afterwards.",
    )
    p.add_argument("config", help="filter-chain .conf file (see `generate`)")
    p.add_argument("--write", action="store_true",
                   help="perform the real install (default: dry-run to a temp dir)")
    p.add_argument("--dest", default=None,
                   help=f"drop-in directory (default: {DEFAULT_DROPIN_DIR})")
    p.add_argument("--name", default="noctalia-audio-tuning",
                   help="drop-in name without .conf (default: %(default)s)")
    p.add_argument("--sink", default=None,
                   help="sink name to substitute for @SPEAKER_SINK@ "
                        "(required when the config contains the placeholder)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing drop-in not written by this tool")
    p.set_defaults(func=run)


def run(args) -> dict:
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        raise TunerError("no_response_file", f"config not found: {config}")
    text = config.read_text()

    sink = args.sink
    if "@SPEAKER_SINK@" in text and not sink:
        raise TunerError(
            "no_sink",
            "config contains @SPEAKER_SINK@; pass --sink (see `devices`).",
        )
    sink = sink or "(none — config has no placeholder)"

    dest_dir = Path(args.dest).expanduser() if args.dest else DEFAULT_DROPIN_DIR
    name = args.name.replace("/", "_")

    if not args.write:
        with tempfile.TemporaryDirectory(prefix="noctalia-apply-dryrun-") as tmp:
            result = install(config, Path(tmp), name, sink, force=True)
        for line in build_plan(config, dest_dir, name):
            print(f"  {line}", file=sys.stderr)
        print(f"  dry-run: plan executed under {result['backup_dir'] or tmp}; "
              f"nothing installed", file=sys.stderr)
        return {"applied": False, "dry_run": True, "dest": str(dest_dir),
                "written": [], "backup_dir": None, "sink": sink,
                "plan": build_plan(config, dest_dir, name)}

    result = install(config, dest_dir, name, sink, force=args.force)
    return {"applied": True, "dry_run": False, "dest": str(dest_dir),
            "written": result["written"], "backup_dir": result["backup_dir"],
            "sink": sink, "plan": build_plan(config, dest_dir, name)}
