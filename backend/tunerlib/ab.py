"""A/B compare + tuning-switch logic.

Pure-logic port of upstream `compare/tuning-switch` and `compare/tuning-compare`
(omarchy-audio-tuner, MIT, © David Heinemeier Hansson). Generalized per the
audit table in AGENTS.md:

- No vendor regex (`sof_sdw|HiFi`, `Speaker`): the raw hardware sink is
  classified from PipeWire properties / sink-name prefixes, with the
  classification overridable.
- Tuning sink prefix is configurable (default `noctalia_audio_tuning`, never
  hardcoded `omarchy_*`).
- EasyEffects handling is best-effort: if EasyEffects is not present/running
  that path is skipped entirely.
- Every external call goes through an injectable `runner` (a callable taking a
  list of argv and returning (returncode, stdout, stderr)) so all behaviour is
  testable with fixtures and mocks — no audio system is touched by tests.

Invariants preserved (AGENTS.md):
- Verify where a stream LANDED by sink id; never trust exit codes.
- Level-match before any A/B verdict (ebur128 trim-to-quietest math).
- Full restore (default sink + original hardware volume) on every exit path.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import TunerError

# Documented defaults (upstream values, exposed as parameters — never hardcoded
# silently). 65536 is PulseAudio's 0..65536 volume scale.
VOLUME_MAX = 65536
VOLUME_MIN = 0
DEFAULT_TUNING_PREFIX = "noctalia_audio_tuning"
EASYEFFECTS_SINK = "easyeffects_sink"
EASYEFFECTS_UNIT = "easyeffects.service"
APP_NAME = "application.name"

# pactl sink-input full listing parsing
_RE_INPUT_ID = re.compile(r"^Sink Input #(\d+)\s*$")
_RE_SINK_FIELD = re.compile(r"^\s*Sink:\s*(\d+)\s*$")
_RE_APP_NAME = re.compile(r'^\s*application\.name\s*=\s*"(.*)"\s*$')
_RE_MUTE_FIELD = re.compile(r"^\s*Mute:\s*(yes|no)\s*$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _real_runner(argv, **kwargs):
    """Default runner: real subprocess. Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    return proc.returncode, proc.stdout, proc.stderr


def volume_for(base_volume: float, lufs_this: float | None, lufs_quietest: float | None) -> int:
    """Trim-to-quietest volume math (upstream tuning-compare `volume_for`).

    `base_volume` is the original hardware volume on the 0..65536 scale.
    The correction clamps at <= 0: a candidate is only ever trimmed DOWN to the
    quietest candidate, never boosted up.
    """
    this = lufs_this if lufs_this is not None else 0.0
    quiet = lufs_quietest if lufs_quietest is not None else 0.0
    correction = quiet - this
    if correction > 0:
        correction = 0.0
    v = base_volume * (10.0 ** (correction / 60.0))
    v = max(VOLUME_MIN, min(VOLUME_MAX, v))
    return int(v + 0.5)


