# noctalia-audio-tuner

A [Noctalia Shell v5](https://docs.noctalia.dev/noctalia/) plugin that measures a
laptop's speakers and produces PipeWire tunings — a cross-hardware port of the
[omarchy-audio-tuner](https://github.com/omacom/omarchy-audio-tuner) toolkit.
**Work in progress** — the implementation plan lives in [AGENTS.md](AGENTS.md).

- **Plugin** (`plugin/`): bar widget, panel (device picker, measurement wizard,
  A/B compare), launcher quick-switcher — Luau, per the v5 plugin contract.
- **Backend** (`backend/`): headless `noctalia-audio-tuner` Python CLI with a
  JSON protocol; all DSP/measurement logic lives here.

MIT licensed (see LICENSE and NOTICE for upstream attribution).

## Status

| Slice | Status |
|---|---|
| SA1 — backend measure core (`devices`, `probe`, `capture`, `analyse`, `delta`, `tone`, `mic-sweep`, `doctor`) | merged on `main` |
| SA2 — fit + exporters (`fit`, `generate`, `tunings`) | separate branch |
| SA3 — Noctalia plugin shell | separate branch |
| SA4 — A/B + switch + apply + docs + CI (this branch) | **open PR, not live-verified** |

Honest caveat: everything is unit-tested against simulated fixtures/mocks
only. No claim is made about behaviour on hardware other than the author's
machine, and the A/B, calibration and install paths have not been exercised
against live audio in CI (CI has no sound device).

## Quickstart (backend)

```sh
# dependencies (Arch): sudo pacman -S python ffmpeg mpv pipewire-pulse
ln -s "$PWD/backend/noctalia-audio-tuner" ~/.local/bin/noctalia-audio-tuner
noctalia-audio-tuner doctor     # dependency check (JSON, actionable hints)
noctalia-audio-tuner devices    # classified sinks/sources (JSON)
```

Full install instructions (generic distros, plugin path source + enable):
[docs/INSTALL.md](docs/INSTALL.md).

### A/B a tuning against the untreated speakers

```sh
noctalia-audio-tuner ab-start            # discover candidates, pick the first
noctalia-audio-tuner ab-switch           # next candidate (streams move, verified)
noctalia-audio-tuner ab-switch --name noctalia_audio_tuning_shipped
noctalia-audio-tuner ab-stop             # full restore (idempotent)
noctalia-audio-tuner ab-stop --keep      # keep the selected candidate
```

`ab-switch` verifies where each stream actually **landed** (by sink id —
`pactl move-sink-input` lies), and trims the hardware volume so no candidate
wins on loudness. Install a generated filter-chain with
`noctalia-audio-tuner apply CONF` — **dry-run by default**; `--write` makes
timestamped backups and restores on failure. See `--help` on each command and
the command contract in [backend/tunerlib/README.md](backend/tunerlib/README.md).

## Docs

- [docs/INSTALL.md](docs/INSTALL.md) — dependencies, backend + plugin install
- [docs/UPSTREAM.md](docs/UPSTREAM.md) — file-by-file upstream mapping, what
  was generalized/stripped, what is untested live
- [AGENTS.md](AGENTS.md) — implementation plan, hard rules, invariants
- [backend/tunerlib/README.md](backend/tunerlib/README.md) — backend JSON
  protocol + command registry contract

## Development

```sh
python3 -m compileall backend
python3 -m unittest discover -s backend/tests -t backend
```

Stdlib only — no pytest, no third-party deps. CI (`.github/workflows/ci.yml`)
runs compile + unittest plus lint jobs (ruff/shellcheck/luau-analyze, CI-only
tools); it performs **no** runtime audio verification.
