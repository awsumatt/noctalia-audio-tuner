"""Fit a cascaded biquad chain to a target curve (port of upstream fit-eq.py).

Generalizations over upstream (fit/fit-eq.py):

- Sample rate is a PARAMETER (default 48000), never a module constant.
- The 14-section-prior layout lives in ``tunerlib/layouts/*.json`` (the default
  matches the upstream LAYOUT verbatim) and is selectable by name.
- The result is returned BOTH as the upstream human-readable text (so it
  round-trips through the generator exactly like upstream ``fit.txt``) and as a
  machine-readable dict.
- The result carries ``bass_group_delay_swing_ms`` (max - min group delay of
  the fitted chain over 30-300 Hz, from the biquad coefficients), one of the
  four fields a tuning must report.

The DSP is faithful to upstream: cascaded biquads multiply in magnitude, so
their dB contributions add; each section's dB curve is cached and only the
changed section is recomputed, which makes coordinate descent over a
13-section chain tractable in pure Python. Multi-seed restarts preserved.
"""
import cmath
import json
import math
import os
import random

from . import TunerError

DEFAULT_RATE = 48000
LAYOUTS_DIR = os.path.join(os.path.dirname(__file__), "layouts")
# Bass group delay is audited over 30-300 Hz (upstream "what a tuning must
# report"); 1 Hz phase grid, group delay from central differences.
GD_FMIN, GD_FMAX, GD_STEP = 30.0, 300.0, 1.0


def load_layout(name, layouts_dir=LAYOUTS_DIR):
    """Load a layout profile JSON by name (file stem)."""
    path = os.path.join(layouts_dir, f"{name}.json")
    if not os.path.exists(path):
        available = sorted(
            f[:-5] for f in os.listdir(layouts_dir) if f.endswith(".json"))
        raise TunerError(
            "no_layout",
            f"no layout profile '{name}' in {layouts_dir} "
            f"(available: {', '.join(available)})")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise TunerError("bad_layout",
                         f"cannot read layout {path}: {e}") from e
    sections = data.get("sections")
    if not sections:
        raise TunerError("bad_layout", f"layout {path} has no sections")
    layout = []
    for s in sections:
        try:
            layout.append((s["kind"], tuple(s["freq"]), tuple(s["q"]),
                           tuple(s["gain_db"])))
        except (KeyError, TypeError) as e:
            raise TunerError("bad_layout",
                             f"layout {path}: malformed section {s}: {e}") from e
    return data.get("name", name), layout


def biquad(kind, f0, q, gain_db, fs):
    """Return (b, a) coefficients in the z^-1 -> z^-2 (RBJ) form upstream uses."""
    w0 = 2 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "peaking":
        a = 10 ** (gain_db / 40)
        return ([1 + alpha * a, -2 * cw, 1 - alpha * a],
                [1 + alpha / a, -2 * cw, 1 - alpha / a])
    if kind == "highpass":
        return ([(1 + cw) / 2, -(1 + cw), (1 + cw) / 2],
                [1 + alpha, -2 * cw, 1 - alpha])
    a = 10 ** (gain_db / 40)
    beta = 2 * math.sqrt(a) * alpha
    if kind == "lowshelf":
        return ([a * ((a + 1) - (a - 1) * cw + beta),
                 2 * a * ((a - 1) - (a + 1) * cw),
                 a * ((a + 1) - (a - 1) * cw - beta)],
                [(a + 1) + (a - 1) * cw + beta,
                 -2 * ((a - 1) + (a + 1) * cw),
                 (a + 1) + (a - 1) * cw - beta])
    return ([a * ((a + 1) + (a - 1) * cw + beta),
             -2 * a * ((a - 1) + (a + 1) * cw),
             a * ((a + 1) + (a - 1) * cw - beta)],
            [(a + 1) - (a - 1) * cw + beta,
             2 * ((a - 1) - (a + 1) * cw),
             (a + 1) - (a - 1) * cw - beta])


def section_db(kind, f0, q, g, zs, fs):
    b, a = biquad(kind, f0, q, g, fs)
    out = []
    for z in zs:
        h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
        out.append(20 * math.log10(abs(h)))
    return out


def chain_response_db(secs, zs, fs):
    """dB magnitude of the whole cascade (no global gain) at each z."""
    cache = [section_db(s[0], s[1], s[2], s[3], zs, fs) for s in secs]
    return [sum(c[i] for c in cache) for i in range(len(zs))]


def weight(f):
    if f < 55:
        return 0.5
    if f > 12000:
        return 0.6
    return 1.0