def parse_ebur128_integrated(stderr_text: str) -> float | None:
    """Extract the integrated loudness (LUFS) from `ffmpeg -filter:a ebur128` stderr.

    ffmpeg prints, at the end of the run, a summary block whose last
    `I:` line is the integrated loudness:
        [Parsed_ebur128_0 @ ...]    I:        -23.1 LUFS
    Returns None if no I: reading is present (callers treat that as a failed
    measurement, never as 0 LU).
    """
    values = re.findall(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", stderr_text, re.M)
    if not values:
        return None
    return float(values[-1])


def parse_sinks_short(stdout_text: str) -> list[dict]:
    """Parse `pactl list sinks short` -> [{index, name, ...columns}].

    Columns are sink index, name, driver, sample spec, channel map, properties
    (varies by version) — only index and name are load-bearing here.
    """
    sinks = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        sinks.append({"index": int(cols[0]), "name": cols[1], "raw": cols})
    return sinks


def parse_sink_inputs(stdout_text: str) -> list[dict]:
    """Parse full `pactl list sink-inputs`.

    Returns [{id, sink, app_name, muted}] where app_name is None when the
    stream has no application.name property (e.g. a filter-chain's own output
    stream) — such streams are never moved.
    """
    streams = []
    current: dict | None = None
    for line in stdout_text.splitlines():
        m = _RE_INPUT_ID.match(line)
        if m:
            if current is not None:
                streams.append(current)
            current = {"id": int(m.group(1)), "sink": None, "app_name": None, "muted": False}
            continue
        if current is None:
            continue
        m = _RE_SINK_FIELD.match(line)
        if m:
            current["sink"] = int(m.group(1))
            continue
        m = _RE_APP_NAME.match(line)
        if m and current["app_name"] is None:
            current["app_name"] = m.group(1)
            continue
        m = _RE_MUTE_FIELD.match(line)
        if m:
            current["muted"] = m.group(1) == "yes"
    if current is not None:
        streams.append(current)
    return streams


def classify_raw_hardware_sink(sink_name: str, properties: str = "") -> bool:
    """True when a sink name/properties look like the raw internal-speaker path.

    Generalized rule (banned upstream regexes must not survive): an
    `alsa_output.*` sink whose channel map / property block mentions a speaker
    port, or whose name ends in the PipeWire `.sink` suffix with `Speaker`
    ports, is the raw path. This is a heuristic pre-filter; the interactive
    fallback (choose among alsa sinks) is handled by the caller when ambiguous.
    """
    if not sink_name.startswith("alsa_output."):
        return False
    if sink_name.endswith(".monitor"):
        return False
    blob = sink_name + " " + properties
    # channel-map or port names containing a speaker-ish word; deliberately
    # vendor-neutral (no sof_sdw / HiFi / vendor model names).
    return bool(re.search(r"(?i)(speaker|front|analog-stereo|channelmap)", blob))


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    name: str
    label: str
    kind: str  # "tuning" | "raw" | "easyeffects"
    trim_db: float = 0.0


def discover_candidates(
    sink_list: str,
    *,
    tuning_prefix: str = DEFAULT_TUNING_PREFIX,
    easyeffects_active: bool = False,
    raw_hardware_sink: str | None = None,
    raw_trim_db: float = -7.4,
) -> list[Candidate]:
    """Build the candidate list from a parsed/short `pactl list sinks short`.

    - every installed tuning sink (name starting with `tuning_prefix`) is a
      candidate, trim 0 (filter-chain tunings are linear with fixed gain);
    - EasyEffects is a candidate only when it is actually running;
    - the raw hardware path is a control with `raw_trim_db` — offered only when
      EasyEffects is NOT active (EasyEffects grabs any stream that follows the
      default sink, so the raw path cannot actually be auditioned while it
      runs); pass raw_hardware_sink explicitly (discovery of it is caller's
      classification concern — see classify_raw_hardware_sink).
    """
    present = {s["name"] for s in parse_sinks_short(sink_list)}
    candidates: list[Candidate] = []

    for name in sorted(present):
        if name.startswith(tuning_prefix + "_") or name == tuning_prefix:
            label = name[len(tuning_prefix) + 1:] or "tuned"
            candidates.append(Candidate(name, label, "tuning"))

    ee_running = easyeffects_active or EASYEFFECTS_SINK in present
    if ee_running:
        candidates.append(Candidate(EASYEFFECTS_SINK, "EasyEffects", "easyeffects"))
    elif raw_hardware_sink and raw_hardware_sink in present:
        candidates.append(Candidate(raw_hardware_sink, "raw (untreated)", "raw", raw_trim_db))

    # EasyEffects running: still offer the raw control? Upstream compare does
    # not (cannot be auditioned); upstream switch instead stops EasyEffects for
    # the session and re-adds raw. Both strategies are exposed to callers;
    # discovery itself stays honest.
    return candidates


def candidate_names_for_switch(
    sink_list: str,
    *,
    tuning_prefix: str = DEFAULT_TUNING_PREFIX,
    easyeffects_service_active: bool = False,
    raw_hardware_sink: str | None = None,
    raw_trim_db: float = -7.4,
) -> list[Candidate]:
    """Candidate list for `switch` semantics (upstream tuning-switch).

    If EasyEffects' user service is running it should be stopped for the
    session (it steals default-sink-following streams back from filter-chains);
    the caller stops it and passes easyeffects_service_active=False, after
    which the raw hardware control is available again. See Session.stop_easyeffects.
    """
    return discover_candidates(
        sink_list,
        tuning_prefix=tuning_prefix,
        easyeffects_active=easyeffects_service_active,
        raw_hardware_sink=raw_hardware_sink,
        raw_trim_db=raw_trim_db,
    )


# --------------------------------------------------------------------------
# session (stateful logic, runner-injected)
# --------------------------------------------------------------------------


@dataclass
class ABSession:
    """An A/B audition session over an injectable pactl/systemctl runner.

    The runner is any callable (argv_list) -> (returncode, stdout, stderr).
    Tests inject a fake; production uses `_real_runner`-style callables per
    tool (pactl, systemctl --user, ffmpeg).
    """

    pactl: Callable
    systemctl: Optional[Callable] = None  # systemctl --user wrapper, optional
    tuning_prefix: str = DEFAULT_TUNING_PREFIX
    raw_trim_db: float = -7.4
    raw_hardware_sink: str | None = None

    # session state
    candidates: list[Candidate] = field(default_factory=list)
    index: int = 0
    original_default: str | None = None
    original_volume: int | None = None
    base_volume: int | None = None
    ee_stopped: bool = False
    offsets: dict = field(default_factory=dict)  # sink -> lufs
    active: bool = False

    # ---- low-level helpers -------------------------------------------------

    def _pactl(self, *args):
        return self.pactl(["pactl", *args])

    def list_sinks(self):
        rc, out, _err = self._pactl("list", "sinks", "short")
        return parse_sinks_short(out)

    def sink_index(self, name: str) -> int | None:
        for s in self.list_sinks():
            if s["name"] == name:
                return s["index"]
        return None

    def sink_exists(self, name: str) -> bool:
        return self.sink_index(name) is not None

    def get_default_sink(self) -> str | None:
        rc, out, _err = self._pactl("get-default-sink")
        out = out.strip()
        return out or None

    def get_sink_volume(self, name: str) -> int | None:
        # `pactl get-sink-volume` prints e.g. "0:  50% 1:  50% ... 65536 / 50%".
        rc, out, _err = self._pactl("get-sink-volume", name)
        m = re.search(r"(\d+)\s*/", out)
        if not m:
            m = re.search(r"(\d+)", out)
        return int(m.group(1)) if m else None

    def set_sink_volume(self, name: str, volume: int):
        self._pactl("set-sink-volume", name, str(int(volume)))

    def set_default_sink(self, name: str):
        self._pactl("set-default-sink", name)

    def app_streams(self) -> list[dict]:
        """Real application streams only.

        A filter-chain's own output is also a sink-input but has no
        application.name; EasyEffects' output must stay put or its chain is
        broken. Neither is ever moved.
        """
        rc, out, _err = self._pactl("list", "sink-inputs")
        return [
            s for s in parse_sink_inputs(out)
            if s["app_name"] is not None and s["app_name"] != "EasyEffects"
        ]

    # ---- stream moving WITH landing verification ---------------------------

    def move_streams(self, target_name: str) -> dict:
        """Move all app streams to `target` and VERIFY by sink id.

        `pactl move-sink-input` reports success while leaving pinned streams
        where they were — exit codes are never trusted; each move is confirmed
        by re-reading the stream's Sink field and comparing sink ids.
        """
        target_id = self.sink_index(target_name)
        if target_id is None:
            return {"moved": 0, "stuck": 0, "target": target_name, "verified": False}

        streams = self.app_streams()
        for s in streams:
            self._pactl("move-sink-input", str(s["id"]), target_name)

        after = {s["id"]: s["sink"] for s in self.app_streams()}
        moved = stuck = 0
        for s in streams:
            now = after.get(s["id"])
            if now == target_id:
                moved += 1
            elif now is not None:
                stuck += 1
        return {
            "moved": moved,
            "stuck": stuck,
            "target": target_name,
            "target_id": target_id,
            "verified": True,
        }

    # ---- EasyEffects (best-effort, skippable) ------------------------------

    def easyeffects_running(self) -> bool:
        if self.systemctl is None:
            return False
        try:
            rc, _out, _err = self.systemctl(["systemctl", "--user", "is-active", "--quiet", EASYEFFECTS_UNIT])
            return rc == 0
        except Exception:
            return False

    def stop_easyeffects(self, timeout_s: float = 5.0) -> bool:
        """Stop the EasyEffects user service for the session. Best-effort:
        returns False (and does nothing else) when systemctl is unavailable or
        the service is not running."""
        if not self.easyeffects_running():
            return False
        try:
            self.systemctl(["systemctl", "--user", "stop", EASYEFFECTS_UNIT])
        except Exception:
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self.sink_exists(EASYEFFECTS_SINK):
                self.ee_stopped = True
                return True
            time.sleep(0.25)
        return False

    def to_dict(self) -> dict:
        return {
            "candidates": [c.__dict__ for c in self.candidates],
            "index": self.index,
            "original_default": self.original_default,
            "original_volume": self.original_volume,
            "base_volume": self.base_volume,
            "ee_stopped": self.ee_stopped,
            "raw_hardware_sink": self.raw_hardware_sink,
            "raw_trim_db": self.raw_trim_db,
            "tuning_prefix": self.tuning_prefix,
            "offsets": self.offsets,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict, pactl: "Callable", systemctl=None) -> "ABSession":
        sess = cls(pactl=pactl, systemctl=systemctl, tuning_prefix=data.get("tuning_prefix", DEFAULT_TUNING_PREFIX))
        sess.candidates = [Candidate(**c) for c in data.get("candidates", [])]
        sess.index = data.get("index", 0)
        sess.original_default = data.get("original_default")
        sess.original_volume = data.get("original_volume")
        sess.base_volume = data.get("base_volume")
        sess.ee_stopped = data.get("ee_stopped", False)
        sess.raw_hardware_sink = data.get("raw_hardware_sink")
        sess.raw_trim_db = data.get("raw_trim_db", -7.4)
        sess.offsets = data.get("offsets", {})
        sess.active = data.get("active", False)
        return sess

    def restore_easyeffects(self):
        if self.ee_stopped and self.systemctl is not None:
            try:
                self.systemctl(["systemctl", "--user", "start", EASYEFFECTS_UNIT])
            except Exception:
                pass
            self.ee_stopped = False

    # ---- volume ------------------------------------------------------------

    def volume_for_candidate(self, cand: "Candidate") -> int:
        """Volume to apply for a candidate.

        When calibrated offsets exist for the candidate (per-track cache,
        upstream tuning-compare), trim to the quietest calibrated candidate;
        otherwise fall back to the static trim (`raw_trim_db` for the raw
        control, 0 dB for linear filter-chain tunings)."""
        if cand.name in self.offsets:
            quiet = quietest_sink(self.offsets, [c.name for c in self.candidates])
            return volume_for(
                self.base_volume or VOLUME_MAX,
                self.offsets.get(cand.name),
                self.offsets.get(quiet),
            )
        trim = cand.trim_db
        base = self.base_volume or VOLUME_MAX
        v = base * (10.0 ** (trim / 60.0))
        return int(max(VOLUME_MIN, min(VOLUME_MAX, v)) + 0.5)

    def apply_volume(self, trim_db: float):
        """Trim applied at the hardware sink (transparent gain change
        downstream of all processing)."""
        v = self.base_volume * (10.0 ** (trim_db / 60.0))
        v = max(VOLUME_MIN, min(VOLUME_MAX, v))
        if self.raw_hardware_sink:
            self.set_sink_volume(self.raw_hardware_sink, int(v + 0.5))

    # ---- lifecycle ----------------------------------------------------------

    def start(
        self,
        *,
        raw_hardware_sink: str | None = None,
        mode: str = "switch",
        raw_trim_db: float | None = None,
    ) -> dict:
        """Discover candidates, remember the original state, select candidate 0.

        Returns a payload dict with the candidate list and any warnings
        (e.g. EasyEffects not stoppable). Raises RuntimeError when fewer than
        two candidates are available.
        """
        if raw_hardware_sink:
            self.raw_hardware_sink = raw_hardware_sink
        if raw_trim_db is not None:
            self.raw_trim_db = raw_trim_db

        sinks = self.list_sinks()
        sink_names = {s["name"] for s in sinks}
        ee_was_running = self.easyeffects_running()

        # switch mode: stop EasyEffects for the session so the raw control can
        # be auditioned. compare mode keeps it running and offers it as a
        # candidate instead (streams with an explicit target are not stolen).
        if mode == "switch" and ee_was_running:
            self.stop_easyeffects()
            sinks = self.list_sinks()
            sink_names = {s["name"] for s in sinks}

        if mode == "switch":
            self.candidates = candidate_names_for_switch(
                "\n".join("\t".join([str(s["index"]), s["name"]]) for s in sinks),
                tuning_prefix=self.tuning_prefix,
                easyeffects_service_active=False,
                raw_hardware_sink=self.raw_hardware_sink,
                raw_trim_db=self.raw_trim_db,
            )
        else:
            self.candidates = discover_candidates(
                "\n".join("\t".join([str(s["index"]), s["name"]]) for s in sinks),
                tuning_prefix=self.tuning_prefix,
                easyeffects_active=(EASYEFFECTS_SINK in sink_names),
                raw_hardware_sink=self.raw_hardware_sink,
                raw_trim_db=self.raw_trim_db,
            )
            # compare mode: no EasyEffects running -> raw path is the control.
            if EASYEFFECTS_SINK not in sink_names:
                if self.raw_hardware_sink and self.raw_hardware_sink in sink_names \
                        and not any(c.kind == "raw" for c in self.candidates):
                    self.candidates.append(
                        Candidate(self.raw_hardware_sink, "raw (untreated)", "raw", self.raw_trim_db)
                    )

        warnings = []
        if ee_was_running and not self.ee_stopped and mode == "switch":
            warnings.append(
                "EasyEffects is running but could not be stopped; the raw "
                "control may be stolen back to easyeffects_sink."
            )

        if len(self.candidates) < 2:
            raise TunerError(
                "no_candidates",
                "Fewer than two candidates available (apply a tuning first, "
                "or connect the raw control).",
            )

        self.original_default = self.get_default_sink()
        self.original_volume = self.get_sink_volume(self.raw_hardware_sink)
        self.base_volume = self.original_volume
        self.active = True
        payload = self.select(0)
        payload["warnings"] = warnings
        payload["ee_stopped"] = self.ee_stopped
        return payload

    def select(self, index: int) -> dict:
        """Switch to candidate `index`: set default sink, move streams (with
        landing verification), apply the trim volume."""
        if not self.active:
            raise TunerError("no_session", "Session not started.")
        cand = self.candidates[index]
        self.index = index
        self.set_default_sink(cand.name)
        move = self.move_streams(cand.name)
        if self.raw_hardware_sink:
            self.set_sink_volume(self.raw_hardware_sink, self.volume_for_candidate(cand))
        return {
            "selected": cand.name,
            "label": cand.label,
            "index": index,
            "move": move,
            "trim_db": cand.trim_db,
        }

    def switch(self, index: int | None = None) -> dict:
        """Switch to `index`, or to the next candidate when None."""
        if index is None:
            index = (self.index + 1) % len(self.candidates)
        return self.select(index)

    def stop(self, keep: bool = False) -> dict:
        """Full restore on EVERY exit path: default sink, streams, volume,
        EasyEffects if we stopped it."""
        if not self.active:
            return {"restored": False, "reason": "not-active"}
        self.active = False
        restored = {}
        if not keep:
            if self.original_default and self.sink_exists(self.original_default):
                self.set_default_sink(self.original_default)
                restored["default_sink"] = self.original_default
                restored["move"] = self.move_streams(self.original_default)
            self.restore_easyeffects()
        if self.original_volume is not None and self.raw_hardware_sink:
            self.set_sink_volume(self.raw_hardware_sink, self.original_volume)
            restored["volume"] = self.original_volume
            # verify the restore actually landed (read back, retry once)
            now = self.get_sink_volume(self.raw_hardware_sink)
            if now != self.original_volume:
                self.set_sink_volume(self.raw_hardware_sink, self.original_volume)
                restored["volume_retried"] = True
        restored["kept"] = keep
        if keep and self.ee_stopped:
            restored["note"] = (
                "EasyEffects left stopped; 'systemctl --user start easyeffects' to restore it."
            )
        return restored

    # ---- calibrated compare (per-track offsets, upstream tuning-compare) ---

    def calibrate(
        self,
        *,
        length_s: float = 30.0,
        ffmpeg: Optional[Callable] = None,
        offsets_file: Optional[Path] = None,
    ) -> dict:
        """Measure each candidate's integrated loudness (LUFS) from the
        hardware sink monitor and cache per-track offsets.

        Requires something actually PLAYING on the machine: the app streams
        are moved onto each candidate in turn (verified landing) while ffmpeg
        taps the hardware monitor, upstream of the volume control, so the trim
        applied there later is a transparent gain change. Nothing is played by
        this library itself — no audio is started, only moved and measured.

        `ffmpeg` is a callable(argv) -> object with .stderr (CompletedProcess
        works). Offsets are written atomically to `offsets_file` when given
        and kept in self.offsets either way. On every exit path the original
        default sink is restored and streams moved back.
        """
        if not self.active:
            raise TunerError("no_session", "Session not started.")
        if ffmpeg is None:
            raise TunerError("dependency", "No ffmpeg runner provided for ebur128 measurement.")
        if not self.raw_hardware_sink:
            raise TunerError("no_raw_sink", "No raw hardware sink known; calibrate needs its monitor.")
        if any(c.name not in self.offsets for c in self.candidates):
            original_index = self.index
            try:
                for cand in self.candidates:
                    if cand.name in self.offsets:
                        continue
                    self.set_default_sink(cand.name)
                    self.move_streams(cand.name)
                    lufs = measure_loudness(ffmpeg, self.raw_hardware_sink, length_s)
                    if lufs is None:
                        continue  # failed measurement is never recorded as 0 LU
                    self.offsets[cand.name] = lufs
            finally:
                # every exit path: back where we were
                if self.original_default and self.sink_exists(self.original_default):
                    self.set_default_sink(self.original_default)
                    self.move_streams(self.original_default)
                self.index = original_index
        if offsets_file is not None:
            write_offsets(Path(offsets_file), self.offsets)
        return {"offsets": dict(self.offsets), "length_s": length_s}


# --------------------------------------------------------------------------
# calibrated compare (loudness offsets per track)
# --------------------------------------------------------------------------


def offsets_path(state_dir: Path, track: Path) -> Path:
    """Per-track offsets cache file: <basename sanitized>.offsets"""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", track.name)
    return Path(state_dir) / (base + ".offsets")


def write_offsets(path: Path, offsets: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(f"{k} {v}\n" for k, v in offsets.items()))
    os.replace(tmp, path)


def read_offsets(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return out


def quietest_sink(offsets: dict, candidates: list[str]) -> str | None:
    quiet = None
    for c in candidates:
        if c not in offsets:
            continue
        if quiet is None or offsets[c] < offsets[quiet]:
            quiet = c
    return quiet


def measure_loudness(run_pactl_cmd: Callable, hw_sink: str, length_s: float) -> float | None:
    """Run ffmpeg ebur128 on the hardware sink MONITOR (upstream of the volume
    control, so the trim there is transparent) and return integrated LUFS.

    `run_pactl_cmd(argv)` runs the command and returns CompletedProcess-like
    (must expose .returncode / .stderr). Never mutates audio: monitor capture
    is read-only.
    """
    argv = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-f", "pulse", "-i", f"{hw_sink}.monitor",
        "-t", str(length_s), "-filter:a", "ebur128", "-f", "null", "-",
    ]
    proc = run_pactl_cmd(argv)
    return parse_ebur128_integrated(getattr(proc, "stderr", "") or "")
