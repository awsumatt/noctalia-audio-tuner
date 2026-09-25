# INSTALL.md — backend installation

Covers the headless Python backend (`backend/noctalia-audio-tuner`). The
Noctalia plugin shell is installed separately (see below).

## Dependencies

### Arch / CachyOS

```sh
sudo pacman -S --needed python ffmpeg mpv pipewire-pulse
# optional — only needed for LSP-based filter-chain tunings:
sudo pacman -S --needed lsp-plugins-lv2
```

- `python` ≥ 3.10 (only stdlib is used — no pip, no venv required)
- `ffmpeg` — probing, ebur128 loudness measurement
- `mpv` — playing probe tones / reference audio
- `pipewire` + `pipewire-pulse` — `pactl`/`wpctl` come with pipewire-pulse
- `lsp-plugins-lv2` (optional): the Linux Studio Plugins chain used by
  generated tunings

### Generic distros

Same five pieces under their usual names (`python3`, `ffmpeg`, `mpv`,
`pipewire-pulse` or equivalent providing `pactl`, `lsp-plugins-lv2` from your
package manager or an LV2 bundle). The backend calls `pactl`; anything
providing a PulseAudio-compatible `pactl` works.

**`doctor` reports missing dependencies for you**:

```sh
noctalia-audio-tuner doctor
```

Each dependency comes back as `{name, ok, hint, required}`; missing required
ones are listed under `missing`.

## Install the backend

Make the single-file entrypoint available on your PATH:

```sh
ln -s "$PWD/backend/noctalia-audio-tuner" ~/.local/bin/noctalia-audio-tuner
```

(ensure `~/.local/bin` is on PATH). The entrypoint finds its `tunerlib`
package relative to the symlink target, so no copy is needed. Check:

```sh
noctalia-audio-tuner doctor          # dependency report (JSON)
noctalia-audio-tuner devices         # classified sinks/sources (JSON)
```

## Install / enable the Noctalia plugin

The plugin ships separately from the backend.

1. **Path source** — either:
   - point Noctalia at the checkout by adding the path under `[plugins]` /
     the plugins path list in your noctalia config, **or**
   - link it into the per-user plugin dir:

     ```sh
     mkdir -p ~/.local/share/noctalia/plugins
     ln -s "$PWD/plugin" ~/.local/share/noctalia/plugins/noctalia-audio-tuner
     ```
2. **Enable it** in Noctalia: **Settings → Plugins → noctalia-audio-tuner →
   enable**. This step is one-time and manual; it cannot be scripted — the
   plugin hot-reloads on edit afterwards.

## State and cache locations

- `$XDG_CACHE_HOME/noctalia-audio-tuner` (default `~/.cache/...`): probes,
  captures, tone lists
- `$XDG_STATE_HOME/noctalia-audio-tuner` (default `~/.local/state/...`): A/B
  session state, per-track loudness offsets
- `~/.config/pipewire/pipewire.conf.d/`: installed tuning filter-chains
  (`apply` writes here; every real install keeps a timestamped backup)

## Honest status

Work in progress. `apply` defaults to dry-run, does **not** restart PipeWire
(run `systemctl --user restart pipewire` after a real install), and the A/B
commands have not been exercised on live hardware in CI — see
[UPSTREAM.md](UPSTREAM.md#untested-live-honest) for what is mocked-only.
