"""Dense pink-weighted multitone probe generation (port of measure/multitone.py).

Invariants preserved from upstream:
- tones are integer Hz and analysed over exactly one second, so each lands on a
  DFT bin (leakage-free) at ANY sample rate — rate is a parameter, not 48000;
- one tone per twelfth octave, pink-weighted (1/sqrt(f));
- default composite peak -6 dBFS so the probe never clips the path under test.

Standard library only (no third-party dependencies).
"""

import math
import os
import random
import struct
import wave

from . import TunerError


def frequencies(f_lo=40.0, f_hi=16000.0, steps_per_octave=12):
    out, f = [], f_lo
    while f <= f_hi:
        i = int(round(f))
        if i not in out:
            out.append(i)
        f *= 2 ** (1 / steps_per_octave)
    return out


def freqs_path_for(wav_path):
    root, _ = os.path.splitext(wav_path)
    return root + "-freqs.txt"


def write_freqs(freqs, path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(str(f) for f in freqs) + "\n")


def read_freqs(path):
    try:
        with open(path) as fh:
            return [int(line) for line in fh if line.strip()]
    except OSError as e:
        raise TunerError("no_freqs", f"no tone list at {path}: {e}") from e


def gen(path, fs=48000, seconds=6, peak_db=-6.0, seed=7):
    """Generate the probe; returns (freqs, freqs_path).

    The tone list is written NEXT TO the probe (path stem + -freqs.txt) so
    analyse can read back exactly what was used.
    """
    freqs = frequencies()
    rng = random.Random(seed)
    phases = [rng.uniform(0, 2 * math.pi) for _ in freqs]
    amps = [1 / math.sqrt(f) for f in freqs]

    # One second of samples; repeated verbatim, so the probe loops cleanly and
    # any integer-Hz tone stays bin-aligned in a one-second analysis window.
    two_pi = 2 * math.pi
    one = [sum(a * math.sin(two_pi * f * n / fs + p)
               for f, a, p in zip(freqs, amps, phases))
           for n in range(fs)]

    peak = max(abs(v) for v in one)
    scale = (10 ** (peak_db / 20)) / peak
    one = [max(-1.0, min(1.0, v * scale)) for v in one]

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(fs)
        frames = bytearray()
        for _ in range(seconds):
            for v in one:
                s = int(v * 32767)
                frames += struct.pack("<hh", s, s)
        w.writeframes(bytes(frames))
    freqs_path = freqs_path_for(path)
    write_freqs(freqs, freqs_path)
    return freqs, freqs_path


def make_tone(path, freq, fs=48000, seconds=1.0, amp=0.25, channels=1):
    """A plain single-tone wav with a KNOWN amplitude (for fixtures/tests)."""
    if freq * 2 > fs / 2 * 0.95:
        raise TunerError("bad_freq", f"{freq} Hz too high for {fs} Hz")
    n = int(round(fs * seconds))
    data = [amp * math.sin(2 * math.pi * freq * i / fs) for i in range(n)]
    _write_pcm(path, data, fs, seconds, channels)


def make_mix(path, tones, fs=48000, seconds=1.0, channels=1):
    """Multi-tone wav with known per-tone amplitudes: tones = [(freq, amp), ...].

    Tones must be integer Hz so a one-second Goertzel window is bin-aligned.
    """
    n = int(round(fs * seconds))
    data = [0.0] * n
    for f, a in tones:
        fi = round(f)
        if abs(f - fi) > 1e-9 or fi * seconds != round(fi * seconds):
            raise TunerError("bad_freq", f"{f} Hz is not integer-Hz (bin-aligned)")
        for i in range(n):
            data[i] += a * math.sin(2 * math.pi * fi * i / fs)
    if data and max(abs(v) for v in data) > 1.0:
        raise TunerError("bad_freq", "tone mix exceeds full scale")
    _write_pcm(path, data, fs, seconds, channels)


def _write_pcm(path, data, fs, seconds, channels):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(fs)
        frames = bytearray()
        for v in data:
            s = int(max(-1.0, min(1.0, v)) * 32767)
            if channels > 1:
                frames += struct.pack("<%dh" % channels, *([s] * channels))
            else:
                frames += struct.pack("<h", s)
        # data covers int(seconds) seconds already; no extra repetition needed
        w.writeframes(bytes(frames))
