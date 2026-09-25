# AGENTS.md — noctalia-audio-tuner

Implementation plan for porting **omarchy-audio-tuner** (local checkout: `~/Projects/omarchy-audio-tuner`) — a measure → fit → generate → A/B toolkit for laptop speaker tunings on PipeWire — into a **Noctalia Shell v5 plugin** that works across arbitrary hardware, not just the machine it was authored on.

Any agent working in this repo MUST read this file first and follow the "Hard rules" section. Read only the sections your slice needs beyond that.

## Provenance

| What | Where |
|---|---|
| Upstream toolkit (source of all DSP logic) | `~/Projects/omarchy-audio-tuner` (MIT, © David Heinemeier Hansson). Local checkout; upstream GitHub URL not verified — do not invent one, check `git -C ~/Projects/omarchy-audio-tuner remote -v` before citing. |
| Upstream README (pipeline semantics, traps) | `~/Projects/omarchy-audio-tuner/README.md` — READ IT. The "Traps" section encodes real measurement bugs; the port must not regress them. |
| Noctalia v5 docs (authoritative) | <https://docs.noctalia.dev/noctalia/plugins/development/> — subpages: `manifest/`, `entries/`, `declarative-ui/`, `runtime-api/`, `plugin-api/`, `workflow/`. NOTE: `/v5/...` URLs 404; use `/noctalia/...`. |
| Vendored knowledge package | `references/v5-plugin-knowledge/` (SKILL.md + references/*.md, cloned from `github.com/dc-ja/noctalia-v5-plugin-development`) |
| Vendored type definitions | `references/noctalia.d.luau` (from `github.com/noctalia-dev/official-plugins`, MIT) — the exhaustive API surface; **never call a function not declared there** |
| Vendored reference plugin | `references/example/` (widget, service, shortcut, launcher, panel — copy its idioms) |
| Noctalia installed locally | 5.1.0 (pacman `noctalia`), plugin dirs under `~/.local/share/noctalia/` |

## Hard rules (subagent conduct)

1. **Do not invent APIs.** Every `noctalia.*` / `ui.*` / `barWidget.*` / `panel.*` / `launcher.*` call must exist in `references/noctalia.d.luau` or the vendored docs. APIs are level-gated: prefer the level-9 surface (see "v5 contract"); anything ≥ 22 (require modules, argv-table exec), ≥ 23 (`readFileAsync`), ≥ 26 (`getSetting`) is off-limits in v1.
2. **Do not push to `main`.** Commit to your assigned branch and open a PR (`gh pr create --fill`). Do not merge, do not rebase/rewrite history, do not force-push. Do not publish to any plugin catalog.
3. **Do not claim cross-hardware verification.** This machine is the only runtime test target. Validity claims must be design-level + simulated-fixture tests, stated as such.
4. **All backend output is JSON.** The Luau side must never parse human text (no awk-style scraping of `pactl`/`mpv` output from Luau). Human-readable messages go to stderr, JSON to stdout (protocol below).
5. **Never mutate live audio config without a backup path.** Any command that writes PipeWire/Omarchy/EasyEffects config must support `--dry-run` (default off in CLI, forced in the plugin until the user confirms), write a timestamped backup first, and restore on failure. Tuning sinks are additive (a filter-chain node), never destructive to existing config.
6. **Respect the upstream invariants** (see "Invariants"). A port that drops the limiter or re-boosts bass is a failed port even if it runs.
7. **Dependencies are declared, never assumed.** Manifest `dependencies` + a `doctor` command check `python`, `ffmpeg`, `mpv`, `pactl/wpctl`, and optional `lsp-plugins-lv2` at runtime with actionable errors.

## Machine-specific audit (what must NOT survive the port)

Found in upstream and banned here, or generalized:

| Upstream hardcode | Where | Porting rule |
|---|---|---|
| `sof_sdw\|HiFi` + `Speaker` regex for the physical speaker sink | `capture.sh:63`, `mic-sweep.sh:24`, `tuning-switch:22`, `tuning-compare:19` | Discovery must classify sinks by PipeWire properties (`media.class`, port availability, `api.alsa.*` names), with an interactive fallback, never a vendor regex |
| `omarchy_speaker_tuning` / `omarchy_tuning_*` sink names | switch/compare/gen-filter-chain | Prefix configurable via setting; default `noctalia_audio_tuning` |
| `@SPEAKER_SINK@` placeholder | `gen-filter-chain.py:131` | Keep the placeholder mechanism; substitute the detected sink at install time |
| `FS = 48000` hardcoded | `multitone.py`, `analyse-dense.py`, `fit-eq.py`, `mic-sweep.sh` | Sample rate is a parameter: detect from the sink's actual rate (`pw-dump`/`pactl`), integer-Hz + bin-aligned DFT requirement is preserved at any rate |
| XPS 14-tuned `LAYOUT` prior (14 sections) | `fit-eq.py:54` | Keep as the default "small laptop drivers" profile; make layout profiles data (JSON/TOML), selectable per tuning |
| Cache/state paths `~/.cache/omarchy-audio-tuner`, `~/.local/state/omarchy-audio-tuning` | measure/compare | Move under `noctalia-audio-tuner` names (XDG-respecting); per-tuning artifacts under `noctalia.pluginDataDir()` on the plugin side |
| EasyEffects systemd unit + `easyeffects_sink` interplay | switch/compare | Detect EasyEffects; if absent, skip that code path entirely. Unit name/service discovery is best-effort |
| `-6 dBFS` probe peak, `-14 LUFS` target, 65536 volume scale | measure/switch | Keep values as documented defaults, expose as flags/settings, never hardcode silently |

## Invariants (copied from upstream — regressions are bugs)

- Probes are dense (1/12-octave), pink-weighted, **integer-Hz tones analysed over exactly one second** so every tone lands on a DFT bin — leakage-free. Never reintroduce third-octave bandpass measurement.
- End every generated chain in a **lookahead limiter** with `"alr" = 0` and `"boost" = 0` (programme-dependent gain shifts tone with level otherwise).
- Never boost what small drivers cannot deliver; the default layout's high-pass/bass-cap ranges encode this.
- Verify where a stream **landed** (by sink id), never trust exit codes — `mpv --audio-device` silently falls back to the default sink and `pactl move-sink-input` reports success while pinning streams.
- Level-match before any A/B verdict; loudness wins blind comparisons otherwise.
- A tuning must report `magnitude_rms_db`, `bass_group_delay_swing_ms`, `limiter_headroom_db`, `dynamic_range_delta_lu` (see upstream README "What a tuning must report").

## Target architecture

```
Noctalia shell (Luau, UI layer — thin clients)
  [[service]] core     orchestrator: polls PipeWire, runs backend commands,
                       publishes progress/state via noctalia.state; owns measurement sessions
  [[widget]] audio     bar widget: active tuning indicator, click = toggle/bypass,
                       scroll = cycle candidates
  [[panel]] tuner      main surface: device picker, tuning list, A/B compare,
                       measurement wizard (probe→capture→analyse→delta→fit→generate→apply)
  [[launcher_provider]] tune   quick switcher: "/tune <query>" lists devices + tunings
Python backend (headless, testable, independent of Noctalia)
  backend/noctalia-audio-tuner   single entrypoint, JSON stdout protocol
  backend/tunerlib/              discovery, measure, fit, export, ab
```

The UI layer contains no DSP logic; the backend contains no UI. Communication: `noctalia.runAsync` / `noctalia.runStream` (string commands via `/bin/sh -c` — shell-quote all interpolated values; argv tables are API ≥ 24, not used in v1). Long-running commands stream NDJSON progress lines (≤ 64 KiB/line cap); the service relays them to `noctalia.state` for widget/panel to `watch`.

### Backend JSON protocol (v1)

- Every command prints exactly one JSON object to stdout: `{"status":"ok"|"error","data":{...},"error":{"code":"...","message":"..."}}`.
- With `--progress` (measurement/fit/compare only): NDJSON lines `{"event":"progress","pct":0-100,"msg":"..."}` first, then a final `{"event":"done","data":{...}}`.
- `doctor` reports each dependency as `{name, ok, hint}`.
- Commands v1: `doctor`, `devices` (sinks+sources with classification + default flags), `probe`, `capture`, `analyse`, `delta`, `tone`, `mic-sweep`, `fit`, `generate`, `tunings`, `apply`, `ab-start`, `ab-switch`, `ab-stop`.

### Export targets (generate)

1. **PipeWire filter-chain** (primary, generalized from `gen-filter-chain.py` — placeholder sink substitution, limiter at the end),
2. **Omarchy tuning tree** when Omarchy is detected (`tunings/<vendor>-<model>/filter-chain.conf` + `tuning.conf` with the four report fields). CAUTION: a fit alone supplies only `magnitude_rms_db` and `bass_group_delay_swing_ms`; `limiter_headroom_db` (needs a hot master + volumedetect) and `dynamic_range_delta_lu` (needs reference LRA) are post-installation measurements. The exporter must emit those two as explicit `not_measured` markers — or refuse to write `tuning.conf` at all — never fabricated values.
3. **EasyEffects preset** (importable),
4. CamillaDSP: out of scope for v1; record as future work.

## Noctalia v5 contract (pinned from docs — do not deviate)

- Plugin = directory with static `plugin.toml` + Luau entry scripts; each entry is an isolated Luau VM off the UI thread with per-call time budgets. **Start every `.luau` with `--!nonstrict`.**
- Manifest: `id` (`awsumatt/noctalia-audio-tuner`), `name`, `version` (strict semver), `plugin_api`, `author`, `license`, `dependencies`, `tags`, `icon`, `description`. v1 declares **`plugin_api = 9`** (matches the reference example: widgets+settings+launcher+panel) and uses only the ≤9 surface: `getConfig`, `runAsync`, `runStream`, `state.*`, `notify*`, `tr/trp`, `json.*`, filesystem sync APIs, `barWidget.*`, `panel.render`, `launcher.*`, `togglePanel`.
- Entry lifecycle that applies to us: `update()` (set `setUpdateInterval`), `onClick/onScroll`, `onOpen/onClose` (panel), `onQuery/onActivate` (launcher), `onIpc(event,payload)`, `onConfigChanged` (service), `onExit(signal, reason)`; `onEnable` is API 17 — not used in v1.
- Launcher: `prefix = "tune"`, `debounce_ms` set (subprocess-backed), `setResults(query, results)` must **echo** the query text; publish a placeholder synchronously then update from the async callback.
- Settings: typed `[[widget.setting]]` / root `[[setting]]`; labels are **translation keys** (`label_key`) → `translations/en.json` is mandatory from day one; only declared keys resolve (`getConfig` warns + nil otherwise).
- Persistence: `noctalia.pluginDataDir()` only — never `pluginDir()` (rewritten on update). Store per-device measurement artifacts + fit results there.
CAVEAT for review: the vendored `references/noctalia.d.luau` is from the upstream tip (level 32) and annotates individual members with `API n` markers above 9 (`getSetting` 26, `readFileAsync` 23, argv-table exec 24, `getColor` 31, …). Before any merge, diff EVERY `noctalia.*`/`ui.*`/`barWidget.*`/`panel.*`/`launcher.*` call the plugin makes against those per-member annotations: anything above 9 must be dropped, or `plugin_api` raised with a written reason. The `plugin_api = 9` claim is a hypothesis until that diff passes.
- Copy `references/noctalia.d.luau` into `plugin/` for luau-lsp diagnostics (type-only, no runtime effect) and keep it **gitignored** in `plugin/` (it is vendored under `references/` instead; `scripts/fetch-noctalia.d.luau.sh` refreshes it).
- Workflow per vendored `references/v5-plugin-knowledge/references/workflow.md`: path source in `[plugins]` or `~/.local/share/noctalia/plugins/<name>/`, enable once, hot-reload on edit.

## Repo layout & ownership

```
AGENTS.md, LICENSE, NOTICE, README.md, .gitignore     (scaffold — do not edit without reason)
references/            vendored v5 knowledge + noctalia.d.luau + example (MIT; cite in NOTICE)
backend/noctalia-audio-tuner + backend/tunerlib/       SA1: discovery+measure / SA2: fit+export / SA4: ab
backend/fixtures/      simulated device profiles (SA1)
plugin/                SA3 only (plugin.toml, entries, lib-free v1, translations/en.json)
docs/                  SA4 (UPSTREAM.md mapping, install guide)
.github/workflows/     SA4 (lint + tests; luau-analyze if available locally, else python+shellcheck)
```

## Milestones & acceptance criteria

- **M0 scaffold (done):** repo created, AGENTS.md committed, references vendored, LICENSE+NOTICE in place.
- **M1 backend measure core (SA1):** `devices` returns JSON classification on this machine; `probe`/`capture`/`analyse`/`delta`/`tone`/`mic-sweep` ported with rate parametrisation, landing-sink verification, JSON protocol, `--help`; ≥ 2 simulated device fixtures exercised in tests; zero regex-on-vendor-name discovery; unit tests green (`python3 -m pytest` or stdlib `unittest`).
- **M2 fit + exporters (SA2):** `fit` parametrized (rate from capture, layout profile data), `generate` emits (a) generalized filter-chain.conf with limiter + `alr/boost = 0` + gain passthrough and (b) Omarchy tree + (c) EasyEffects preset; fit on a synthetic target converges ≤ 1 dB RMS; exporter output for the upstream example fit is byte-comparable (modulo sink name) to `gen-filter-chain.py` output.
- **M3 Noctalia plugin shell (SA3):** `plugin.toml` parses; all four entries present; JSON-RPC client helpers in each entry (no invented APIs — diff against `references/noctalia.d.luau`); `luau-analyze`/`luau` lint clean if tooling exists, else a documented manual check; installs as a path source and renders on this machine (user enables it; report exact steps taken).
- **M4 A/B + switch + safety (SA4):** `ab-start/ab-switch/ab-stop` port level-matching (ebur128 trim math) + stream-moving with landing verification; every `apply` path has `--dry-run` + backup + restore-on-failure; docs + CI.
- **M5 integration (lead):** merge order SA1 → SA2 → SA3 → SA4; end-to-end smoke on this machine; then the user runs the measurement wizard for real.

## Subagent assignments

| Agent | Branch | Slice | Must not touch |
|---|---|---|---|
| SA1 | `feat/backend-measure` | backend entry + discovery + measure commands + fixtures + tests | plugin/, fit+export files |
| SA2 | `feat/fit-export` | fit + exporters + fit/export tests | plugin/, discovery/measure files |
| SA3 | `feat/noctalia-plugin` | plugin/ only, against pinned contract | backend/ |
| SA4 | `feat/ab-docs-ci` | ab commands + docs/ + CI + README | plugin/, measure/fit internals |

## Open items (record, don't guess)

- Upstream repo URL: verify via `git remote -v` before citing in README.
- `dc-ja` knowledge package license unknown → vendored under `references/v5-plugin-knowledge/` as a working copy for development only; if redistribution is wanted, re-check its license first.
- Plugin cannot be auto-enabled headlessly; the user enables it once (Settings → Plugins) — never script around this.
- Measurement requires hardware the CI does not have: CI is lint + simulated-fixture tests only.
