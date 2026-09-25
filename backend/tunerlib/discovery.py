"""PipeWire/PulseAudio discovery — property-based, no vendor-name regexes.

Banned upstream pattern: matching `sof_sdw|HiFi` / `Speaker` inside sink names.
Here everything is classified from PipeWire properties:

- hardware device: properties carry `api.alsa.pcm.card` / `api.alsa.path`
- monitor: properties carry `device.class = "monitor"`
- virtual (null/filter/effect) sink: hardware-ish properties absent
- internal speaker candidate: a hardware sink with a port whose PipeWire
  `type` is "Speaker" and availability is "yes"

Sample rate is read from the sink's actual negotiated rate (sample spec, with a
pw-dump fallback) — never hardcoded.
"""

import json
import os
import shutil
import subprocess

from . import TunerError


def run_tool(argv, code="tool_failed", context=""):
    """Run a tool, raising TunerError if missing or failing."""
    exe = shutil.which(argv[0])
    if exe is None:
        raise TunerError("dependency", f"{argv[0]} not found in PATH")
    try:
        proc = subprocess.run([exe] + argv[1:], capture_output=True,
                              text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise TunerError(code, f"{argv[0]} timed out ({context})") from e
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise TunerError(code, f"{argv[0]} failed ({context}): "
                               f"{detail[-1] if detail else proc.returncode}")
    return proc.stdout


def list_sinks():
    return json.loads(run_tool(["pactl", "-f", "json", "list", "sinks"],
                               context="listing sinks"))


def list_sources():
    return json.loads(run_tool(["pactl", "-f", "json", "list", "sources"],
                               context="listing sources"))


def list_sink_inputs():
    return json.loads(run_tool(["pactl", "-f", "json", "list", "sink-inputs"],
                               context="listing sink inputs"))


def defaults():
    return {"sink": run_tool(["pactl", "get-default-sink"],
                             context="default sink").strip(),
            "source": run_tool(["pactl", "get-default-source"],
                               context="default source").strip()}


def parse_sample_spec(spec):
    """'float32le 2ch 48000Hz' -> (rate, channels) or (None, None)."""
    if not spec:
        return None, None
    rate = ch = None
    for tok in str(spec).split():
        if tok.endswith("Hz") and tok[:-2].isdigit():
            rate = int(tok[:-2])
        if tok.endswith("ch") and tok[:-2].isdigit():
            ch = int(tok[:-2])
    return rate, ch


def _norm_avail(value):
    """pactl emits 'yes' / 'no' / 'availability unknown'."""
    v = (value or "").strip()
    if v == "yes":
        return "yes"
    if v in ("no", "availability unknown", "unknown", ""):
        return "unknown"
    return "unknown"


def classify_port(port):
    return {"name": port.get("name", ""),
            "description": port.get("description", ""),
            "type": port.get("type", ""),
            "availability": _norm_avail(port.get("availability"))}


def classify_sink(sink, default_sink):
    props = sink.get("properties", {}) or {}
    ports = [classify_port(p) for p in sink.get("ports", []) or []]
    is_hw = bool(props.get("api.alsa.pcm.card") or props.get("api.alsa.path"))
    rate, channels = parse_sample_spec(sink.get("sample_specification"))
    speaker_port = next((p["name"] for p in ports
                         if p["type"] == "Speaker" and p["availability"] == "yes"),
                        None)
    return {
        "name": sink.get("name", ""),
        "index": sink.get("index"),
        "description": sink.get("description", ""),
        "kind": "hardware" if is_hw else "virtual",
        "rate": rate,
        "channels": channels,
        "monitor_source": sink.get("monitor_source", ""),
        "default": sink.get("name") == default_sink,
        "speaker_candidate": is_hw and speaker_port is not None,
        "ports": ports,
    }


def classify_source(src, default_source):
    props = src.get("properties", {}) or {}
    ports = [classify_port(p) for p in src.get("ports", []) or []]
    if props.get("device.class") == "monitor" or src.get("monitor_of_sink") is not None:
        kind = "monitor"
    elif props.get("api.alsa.pcm.card") or props.get("api.alsa.path"):
        kind = "hardware"
    else:
        kind = "virtual"
    return {
        "name": src.get("name", ""),
        "index": src.get("index"),
        "description": src.get("description", ""),
        "kind": kind,
        "monitor_of": src.get("monitor_of_sink"),
        "default": src.get("name") == default_source,
        "ports": ports,
    }


def devices():
    dfl = defaults()
    sinks = [classify_sink(s, dfl["sink"]) for s in list_sinks()]
    sources = [classify_source(s, dfl["source"]) for s in list_sources()]
    speaker = next((s["name"] for s in sinks if s["speaker_candidate"]), None)
    return {"sinks": sinks, "sources": sources,
            "default_sink": dfl["sink"], "default_source": dfl["source"],
            "speaker_sink": speaker}


def sink_index(sink_name):
    """Numeric sink id of a sink (used for landing verification)."""
    for s in list_sinks():
        if s.get("name") == sink_name:
            idx = s.get("index")
            if idx is None:
                raise TunerError("no_sink_index",
                                 f"sink '{sink_name}' has no index")
            return int(idx)
    raise TunerError("no_sink", f"no such sink: '{sink_name}'")


def sink_rate(sink_name):
    """Actual rate of a sink: sample spec, else pw-dump node property."""
    for s in list_sinks():
        if s.get("name") != sink_name:
            continue
        rate, _ = parse_sample_spec(s.get("sample_specification"))
        if rate:
            return rate
        rate = _pw_dump_rate(sink_name)
        if rate:
            return rate
        raise TunerError("no_rate",
                         f"could not determine the sample rate of sink "
                         f"'{sink_name}'")
    raise TunerError("no_sink", f"no such sink: '{sink_name}'")


def _pw_dump_rate(node_name):
    """Best-effort pw-dump lookup of the node's audio.rate property."""
    exe = shutil.which("pw-dump")
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    try:
        dump = json.loads(proc.stdout)
    except ValueError:
        return None
    for entry in dump:
        info = entry.get("info") or {}
        props = info.get("props") or {}
        if props.get("node.name") == node_name:
            try:
                return int(props.get("audio.rate", 0)) or None
            except (TypeError, ValueError):
                return None
    return None


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return os.path.join(base, "noctalia-audio-tuner")
