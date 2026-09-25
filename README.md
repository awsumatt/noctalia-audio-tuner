# noctalia-audio-tuner

A [Noctalia Shell v5](https://docs.noctalia.dev/noctalia/) plugin that measures a
laptop's speakers and produces PipeWire tunings — audio-tuner toolkit (https://github.com/omacom/omarchy-audio-tuner). Work in progress; the implementation plan lives in
[AGENTS.md](AGENTS.md).

- **Plugin** (`plugin/`): bar widget, panel (device picker, measurement wizard,
  A/B compare), launcher quick-switcher — Luau, per the v5 plugin contract.
- **Backend** (`backend/`): headless `noctalia-audio-tuner` Python CLI with a
  JSON protocol; all DSP/measurement logic lives here.

MIT licensed (see LICENSE and NOTICE for upstream attribution).
