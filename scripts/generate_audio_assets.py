"""
generate_audio_assets.py
========================
Synthesizes tick.mp3 and outro.mp3 using only numpy + pydub (no ffmpeg needed).

  tick.mp3  – 5-second loop of mechanical clock ticks (sharp transient every 1s)
  outro.mp3 – 6-second lo-fi pad: layered sine tones that fade in/out

Run:
    python scripts/generate_audio_assets.py
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import wave

import numpy as np

OUT = Path("templates/audio")
OUT.mkdir(parents=True, exist_ok=True)

SAMPLE_RATE = 44100


# ── helpers ───────────────────────────────────────────────────────────────────

def _save_wav(samples: np.ndarray, path: Path, sr: int = SAMPLE_RATE) -> None:
    """Save float32 numpy array [-1,1] as 16-bit stereo WAV using stdlib only."""
    pcm = np.clip(samples, -1.0, 1.0)
    pcm_int = (pcm * 32767).astype(np.int16)
    # mono → stereo by duplicating channel
    stereo = np.column_stack([pcm_int, pcm_int])
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(stereo.tobytes())


def sine(freq: float, duration_s: float, amp: float = 1.0, sr: int = SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    return amp * np.sin(2 * math.pi * freq * t)


def envelope(samples: np.ndarray, attack_s: float, release_s: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    n = len(samples)
    att = int(attack_s * sr)
    rel = int(release_s * sr)
    env = np.ones(n)
    if att > 0:
        env[:att] *= np.linspace(0, 1, att)
    if rel > 0 and rel <= n:
        env[-rel:] *= np.linspace(1, 0, rel)
    return samples * env


# ── tick sound ────────────────────────────────────────────────────────────────

def make_tick(duration_s: float = 5.0) -> AudioSegment:
    """
    Mechanical clock tick: sharp transient every 1 second.
    Transient = band-pass noise burst (50ms) with fast decay.
    """
    sr = SAMPLE_RATE
    total = np.zeros(int(sr * duration_s))

    tick_len = int(0.05 * sr)   # 50ms burst
    for beat in range(int(duration_s)):
        t = np.linspace(0, 0.05, tick_len, endpoint=False)
        # layered tones: 1200 Hz click body + 3000 Hz high transient
        burst = (
            0.6 * np.sin(2 * math.pi * 1200 * t) +
            0.3 * np.sin(2 * math.pi * 3000 * t) +
            0.1 * (np.random.random(tick_len) * 2 - 1)
        )
        # exponential decay
        decay = np.exp(-60 * t)
        burst *= decay
        start = beat * sr
        end = start + tick_len
        if end <= len(total):
            total[start:end] += burst

    # Normalise
    peak = np.max(np.abs(total))
    if peak > 0:
        total /= peak
    total *= 0.75

    return total


# ── outro pad ─────────────────────────────────────────────────────────────────

def make_outro_pad(duration_s: float = 6.0) -> np.ndarray:
    """
    Warm lo-fi pad: root + fifth + octave, slow attack/release.
    Root = A3 (220 Hz), fifth = E4 (330 Hz), octave = A4 (440 Hz).
    """
    sr = SAMPLE_RATE
    freqs   = [220, 277.18, 329.63, 440]  # A3, C#4, E4, A4  (A major chord)
    weights = [0.35, 0.25, 0.25, 0.15]

    pad = np.zeros(int(sr * duration_s))
    for freq, w in zip(freqs, weights):
        layer = sine(freq, duration_s, amp=w, sr=sr)
        # slight detune per layer adds warmth
        layer += sine(freq * 1.002, duration_s, amp=w * 0.4, sr=sr)
        pad += layer

    # Slow attack (0.8s), slow release (1.2s)
    pad = envelope(pad, attack_s=0.8, release_s=1.2, sr=sr)

    # Soft saturation
    pad = np.tanh(pad * 1.5) / 1.5

    peak = np.max(np.abs(pad))
    if peak > 0:
        pad /= peak
    pad *= 0.70

    return pad


# ── beat bed ────────────────────────────────────────────────────────────────

def make_beat(duration_s: float = 4.0, bpm: float = 100.0) -> np.ndarray:
    """
    Loopable music bed: punchy pitch-swept kick on every beat, hi-hat on the
    off-beats, and a soft sub-bass pulse. Tasteful and driving, not cheesy.
    Designed to be looped seamlessly (integer number of beats).
    """
    sr = SAMPLE_RATE
    n = int(sr * duration_s)
    out = np.zeros(n)
    beat_dur = 60.0 / bpm
    n_beats = int(round(duration_s / beat_dur))

    for b in range(n_beats):
        # ── Kick: pitch sweep 110 → 45 Hz with fast amplitude decay ──
        kd = int(0.20 * sr)
        t = np.linspace(0, 0.20, kd, endpoint=False)
        freq = 45 + 75 * np.exp(-t * 28)
        phase = 2 * math.pi * np.cumsum(freq) / sr
        kick = np.sin(phase) * np.exp(-t * 16) * 0.95
        start = int(b * beat_dur * sr)
        end = min(start + kd, n)
        out[start:end] += kick[: end - start]

        # ── Sub-bass pulse under the kick ──
        sd = int(beat_dur * 0.7 * sr)
        ts = np.linspace(0, beat_dur * 0.7, sd, endpoint=False)
        sub = np.sin(2 * math.pi * 55 * ts) * np.exp(-ts * 6) * 0.18
        end = min(start + sd, n)
        out[start:end] += sub[: end - start]

        # ── Hi-hat on the off-beat (filtered noise burst) ──
        hstart = int((b + 0.5) * beat_dur * sr)
        hl = int(0.045 * sr)
        th = np.linspace(0, 0.045, hl, endpoint=False)
        hat = (np.random.random(hl) * 2 - 1) * np.exp(-th * 130) * 0.16
        end = min(hstart + hl, n)
        if hstart < n:
            out[hstart:end] += hat[: end - hstart]

    peak = np.max(np.abs(out))
    if peak > 0:
        out /= peak
    out = np.tanh(out * 1.2) / 1.2  # gentle glue compression
    out *= 0.85
    return out


# ── reveal ding ───────────────────────────────────────────────────────────────

def make_ding(duration_s: float = 0.6) -> np.ndarray:
    """Bright bell chime — 'correct answer' reveal sting (stacked harmonics)."""
    sr = SAMPLE_RATE
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    ding = (
        0.60 * np.sin(2 * math.pi * 880 * t) +
        0.30 * np.sin(2 * math.pi * 1760 * t) +
        0.15 * np.sin(2 * math.pi * 2640 * t)
    ) * np.exp(-t * 7.0)
    peak = np.max(np.abs(ding))
    if peak > 0:
        ding /= peak
    ding *= 0.8
    return ding


# ── transition whoosh ───────────────────────────────────────────────────────

def make_whoosh(duration_s: float = 0.45) -> np.ndarray:
    """Transition swoosh — noise + rising pitch sweep under a bell envelope."""
    sr = SAMPLE_RATE
    n = int(sr * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False)
    env = np.sin(math.pi * np.linspace(0, 1, n)) ** 2          # swell in/out
    noise = (np.random.random(n) * 2 - 1) * 0.5
    sweep = np.sin(2 * math.pi * (200 + 1600 * (t / duration_s)) * t) * 0.25
    whoosh = (noise + sweep) * env
    peak = np.max(np.abs(whoosh))
    if peak > 0:
        whoosh /= peak
    whoosh *= 0.6
    return whoosh


# ── record scratch ────────────────────────────────────────────────────────────

def make_scratch(duration_s: float = 0.5) -> np.ndarray:
    """
    Vinyl record-scratch: a buzzy sawtooth whose pitch zips up and down
    (the classic 'wikka-wikka'), plus vinyl crackle, under a swell envelope.
    """
    sr = SAMPLE_RATE
    n = int(sr * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False)

    mod = np.sin(2 * math.pi * 5.0 * t)          # 5 back-and-forth zips
    freq = 220.0 * (2.0 ** (1.3 * mod))          # +/- ~1.3 octaves
    phase = 2 * math.pi * np.cumsum(freq) / sr
    x = phase / (2 * math.pi)
    saw = 2.0 * (x - np.floor(x + 0.5))          # sawtooth from phase
    crackle = (np.random.random(n) * 2 - 1) * 0.14
    env = np.sin(math.pi * np.linspace(0, 1, n)) ** 1.3
    scratch = (saw * 0.55 + crackle) * env

    peak = np.max(np.abs(scratch))
    if peak > 0:
        scratch /= peak
    scratch *= 0.7
    return scratch


# ── fun outro jingle ──────────────────────────────────────────────────────────

def make_outro_jingle(duration_s: float = 3.0) -> np.ndarray:
    """Bright 'level complete' sting: ascending pluck arpeggio into a major chord."""
    sr = SAMPLE_RATE
    n = int(sr * duration_s)
    out = np.zeros(n)

    def pluck(freq: float, start_s: float, dur: float, amp: float) -> None:
        ln = int(dur * sr)
        tt = np.linspace(0, dur, ln, endpoint=False)
        tone = (np.sin(2 * math.pi * freq * tt) +
                0.35 * np.sin(2 * math.pi * freq * 2 * tt)) * np.exp(-tt * 7) * amp
        s = int(start_s * sr)
        e = min(s + ln, n)
        out[s:e] += tone[: e - s]

    # Ascending arpeggio: C5 E5 G5 C6
    for i, f in enumerate([523.25, 659.25, 783.99, 1046.50]):
        pluck(f, 0.10 * i, 0.45, 0.5)

    # Sustained bright C-major chord that rings to the end
    chord_start = 0.42
    cl = n - int(chord_start * sr)
    if cl > 0:
        tc = np.linspace(0, duration_s - chord_start, cl, endpoint=False)
        chord = np.zeros(cl)
        for f, w in [(261.63, 0.30), (329.63, 0.26), (392.00, 0.26),
                     (523.25, 0.22), (783.99, 0.14)]:
            chord += w * np.sin(2 * math.pi * f * tc)
        chord += 0.10 * np.sin(2 * math.pi * 1046.50 * tc) * np.exp(-tc * 3)  # sparkle
        chord *= np.exp(-tc * 1.1)
        s = int(chord_start * sr)
        out[s:s + cl] += chord

    out = np.tanh(out * 1.3) / 1.3
    peak = np.max(np.abs(out))
    if peak > 0:
        out /= peak
    out *= 0.85
    return out


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    assets = [
        ("tick.wav",    lambda: make_tick(5.0)),
        ("outro.wav",   lambda: make_outro_jingle(3.0)),
        ("beat.wav",    lambda: make_beat(4.8, bpm=100)),
        ("ding.wav",    lambda: make_ding(0.6)),
        ("whoosh.wav",  lambda: make_whoosh(0.45)),
        ("scratch.wav", lambda: make_scratch(0.5)),
    ]
    for name, gen in assets:
        path = OUT / name
        if path.exists():
            print(f"  Skipped (exists): {path}")
            continue
        print(f"  Generating {name} …")
        _save_wav(gen(), path)
        print(f"  Created: {path}  ({path.stat().st_size // 1024} KB)")

    print("\nDone. Run: python scripts/health_check.py")


if __name__ == "__main__":
    main()
