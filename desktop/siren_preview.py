"""Render the ESP's siren generator on the PC so it can be auditioned.

Mirrors ampSirenPlay() in firmware/trace_e_bot/amp.cpp exactly (same rate,
harmonics, glide and fades) so what you hear here is what the amp will play.

    python desktop/siren_preview.py            # UK two-tone -> siren_uk.wav
    python desktop/siren_preview.py wail       # US sweep
"""

import math
import struct
import sys
import wave

RATE = 16000  # AMP_SAMPLE_RATE


def render(ms=4000, lo=440, hi=587, wail_ms=1000, mode=1):
    total = int(ms * RATE / 1000)
    wail = int(wail_ms * RATE / 1000)
    fade = RATE // 50
    glide = 1.0 / (0.022 * RATE)

    phase = 0.0
    freq = float(hi if mode == 1 else lo)
    out = []
    two_pi = 2.0 * math.pi

    for idx in range(total):
        pos = idx % wail
        if mode == 1:
            target = float(hi) if pos * 2 < wail else float(lo)
            freq += (target - freq) * glide
            phase += two_pi * freq / RATE
            if phase > two_pi:
                phase -= two_pi
            sample = (
                math.sin(phase)
                + 0.45 * math.sin(phase * 2.0)
                + 0.22 * math.sin(phase * 3.0)
            ) * 0.6
        else:
            tri = pos / wail
            tri = tri * 2.0 if tri < 0.5 else 2.0 - tri * 2.0
            freq = lo + (hi - lo) * tri
            phase += two_pi * freq / RATE
            if phase > two_pi:
                phase -= two_pi
            sample = math.sin(phase)

        env = 1.0
        if idx < fade:
            env = idx / fade
        left = total - idx
        if left < fade:
            env = left / fade

        s = int(sample * env * 11000.0)
        out.append(max(-32768, min(32767, s)))
    return out


def main():
    mode = 0 if (len(sys.argv) > 1 and sys.argv[1].lower() in ("wail", "us", "0")) else 1
    if mode == 1:
        samples = render(mode=1)
        path = "siren_uk.wav"
    else:
        samples = render(lo=600, hi=1600, wail_ms=700, mode=0)
        path = "siren_wail.wav"

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    print(f"wrote {path} ({len(samples) / RATE:.1f}s)")


if __name__ == "__main__":
    main()
