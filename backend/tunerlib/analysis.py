"""Leakage-free Goertzel analysis (ports of analyse-dense.py / analyze-tone.py).

Invariant: every analysis window is EXACTLY one second at the wav's own sample
rate and the frequencies are integer Hz, so each tone lands exactly on a DFT
bin — no windowing, no leakage. The sample rate is read from the wav header,
never hardcoded.
"""

import math
import struct
import wave

from . import TunerError

FULL_SCALE = 32768.0


def read_mono(path):
    """Read a 16-bit wav as mono (channels averaged); returns (samples, fs)."""
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                raise TunerError("bad_wav", f"{path}: expected 16-bit pcm")
            fs = w.getframerate()
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
    except (wave.Error, FileNotFoundError, OSError) as e:
        raise TunerError("bad_wav", f"cannot read wav {path}: {e}") from e
    if len(raw) % 2:
        raise TunerError("bad_wav", f"{path}: truncated frame data")
    data = struct.unpack("<%dh" % (len(raw) // 2), raw)
    if ch > 1:
        data = [sum(data[i:i + ch]) / ch for i in range(0, len(data), ch)]
    return list(data), fs


def goertzel_amplitude(samples, fs, freq):
    """Amplitude of `freq` in `samples` (length should be fs: one second)."""
    n = len(samples)
    k = freq * n / fs
    if abs(k - round(k)) > 1e-6:
        raise TunerError("not_bin_aligned",
                         f"{freq} Hz over {n} samples at {fs} Hz is not "
                         f"bin-aligned (window must be an exact multiple of "
                         f"the tone period)")
    w = 2 * math.pi * round(k) / n
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for s in samples:
        s0 = s + coeff * s1 - s2
        s2, s1 = s1, s0
    return 2 * math.hypot(s1 - s2 * math.cos(w), s2 * math.sin(w)) / n


def dbfs(amp):
    return 20 * math.log10(amp / FULL_SCALE) if amp > 0 else -999.0


def analyse_response(path, freqs, start_s=0.3, window_s=1.0, progress_cb=None):
    """Level of each probe tone in a capture — port of analyse-dense.py.

    Reads the rate from the wav; the analysis window is exactly one second of
    it, so integer-Hz tones stay bin-aligned at any capture rate.
    Returns a list of {"freq": int, "dbfs": float}.
    """
    data, fs = read_mono(path)
    window = int(round(window_s * fs))
    if window != fs:
        # The window must be an integer number of samples; bin alignment then
        # requires integer-Hz tones AND an integer-second window at any rate.
        # fs samples == exactly one second, which is what we use.
        window = int(window_s * fs)
    start = int(start_s * fs)
    seg = data[start:start + window]
    if len(seg) < window:
        seg = data[:window]
    if len(seg) < window:
        raise TunerError("short_wav",
                         f"{path}: need {window / fs:.2f}s of audio, found "
                         f"{len(seg) / fs:.2f}s")
    out = []
    for i, f in enumerate(freqs):
        amp = goertzel_amplitude(seg, fs, f)
        out.append({"freq": f, "dbfs": round(dbfs(amp), 2)})
        if progress_cb and i % 32 == 0:
            progress_cb(i, len(freqs))
    if progress_cb:
        progress_cb(len(freqs), len(freqs))
    return out


def response_text(levels):
    """Upstream-compatible text form: '<freq> <dbfs>' per line."""
    return "\n".join(f"{lv['freq']} {lv['dbfs']:.2f}" for lv in levels)


def parse_response_text(text):
    """Parse '<freq> <dbfs>' lines into {freq: dbfs}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[int(parts[0])] = float(parts[1])
    return out


def delta(raw, reference):
    """reference - raw per shared frequency (port of response-delta.py).

    Both inputs are {freq: dbfs} dicts from analyse_response. When both captures
    went through the same mic in the same position, the mic's own response
    cancels in the subtraction.
    """
    shared = sorted(set(raw) & set(reference))
    if not shared:
        raise TunerError("no_overlap", "no frequencies in common between the "
                                       "raw and reference responses")
    return [{"freq": f, "db": round(reference[f] - raw[f], 2)} for f in shared]


def tone_analysis(path, f0, n_harmonics=6):
    """Fundamental + harmonics + THD of a mono wav — port of analyze-tone.py."""
    data, fs = read_mono(path)
    window = fs  # exactly one second: fundamental AND harmonics bin-aligned
    # Window is exactly one second (fs samples): for integer-Hz f0, every
    # harmonic h*f0 is also integer-Hz and thus bin-aligned.
    seg = data[:window] if len(data) >= window else data
    amps = []
    for h in range(1, n_harmonics + 2):
        f = f0 * h
        if f > fs / 2 * 0.95:
            amps.append(0.0)
        else:
            amps.append(goertzel_amplitude(seg, fs, f))
    fund = amps[0]
    harm = math.sqrt(sum(a * a for a in amps[1:]))
    thd = (harm / fund * 100) if fund > 0 else float("nan")
    return {
        "freq": f0,
        "fs": fs,
        "fundamental_dbfs": round(dbfs(fund), 1),
        "harmonics_dbfs": [round(dbfs(a), 1) for a in amps[1:]],
        "thd_pct": round(thd, 2),
    }


def _aligned(freq, n, fs):
    return abs(freq * n / fs - round(freq * n / fs)) <= 1e-6


def _dft_term(samples, fs, freq):
    n = len(samples)
    w = 2 * math.pi * freq / fs
    re = sum(s * math.cos(w * i) for i, s in enumerate(samples))
    im = -sum(s * math.sin(w * i) for i, s in enumerate(samples))
    return 2 * math.hypot(re, im) / n
