#!/usr/bin/env python3
"""
gen_click.py  –  Generate a synthetic UI click WAV that mimics the
                 iPhone keyboard tap (sharp attack, fast noise decay,
                 faint low-frequency thud).

Usage:
    python tools/gen_click.py              # writes src/sounds/click.wav
    python tools/gen_click.py --preview    # also plays it via simpleaudio

Output spec expected by CircuitPython audiocore.WaveFile:
    16-bit signed PCM  |  mono  |  22 050 Hz
"""

import argparse
import array
import math
import os
import random
import struct
import wave

SAMPLE_RATE  = 22_050   # Hz  – matches CircuitPython default
DURATION_MS  = 65       # ms  – total click length
ATTACK_MS    = 1.5      # ms  – linear ramp-up
DECAY_K      = 70       # exponential decay constant (higher = shorter)
PEAK         = 28_000   # peak amplitude (16-bit max is 32 767)

# Mix parameters (all 0–1, should sum ≤ 1)
MIX_NOISE    = 0.55     # white noise – gives the "tap" texture
MIX_TONE_MID = 0.25     # ~1.1 kHz – body/click character
MIX_TONE_LO  = 0.20     # ~200 Hz  – slight thud / weight

FREQ_MID = 1_100.0
FREQ_LO  =   200.0


def gen_samples():
    n = int(SAMPLE_RATE * DURATION_MS / 1000)
    attack_n = int(SAMPLE_RATE * ATTACK_MS / 1000)
    out = array.array("h")

    rng = random.Random(42)   # fixed seed → reproducible

    for i in range(n):
        t = i / SAMPLE_RATE

        # Envelope: linear attack then exponential decay
        if i < attack_n:
            env = i / attack_n
        else:
            env = math.exp(-DECAY_K * (t - ATTACK_MS / 1000))

        noise    = rng.uniform(-1.0, 1.0)
        tone_mid = math.sin(2 * math.pi * FREQ_MID * t)
        tone_lo  = math.sin(2 * math.pi * FREQ_LO  * t)

        val = env * (MIX_NOISE * noise + MIX_TONE_MID * tone_mid + MIX_TONE_LO * tone_lo)
        sample = int(val * PEAK)
        out.append(max(-32_768, min(32_767, sample)))

    return out


def write_wav(samples, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)          # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())
    size = os.path.getsize(path)
    print(f"Wrote {path}  ({size} bytes, {DURATION_MS} ms)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="src/sounds/click.wav")
    parser.add_argument("--preview", action="store_true",
                        help="Play the clip via simpleaudio (pip install simpleaudio)")
    args = parser.parse_args()

    samples = gen_samples()
    write_wav(samples, args.out)

    if args.preview:
        try:
            import simpleaudio as sa
            play = sa.play_buffer(samples.tobytes(), 1, 2, SAMPLE_RATE)
            play.wait_done()
        except ImportError:
            print("pip install simpleaudio  to enable --preview")
