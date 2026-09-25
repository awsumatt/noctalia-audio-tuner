# tunerlib — backend measure core (SA1)

Headless backend for the Noctalia audio tuner: discovery + measurement.
Fit/export and A/B commands live in the same registry on their own branches.

## Protocol (binding for all commands)

- One JSON envelope on **stdout**, and nothing else:
  `{"status":"ok","data":{...},"error":null}` or
  `{"status":"error","data":null,"error":{"code":"...","message":"..."}}`.
- Human text ALWAYS to **stderr**.
- Errors are raised as `tunerlib.TunerError(code, message)`.
- With the global `--progress` flag, long-running commands stream NDJSON on
  stdout instead: `{"event":"progress","pct":0-100,"msg":"..."}` lines, then a
  final `{"event":"done","data":{...}}`. On error the single error envelope is
  printed and the process exits 1.

## Registry loading rule (follow EXACTLY)

The entrypoint globs the package: every module in `backend/tunerlib/commands/`
is imported via `pkgutil.iter_modules(tunerlib.commands.__path__)` and
registered as a subcommand whose name is the module name with `_` -> `-`.
Modules whose name starts with `_` are skipped (private helpers such as
`_common.py`), as are modules without a `register` function.
Each module must expose:

- `register(sub)` — receive the argparse subparser, add arguments, then
  `parser.set_defaults(func=run)`.
- `run(args) -> dict` — the payload placed under `"data"` in the envelope.

Nothing in a command module may print to stdout. Adding a command = adding a
module; no entrypoint edit needed.

## Entry point

`backend/noctalia-audio-tuner` (executable, python3). Global flags come BEFORE the subcommand:
`noctalia-audio-tuner --progress probe ...`. Common error codes: `dependency`, `no_sink`, `no_rate`,
`no_freqs`, `bad_wav`, `short_wav`, `not_bin_aligned`, `no_overlap`,
`landing_mismatch`, `no_speaker_sink`, `no_monitor`, `record_failed`,
`bad_freq`, `no_response_file`.

## Invariants (regressions are bugs)

- Probes are dense (1/12-octave), pink-weighted, **integer-Hz tones analysed
  over exactly one second** of the wav's own sample rate so every tone lands
  on a DFT bin — leakage-free, no windowing. Sample rate is a parameter
  (detected from the sink), never hardcoded 48000.
- Verify where a stream **landed** by sink id (numeric index), never trust
  exit codes — mpv silently falls back to the default sink.
- Artifacts live under `$XDG_CACHE_HOME/noctalia-audio-tuner` (default
  `~/.cache/noctalia-audio-tuner`); the tone list is written next to the probe.
- Probe peak default -6 dBFS, exposed as a flag.

## Commands (SA1) — names, arguments, exact response shapes

Every response below is the `data` object of the `ok` envelope. Flag names are
exactly as parsed by argparse.

### `doctor`
No arguments.
```json
{"deps": [{"name": "python3", "ok": true, "hint": "...", "required": true},
          {"name": "ffmpeg",  "ok": true, "hint": "...", "required": true},
          {"name": "mpv",     "ok": true, "hint": "...", "required": true},
          {"name": "pactl or wpctl", "ok": true, "hint": "...", "required": true},
          {"name": "lsp-plugins-lv2", "ok": false, "hint": "...", "required": false}],
 "ok": true, "missing": []}
```
`ok` = all `required` deps present; `missing` lists required-but-absent names.

### `devices`
No arguments. Read-only.
```json
{"sinks": [{"name": "alsa_output...", "index": 41, "description": "...",
            "kind": "hardware|virtual", "rate": 48000, "channels": 2,
            "monitor_source": "alsa_output....monitor",
            "default": true, "speaker_candidate": true,
            "ports": [{"name": "...", "description": "...",
                       "type": "Speaker", "availability": "yes|unknown"}]}],
 "sources": [{"name": "...", "index": 7, "description": "...",
              "kind": "hardware|monitor|virtual", "monitor_of": "sink-name"|null,
              "default": true, "ports": []}],
 "default_sink": "...", "default_source": "...", "speaker_sink": "name"|null}
```
Classification is property-based (`media.class`, `api.alsa.pcm.card/path`,
port `type: Speaker` + availability); vendor-name regexes are banned.

### `probe`
`probe [--rate INT] [--seconds INT=6] [--peak-db FLOAT=-6.0] [--seed INT=7]
       [out]` — `out` is a directory (creates `dense.wav` inside) or a `.wav`
