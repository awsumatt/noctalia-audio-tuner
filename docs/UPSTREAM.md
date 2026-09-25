# UPSTREAM.md — upstream mapping and audit

Maps the upstream **omarchy-audio-tuner** toolkit (`github.com/omacom/omarchy-audio-tuner`,
MIT, © David Heinemeier Hansson) to this repo, per the audit table in
[AGENTS.md](../AGENTS.md). This document covers the SA4 slice (A/B compare +
switch + apply + docs + CI); SA1/SA2 mappings live in their PRs.

Status values: **ported** (faithful, logic-preserving), **modified**
(ported with generalizations), **generalized** (rewritten to remove
machine-specific assumptions), **stubbed** (mechanism kept, part deliberately
dropped — stated explicitly).

## compare/tuning-switch → backend/tunerlib/ab.py + commands/ab_{start,switch,stop}.py

| Upstream element | Ported to | Status | Notes |
|---|---|---|---|
| `sof_sdw\|HiFi` + `Speaker` vendor regex for the raw sink (`tuning-switch:21-22`) | `ab.classify_raw_hardware_sink()` + `commands/ab_start.find_raw_hardware_sink()` | generalized | Property/name-shape heuristic over `alsa_output.*` sinks (speaker-ish words, no vendor names); ambiguous → caller must pass `--sink`. `devices` (SA1) is the classified source of truth. |
| `omarchy_speaker_tuning` / `omarchy_tuning_*` sink names (`:41-45`) | `ab.DEFAULT_TUNING_PREFIX` = `noctalia_audio_tuning`, `--prefix` flag | generalized | Prefix configurable; discovery matches `<prefix>` and `<prefix>_*`. |
| Interactive number-key loop (`stty`/`read`, `:159-201`) | `ab-start` / `ab-switch` / `ab-stop` subcommands | modified | The headless JSON backend cannot own a TTY; the interactive loop is the Noctalia plugin's job. Session state (candidates, index, originals) persists in `$XDG_STATE_HOME/noctalia-audio-tuner/ab-session.json` between calls. |
| EasyEffects stop-for-session (`:52-60`) + restore on exit (`:145`) | `ABSession.stop_easyeffects/restore_easyeffects` | ported | Best-effort via `systemctl --user`; if absent/unstoppable the raw path warning is reported instead of failing. |
| `app_streams` (skip filter-chain outputs, skip EasyEffects' own stream, `:78-87`) | `ABSession.app_streams` | ported | Streams without `application.name` and EasyEffects' stream are never moved. |
| `move_streams` with landing verification (`:89-113`) | `ABSession.move_streams` | ported | `pactl move-sink-input` rc is ignored; every stream's `Sink:` field is re-read and compared to the target sink id. Pinned streams counted as `stuck`. |
| Trim math at the hardware sink (`apply_volume`, `:115-125`) | `ABSession.apply_volume` / `volume_for_candidate` | ported | `v = base * 10^(t/60)`, clamped 0..65536 (PulseAudio scale), applied at the raw hardware sink (downstream-transparent). The `/60` exponent is upstream's exact formula, kept faithfully. |
| `--trim` default `-7.4` (`:18`) | `ab.raw_trim_db` / `ab-start --trim` | ported | Exposed as a flag, documented default. |
| Volume +/- keys with 3 dB steps (`:185-193`) | **stubbed** (dropped) | — | Interactive-only; the plugin UI can adjust `pactl set-sink-volume` directly. No backend replacement. |
| `keep` (k key, `:136-140,195-198`) | `ab-stop --keep` | ported | Keeps the selected candidate; reports EasyEffects left stopped. |
| `cleanup` restore on EXIT (`:135-148`) | `ABSession.stop` | ported + hardened | Restores default sink, moves streams back (verified), restores volume **with read-back and one retry** (upstream `restore_volume` did the same read-back; kept). |

## compare/tuning-compare → ab.py (calibrated-compare part)

| Upstream element | Ported to | Status | Notes |
|---|---|---|---|
| Candidate list incl. EasyEffects-if-running; raw withheld while EE runs (`:55-64`) | `ab.discover_candidates` | ported | Same withholding rule. |
| mpv per-candidate playback loop (`play_on`, `:92-118`) | **stubbed** (dropped) | — | The backend never starts playback (CI/test safety; the plugin or the user owns the player). Calibration and switching move *your already-playing* streams instead. |
| Landing verification of the mpv stream (`:107-117`) | `ABSession.move_streams` | ported | Same verify-by-sink-id rule for whatever streams are present. |
| ebur128 measurement from the hardware monitor, upstream of the volume control (`measure_loudness`, `:120-124`) | `ab.measure_loudness` | ported | `ffmpeg -f pulse -i <hw>.monitor -t LEN -filter:a ebur128`; last `I:` line. Monitor tap is read-only. |
| Per-track offsets cache `$XDG_STATE_HOME/omarchy-audio-tuning/<track>.offsets` (`:126-145`) | `ab.offsets_path/write_offsets/read_offsets` under `$XDG_STATE_HOME/noctalia-audio-tuner` | generalized | Atomic write (.tmp + rename); sanitized basename; path renamed per the audit table. |
| `volume_for` trim-to-quietest (`:157-169`) | `ab.volume_for` | ported | Correction clamped ≤ 0 (never boosted up); 65536 scale. |
| quietest candidate selection (`:147-155`) | `ab.quietest_sink` | ported | |
| calibration session (`:128-140`) | `ABSession.calibrate` | modified | No mpv launching (see above): calibration measures whatever is playing after moving streams to each candidate; restore-on-error guaranteed via try/finally. A failed measurement is skipped, never recorded as 0 LU. |
| `easyeffects --bypass 2` before selecting EE (`:98-100`) | **stubbed** (dropped) | — | Requires the `easyeffects` CLI in compare mode; the plugin can issue it. Documented as a limitation, not silently lost. |

## generate/gen-filter-chain.py → commands/apply.py (partial; exporter itself is SA2)

| Upstream element | Ported to | Status | Notes |
|---|---|---|---|
| `@SPEAKER_SINK@` placeholder (`gen-filter-chain.py:131`) | `apply.substitute_placeholder` | ported | Placeholder mechanism kept; substituted at install time with `--sink` (required when present). |
| conf install into PipeWire drop-in dir | `apply` command | modified | **Dry-run is the default** (executes into a temp dir, reports plan). `--write` makes timestamped backups of every touched file and restores them on failure. Upstream had no install step to port (their script only writes the conf); this is a faithful-spirit implementation, not a line port. |

## Deliberately stripped (machine-specific audit, AGENTS.md)

- Vendor-name regexes (`sof_sdw|HiFi`, `Speaker` in sink names) — replaced by
  property-based classification (SA1 `devices`) + neutral shape heuristic here.
- `omarchy_*` sink-name prefix — replaced by configurable `--prefix`
  (default `noctalia_audio_tuning`).
- Hardcoded 48000 — nothing in SA4 bakes in a sample rate; loudness
  measurement is rate-agnostic (ebur128 on the monitor).
- `~/.local/state/omarchy-audio-tuning` cache path — moved to
  `$XDG_STATE_HOME/noctalia-audio-tuner`.
- EasyEffects systemd interplay — best-effort only; entirely skipped when
  EasyEffects is not installed/running (no hard dependency).
- `-7.4 dB` raw trim, `-6 dBFS`/`-14 LUFS` — kept as **documented defaults,
  exposed as flags**, never hardcoded silently.

## Untested live (honest)

The A/B path mutates live audio state (default sink, stream routing, hardware
volume) and needs running players. All tests are fixture/mocked-only
(`backend/tests/test_ab.py`, in-memory pactl/systemctl/ffmpeg fakes). The
pactl output *parsers* are exercised against realistic text, but no command
below has been run against real hardware in this repo:

- `ab-start` / `ab-switch` / `ab-stop` end-to-end on real streams
- real ebur128 calibration (ffmpeg monitor tap timings, pinned-stream
  behaviour of real PipeWire)
- real `apply --write` into `~/.config/pipewire/pipewire.conf.d` + PipeWire
  reload
