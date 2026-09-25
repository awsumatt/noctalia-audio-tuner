# Noctalia Audio Tuner — Noctalia Shell v5 plugin

UI shell (SA3) for the `noctalia-audio-tuner` backend: a bar widget, a tuner
panel, a `/tune` launcher provider, a control-center shortcut tile, and a
headless service that runs the Python backend and shares state between the
entries. The shell contains no DSP logic; every measurement decision lives in
the backend (`backend/tunerlib/`, contract in `backend/tunerlib/README.md`).

## Entries

| Manifest entry | File | Role |
|---|---|---|
| `[[service]] core` | `core.luau` | Runs the backend (`/bin/sh -c` strings, JSON-only stdout, `noctalia.runAsync`/`runStream`), publishes shared state, owns the measurement wizard. |
| `[[widget]] audio` | `audio.luau` | Bar widget: glyph + active tuning, click = toggle/bypass, scroll = cycle candidates. |
| `[[panel]] tuner` | `tuner.luau` | Device picker, tunings, A/B controls, measurement wizard + dry-run confirm. |
| `[[launcher_provider]] tune` | `tune.luau` | Quick switcher (`/tune`): devices + tunings, fuzzy-ranked, drill-in via `launcher.setQuery`. |
| `[[shortcut]] toggle` | `shortcut.luau` | Control-center quick-toggle tile. |

## Backend contract implemented

- Command names are the registry names (`_` -> `-`): `doctor`, `devices`,
  `probe`, `capture`, `analyse`, `delta`, `tone`, `mic-sweep` (SA1), plus
  `fit`, `generate`, `tunings`, `apply`, `ab-start`, `ab-switch`, `ab-stop`
  (SA2/SA4).
- The global `--progress` flag goes BEFORE the subcommand.
- One-shot commands print one envelope `{"status","data","error"}`; progress
  commands stream NDJSON `{"event":"progress",...}` lines ending in
  `{"event":"done","data":...}`. On error the single error envelope prints and
  the process exits 1 — the stream reader in `core.luau` handles both shapes.
- Stdout is parsed ONLY with `noctalia.json.decode` — never human text.
- Data shapes consumed (see `backend/tunerlib/README.md`): `devices` ->
  `sinks/sources/default_sink/default_source/speaker_sink`; `doctor` ->
  `deps[{name,ok,hint,required}]/ok/missing`; `probe` ->
  `wav/freqs_path/rate/seconds/peak_db/seed/n_tones/freqs`; `analyse` ->
  `levels[{freq,dbfs}]` + `out` when `--out` is given; `delta` ->
  `delta[{freq,db}]`; `capture` -> `out/sink/sink_index/source/landed_sink_index/rate`.
- Every subprocess is a shell string; every interpolated value (backend path,
  sink names, file paths, labels) is single-quote shell-escaped.
- `speaker_sink` is nullable (machines without internal speakers). The panel
  warns and asks the user to pick a sink explicitly; there is NO silent
  fallback to a USB sink.

## Measurement wizard

`probe` -> `capture` (reference) -> `analyse --out ref.txt` → *user changes
configuration* → `capture` (candidate) -> `analyse --out raw.txt` ->
`delta raw.txt ref.txt` -> `fit` -> `generate` -> dry-run `apply` ->
confirmed `apply`.

`fit` / `generate` / `tunings` / `ab-*` / `apply` come from sibling slices.
When the installed backend does not implement them yet (argparse prints usage
to stderr and nothing to stdout), the service reports a clear "not yet
available" state (`unavailable` state key + notification) instead of failing.

## Enabling the plugin (user steps)

1. Install the backend (or note its path):
   - `backend/noctalia-audio-tuner` from this repo — put it on `$PATH`
     (e.g. `ln -s .../backend/noctalia-audio-tuner ~/.local/bin/`), or
   - set the plugin setting `backend_path` to the absolute path.
2. Register the plugin source with Noctalia, either:
   - add a path source to the noctalia config:
     ```yaml
     plugins:
       source:
         - kind: "path"
           path: "/path/to/noctalia-audio-tuner/plugin"
     ```
     (check the current `[plugins.source]` schema in
     <https://docs.noctalia.dev/noctalia/plugins/workflow/>), or
   - copy/symlink the `plugin/` directory to
     `~/.local/share/noctalia/plugins/noctalia-audio-tuner/`.
3. Open Noctalia **Settings -> Plugins** and enable **Noctalia Audio Tuner**.
   (Enabling cannot be done headlessly; the user must do it once in the GUI.)
4. Edits to the plugin files hot-reload — no re-enable needed.

`plugin/noctalia.d.luau` is a gitignored, type-only copy of
`references/noctalia.d.luau` (refresh with `bash scripts/fetch-noctalia.d.luau.sh`)
for luau-lsp users; it has no runtime effect. NOTE: no luau/luau-analyze
tooling exists on this machine, so linting was a documented manual check.

## plugin_api = 9 — audit evidence

Every `noctalia.*` / `ui.*` / `barWidget.*` / `panel.*` / `launcher.*` /
`shortcut.*` call was diffed against the per-member `API n` annotations in
`references/noctalia.d.luau` (tip level 32). Result: **every call the plugin
makes is on the base (≤ 9) surface** — none of the > 9 members
(`getSetting` 26, `readFileAsync` 23, argv-table exec 24, `getColor` 31,
`require` modules 22, panel `tooltip` props 32, …) are used. Calls used:
`noctalia.{log,runAsync,runStream,tr,notify,notifyError,getConfig,expandPath,fileExists,mkdirAll,pluginDataDir,setUpdateInterval,fuzzyScore,state.{get,set,watch},json.{decode,encode},string.trim}`,
`ui.{column,row,scroll,button,label,select,spacer,progress,separator}`,
`barWidget.{setText,setGlyph,setTooltip,setColor}`,
`panel.{render,close}`, `launcher.{setResults,setQuery}`,
`shortcut.{setLabel,setIcon,setActive,setEnabled}`. Lifecycle callbacks used
are all base-level: `update`, `onClick`, `onScroll`, `onOpen`/`onClose`,
`onQuery`/`onActivate`, `onIpc`, `onConfigChanged`, `onExit`.
`plugin_api = 9` is therefore safe as declared. Every `.luau` starts with
`--!nonstrict` and there are no `require()` modules.

## Known gaps (not verifiable without the GUI / sibling backends)

- Rendering, widget click/scroll and launcher UX need the user's Noctalia
  session; nothing here was visually verified.
- `capture` / `mic-sweep` / `apply` / `ab-*` were never run live (mutating);
  only `doctor`, `devices`, `probe` were exercised against the real backend.
- `fit` / `generate` argument shapes are best-effort (SA2 defines them); the
  plugin degrades to "not yet available" until they land and may need a
  one-line args update when their contract is documented.
- `runStream` has no completion callback at API 9; a stream that dies without
  printing a done line or an error envelope would leave `busy` set until the
  next command replaces it — a host-side limitation, noted here honestly.