path; default `$XDG_CACHE_HOME/noctalia-audio-tuner/dense.wav`. `--rate`
default: the default sink's actual negotiated rate (pactl, pw-dump fallback).
Writes the tone list NEXT TO the wav as `<stem>-freqs.txt`.
```json
{"wav": "/abs/dense.wav", "freqs_path": "/abs/dense-freqs.txt",
 "rate": 48000, "seconds": 6, "peak_db": -6.0, "seed": 7,
 "n_tones": 104, "freqs": [40, 42, ...]}
```

### `capture`
`capture [--from SOURCE|monitor] [--seconds INT=2] [--rate INT]
         [--probe WAV] [--peak-db FLOAT=-6.0] SINK OUT.wav`
`--from monitor` (default) taps the internal-speaker sink's monitor; refuses
(`no_speaker_sink`) if none is detected. `--rate` default: SINK's actual rate.
Generates the probe (at SINK's rate) if `--probe` missing/absent. Verifies the
mpv sink-input landed on SINK's id before recording; raises
`landing_mismatch` otherwise.
```json
{"out": "/abs/out.wav", "sink": "alsa_output...", "sink_index": 41,
 "source": "alsa_output....monitor", "landed_sink_index": 41,
 "rate": 48000, "seconds": 2, "probe": "/abs/dense.wav"}
```
Ported but NOT live-tested (needs mutable audio state).

### `analyse`
`analyse [--freqs FILE] [--start FLOAT=0.3] [--out FILE] WAV` — one-second
window of the wav's own rate, starting `--start` seconds in (falls back to the
wav head if too short). `--freqs` default: cache `dense-freqs.txt`. With
`--out FILE`, also writes the response in upstream text form
(`<freq> <dbfs>` per line — exactly what `delta` reads) and returns its
absolute path under `"out"`.
```json
{"wav": "/abs/capture.wav", "rate": 48000, "start": 0.3,
 "freqs_path": "/abs/dense-freqs.txt",
 "levels": [{"freq": 40, "dbfs": -23.1}, {"freq": 42, "dbfs": -22.9}, ...]}
```
Also available in text form (`<freq> <dbfs>` lines) via
`tunerlib.analysis.response_text(levels)`.

### `delta`
`delta RAW.txt REFERENCE.txt [--out FILE]` — both files are `analyse`-style
response text (`<freq> <dbfs>` per line). Output = reference - raw per shared
frequency. With `--out FILE`, also writes the target curve as `<freq> <db>`
text (the format `fit` consumes) and returns its path under `"out"`.
```json
{"raw": "/abs/raw.txt", "reference": "/abs/ref.txt",
 "delta": [{"freq": 40, "db": 2.14}, ...], "n": 104,
 "out": "/abs/target.txt"|null}
```

### `tone`
`tone WAV FREQ [--harmonics INT=6]` — fundamental + harmonics + THD; window is
exactly one second of the wav's rate (integer-Hz FREQ keeps harmonics
bin-aligned).
```json
{"freq": 200.0, "fs": 48000, "fundamental_dbfs": -12.1,
 "harmonics_dbfs": [-38.2, -45.0, ...], "thd_pct": 12.34}
```

### `mic-sweep`
`mic-sweep [--mic SOURCE] [--out-dir DIR] [--rate INT]
           [--levels PCT [PCT ...]] [--harmonics INT=6]
           SINK TONE.wav FREQ LABEL`
Switches the default sink, verifies landing by sink id, records 1.5 s mono per
level, restores the previous default sink + speaker volume on exit. Captures
written to `DIR/mic-LABEL-FREQ-PCT.wav` (default DIR: cache dir/`spectral`).
```json
{"sink": "...", "sink_index": 41, "speaker_sink": "...", "mic": "...",
 "freq": 200.0, "label": "sweep", "landed_sink_index": 41,
 "rows": [{"pct": 40, "wav": "/abs/mic-sweep-200-40.wav",
           "fundamental_dbfs": -18.2, "harmonics_dbfs": [-40.1, ...],
           "thd_pct": 9.87}, ...]}
```
Ported but NOT live-tested (needs mutable audio state).

## Commands (other slices — same registry, do not collide)

`fit`, `generate`, `tunings`, `apply` (SA2); `ab-start`, `ab-switch`,
`ab-stop` (SA4). They register through the identical `register(sub)` /
`run(args)` contract and appear as subcommands automatically.

## Fixtures & tests

- `backend/fixtures/devices/` — simulated device profiles (pactl-shaped JSON)
  exercised against `discovery.classify_sink/classify_source`.
- `backend/fixtures/audio/` — synthetic wavs with known tone amplitudes
  (mono 16-bit, manifest in `manifest.json`), generated with
  `tunerlib.multitone.make_mix`; tests verify analyse recovers them.
- `backend/tests/` — stdlib `unittest` only (`python3 -m unittest discover
  -s backend/tests -t backend`, from `backend/`); no pytest, no third-party deps.