def _fit_one(freqs, target, zs, ws, layout, fs, seed):
    rng = random.Random(seed)
    secs, cache = [], []
    for kind, fr, qr, gr in layout:
        s = [kind,
             math.exp(rng.uniform(math.log(fr[0]), math.log(fr[1]))),
             rng.uniform(*qr),
             0.0 if kind == "highpass" else rng.uniform(*gr)]
        secs.append(s)
        cache.append(section_db(s[0], s[1], s[2], s[3], zs, fs))
    gain = rng.uniform(-8, 8)

    def total_err():
        num = den = 0.0
        for i, f in enumerate(freqs):
            got = gain + sum(c[i] for c in cache)
            num += ws[i] * (got - target[i]) ** 2
            den += ws[i]
        return num / den

    best = total_err()
    step = {"f": 0.3, "q": 0.5, "g": 3.0, "gain": 3.0}
    for _ in range(300):
        improved = False
        for i, (kind, fr, qr, gr) in enumerate(layout):
            for key, lo, hi in (("f", fr[0], fr[1]), ("q", qr[0], qr[1]),
                                ("g", gr[0], gr[1])):
                if key == "g" and kind == "highpass":
                    continue
                idx = {"f": 1, "q": 2, "g": 3}[key]
                for d in (1, -1):
                    old = secs[i][idx]
                    new = old * math.exp(d * step["f"]) if key == "f" \
                        else old + d * step[key]
                    new = min(max(new, lo), hi)
                    if new == old:
                        continue
                    oldc = cache[i]
                    secs[i][idx] = new
                    cache[i] = section_db(secs[i][0], secs[i][1], secs[i][2],
                                          secs[i][3], zs, fs)
                    e = total_err()
                    if e < best - 1e-9:
                        best, improved = e, True
                        break
                    secs[i][idx] = old
                    cache[i] = oldc
        for d in (1, -1):
            old = gain
            gain = old + d * step["gain"]
            e = total_err()
            if e < best - 1e-9:
                best, improved = e, True
                break
            gain = old
        if not improved:
            for k in step:
                step[k] *= 0.62
            if step["f"] < 5e-5:
                break
    return best, secs, gain


def fit_curve(freqs, target, fs, layout, restarts=12, seed0=0):
    """Multi-seed coordinate-descent fit. Returns the best (err, secs, gain).

    ``err`` is the weighted mean-squared dB error; ``secs`` are
    [kind, freq, q, gain_db] rows; ``gain`` the global gain in dB.
    """
    zs = [cmath.exp(-2j * math.pi * f / fs) for f in freqs]
    ws = [weight(f) for f in freqs]
    best = None
    for seed in range(seed0, seed0 + restarts):
        r = _fit_one(freqs, target, zs, ws, layout, fs, seed)
        if best is None or r[0] < best[0]:
            best = r
    return best


def bass_group_delay_swing_ms(secs, fs, fmin=GD_FMIN, fmax=GD_FMAX,
                              step=GD_STEP):
    """max - min group delay (ms) of the chain over [fmin, fmax].

    Group delay tau(f) = -dphi/domega from numerical differences of the
    unwrapped cascade phase on a fixed frequency grid.
    """
    freqs = []
    f = fmin
    while f <= fmax + 1e-9:
        freqs.append(f)
        f += step
    phases = []
    for fr in freqs:
        z = cmath.exp(-2j * math.pi * fr / fs)
        h = 1.0 + 0.0j
        for kind, f0, q, g in secs:
            b, a = biquad(kind, f0, q, g, fs)
            h *= ((b[0] + b[1] * z + b[2] * z * z)
                  / (a[0] + a[1] * z + a[2] * z * z))
        phases.append(cmath.phase(h))
    unwrapped = [phases[0]]
    for i in range(1, len(phases)):
        dphi = phases[i] - phases[i - 1]
        while dphi > math.pi:
            dphi -= 2 * math.pi
        while dphi < -math.pi:
            dphi += 2 * math.pi
        unwrapped.append(unwrapped[-1] + dphi)
    taus = []
    for i in range(1, len(freqs)):
        dw = 2 * math.pi * (freqs[i] - freqs[i - 1])
        taus.append(-(unwrapped[i] - unwrapped[i - 1]) / dw)
    return (max(taus) - min(taus)) * 1000.0


