"""Microphone and speaker as PCM16 mono streams at 24 kHz, with as little buffering as possible."""

import threading
import time

from bmw_to_vw_speech.realtime import RATE

CHUNK_MS = 20


class PcmBuffer:
    """Thread-safe byte FIFO between the WebSocket (writer) and the sound card callback (reader)."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_played = 0.0

    def write(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm

    def read(self, n: int) -> bytes:
        """Exactly n bytes, padded with silence."""
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            if chunk:
                self._last_played = time.monotonic()
        return chunk + b"\x00" * (n - len(chunk))

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def busy(self, tail_s: float = 0.3) -> bool:
        """True while audio is queued or just finished (the room still echoes it)."""
        with self._lock:
            return bool(self._buf) or time.monotonic() - self._last_played < tail_s


class Speaker:
    def __init__(self, device=None):
        import sounddevice as sd
        self.buffer = PcmBuffer()
        self._stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", latency="low",
                                          device=device, callback=self._callback)
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):
        outdata[:] = self.buffer.read(len(outdata))

    def close(self):
        self._stream.stop()
        self._stream.close()


class Microphone:
    """Calls on_chunk(bytes) from the audio thread every CHUNK_MS."""

    def __init__(self, on_chunk, device=None):
        import sounddevice as sd
        self._stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", latency="low",
                                         blocksize=RATE * CHUNK_MS // 1000, device=device,
                                         callback=lambda data, frames, t, status: on_chunk(bytes(data)))
        self._stream.start()

    def close(self):
        self._stream.stop()
        self._stream.close()
