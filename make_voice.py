"""
Checks the two test recordings are in place before a build.

sounds/music.wav is a short jazz loop. sounds/voice.wav is a radio announcement.
Both are part of the project, so there is nothing to download. If voice.wav is
ever missing, put a spoken clip at sounds/voice.mp3 and this script turns it
into voice.wav with an old-radio sound.
"""

import math
import struct
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOUNDS = HERE / "sounds"
MP3 = SOUNDS / "voice.mp3"
WAV = SOUNDS / "voice.wav"


def main():
    SOUNDS.mkdir(exist_ok=True)
    if WAV.exists():
        print("voice.wav is in place")
        return 0
    if not MP3.exists():
        print("sounds/voice.wav is missing. Put a spoken clip at sounds/voice.mp3 and build again.")
        return 1
    try:
        import miniaudio
    except ImportError:
        print("The miniaudio library is missing. Run: pip install miniaudio")
        return 1

    decoded = miniaudio.decode_file(str(MP3), output_format=miniaudio.SampleFormat.SIGNED16,
                                    nchannels=1, sample_rate=22050)
    rate = 22050

    def highpass(x, cutoff):
        rc = 1 / (2 * math.pi * cutoff)
        a = rc / (rc + 1 / rate)
        y = [0.0] * len(x)
        for i in range(1, len(x)):
            y[i] = a * (y[i - 1] + x[i] - x[i - 1])
        return y

    def lowpass(x, cutoff):
        rc = 1 / (2 * math.pi * cutoff)
        a = (1 / rate) / (rc + 1 / rate)
        y = [0.0] * len(x)
        for i in range(1, len(x)):
            y[i] = y[i - 1] + a * (x[i] - y[i - 1])
        return y

    x = [s / 32768 for s in decoded.samples]
    x = highpass(highpass(x, 300), 300)
    x = lowpass(lowpass(x, 3400), 3400)
    x = [math.tanh(v * 2.2) for v in x]
    peak = max(abs(v) for v in x) or 1
    frames = b"".join(struct.pack("<h", int(30000 * v / peak)) for v in x)
    with wave.open(str(WAV), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(frames)
    print("Made", WAV, f"({len(x) / rate:.1f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
