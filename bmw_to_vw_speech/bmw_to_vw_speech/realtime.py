"""OpenAI Realtime API (GA) messages, kept free of I/O so they can be tested."""

import base64

URL = "wss://api.openai.com/v1/realtime?model={model}"
RATE = 24000  # the Realtime API's PCM16 sample rate


def session_update(instructions: str, voice: str = "marin", silence_ms: int = 350,
                   transcribe: bool = True, noise_reduction: str = "far_field", vad: bool = True,
                   tools: list[dict] | None = None) -> dict:
    audio_in = {
        "format": {"type": "audio/pcm", "rate": RATE},
        # Server-side VAD answers as soon as the driver pauses: lowest latency without push-to-talk.
        "turn_detection": None if not vad else {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 200,
            "silence_duration_ms": silence_ms,
            "create_response": True,
            "interrupt_response": False,  # the mic is gated while we speak, see audio.py
        },
    }
    if noise_reduction != "off":
        audio_in["noise_reduction"] = {"type": noise_reduction}
    if transcribe:
        # Runs in parallel to the response, only for the console log; it does not delay the answer.
        audio_in["transcription"] = {"model": "gpt-4o-mini-transcribe"}
    session = {
        "type": "realtime",
        "instructions": instructions,
        "output_modalities": ["audio"],
        "max_output_tokens": 300,
        "audio": {
            "input": audio_in,
            "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": voice},
        },
    }
    if tools:
        session["tools"] = tools
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}


def append_audio(pcm: bytes) -> dict:
    return {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")}


def say_text(text: str) -> list[dict]:
    """Feed a typed sentence instead of speech (testing without a microphone)."""
    return [
        {"type": "conversation.item.create",
         "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
        {"type": "response.create"},
    ]


def say_audio(pcm: bytes, chunk: int = 32000) -> list[dict]:
    """Feed a recorded utterance (PCM16 mono 24 kHz) as one turn; needs the session without VAD."""
    events = [append_audio(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk)]
    return events + [{"type": "input_audio_buffer.commit"}, {"type": "response.create"}]


def read_wav(path: str) -> bytes:
    import wave
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path}: need 24 kHz mono 16-bit WAV, convert with: "
                             f"afconvert -f WAVE -d LEI16@24000 -c 1 in.xxx out.wav  (or ffmpeg -ar 24000 -ac 1)")
        return w.readframes(w.getnframes())


class History:
    """Each turn stands alone: older items are deleted so the context, and with it the latency, stays small."""

    def __init__(self, keep: int = 2):
        self.keep = keep
        self.ids: list[str] = []

    def added(self, item_id: str) -> None:
        if item_id and item_id not in self.ids:
            self.ids.append(item_id)

    def prune(self, keep: int | None = None) -> list[dict]:
        keep = self.keep if keep is None else keep
        cut = max(len(self.ids) - keep, 0)
        old, self.ids = self.ids[:cut], self.ids[cut:]
        return [{"type": "conversation.item.delete", "item_id": i} for i in old]