def format_fit_text(wrms_db, gain_db, secs, freqs, target, zs, fs):
    """Upstream fit-eq.py output format (round-trips through the generator)."""
    lines = [f"# weighted RMS error: {wrms_db:.2f} dB",
             f"# global gain: {gain_db:+.3f} dB  (linear {10 ** (gain_db / 20):.4f})"]
    for kind, f0, q, g in secs:
        if kind == "highpass":
            lines.append(f"{kind:10s} Freq={f0:8.1f} Q={q:.3f}")
        else:
            lines.append(f"{kind:10s} Freq={f0:8.1f} Q={q:.3f} Gain={g:+.2f}")
    cache = [section_db(s[0], s[1], s[2], s[3], zs, fs) for s in secs]
    lines.append("")
    lines.append("# freq  target    fit    err")
    for i, f in enumerate(freqs):
        if i % 6:
            continue
        got = gain_db + sum(c[i] for c in cache)
        lines.append(f"{f:7.0f} {target[i]:+7.1f} {got:+7.1f} {got - target[i]:+6.1f}")
    return "\n".join(lines)


def fit_to_payload(target_path, fs=DEFAULT_RATE, layout_name="default",
                   restarts=12):
    """Full fit run: read a target file, fit, and build the result payload.

    Target file: '<freq> <db>' lines (delta output is compatible).
    """
    if not os.path.exists(target_path):
        raise TunerError("no_response_file", f"no such target file: {target_path}")
    try:
        with open(target_path) as fh:
            rows = [l.split() for l in fh if l.strip()]
        freqs = [float(r[0]) for r in rows]
        target = [float(r[1]) for r in rows]
    except (OSError, ValueError, IndexError) as e:
        raise TunerError("bad_freq",
                         f"cannot parse target {target_path}: {e}") from e
    if not freqs:
        raise TunerError("no_freqs", f"target file {target_path} is empty")

    _name, layout = load_layout(layout_name)
    err, secs, gain = fit_curve(freqs, target, fs, layout, restarts)
    zs = [cmath.exp(-2j * math.pi * f / fs) for f in freqs]
    wrms = math.sqrt(err)
    text = format_fit_text(wrms, gain, secs, freqs, target, zs, fs)
    gd = bass_group_delay_swing_ms(secs, fs)
    return {
        "target": os.path.abspath(target_path),
        "rate": fs,
        "layout": layout_name,
        "restarts": restarts,
        "n_points": len(freqs),
        "weighted_rms_error_db": wrms,
        "magnitude_rms_db": wrms,
        "bass_group_delay_swing_ms": gd,
        "global_gain_db": gain,
        "linear_gain": 10 ** (gain / 20),
        "sections": [{"kind": s[0], "freq": s[1], "q": s[2], "gain_db": s[3]}
                     for s in secs],
        "fit_text": text,
    }


def parse_fit_text(text):
    """Parse an upstream fit.txt (LABELS form) into (sections, linear_gain).

    Sections are (label, freq, q, gain_or_None) with the generator's bq_* labels.
    Raises ``bad_fit`` if no section line is found.
    """
    import re
    labels = {"highpass": "bq_highpass", "lowpass": "bq_lowpass",
              "peaking": "bq_peaking", "lowshelf": "bq_lowshelf",
              "highshelf": "bq_highshelf"}
    sections, gain = [], 1.0
    for line in text.splitlines():
        m = re.match(r"#\s*global gain:.*\(linear\s+([\d.]+)\)", line)
        if m:
            gain = float(m.group(1))
            continue
        m = re.match(
            r"(\w+)\s+Freq=\s*([\d.]+)\s+Q=([\d.]+)(?:\s+Gain=([-+][\d.]+))?",
            line)
        if m and m.group(1) in labels:
            kind, freq, q, g = m.groups()
            sections.append(
                (labels[kind], float(freq), float(q), float(g) if g else None))
    if not sections:
        raise TunerError("bad_fit", "no biquad sections found in fit input")
    return sections, gain


def load_fit(src):
    """Accept a fit result dict payload OR upstream fit.txt text.

    Returns (sections, linear_gain, payload_or_None) where sections are
    (label, freq, q, gain_or_None) bq_* tuples.
    """
    if isinstance(src, dict):
        secs = [(labels_of(s["kind"]), s["freq"], s["q"],
                 None if s["kind"] == "highpass" else s["gain_db"])
                for s in src["sections"]]
        return secs, src.get("linear_gain", 1.0), src
    secs, gain = parse_fit_text(src)
    return secs, gain, None


def labels_of(kind):
    return {"highpass": "bq_highpass", "lowpass": "bq_lowpass",
            "peaking": "bq_peaking", "lowshelf": "bq_lowshelf",
            "highshelf": "bq_highshelf"}.get(kind, "bq_" + kind)
