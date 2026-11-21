"""BMWtoVWSpeech: aggressive speech in, friendly speech out, via GPT-Realtime.

    uv run bmw-to-vw-speech                              # microphone -> speaker
    uv run bmw-to-vw-speech --say "Fahr endlich, du Penner!"

Side events (parallel, never slow down the speech): insults -> FCK_PROJECTION + /Swearword,
talk about food -> /advertisement "McDonalds", both published on the car via its Foxglove bridge.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time

import websockets
from dotenv import find_dotenv, load_dotenv

from bmw_to_vw_speech import events, realtime
from bmw_to_vw_speech.foxglove import FoxglovePublisher, encode_string
from bmw_to_vw_speech.prompt import SILENT_TOOL, instructions, silent_tool


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="bmw_to_vw_speech", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=os.environ.get("BMW2VW_MODEL", "gpt-realtime"))
    p.add_argument("--voice", default=os.environ.get("BMW2VW_VOICE", "marin"))
    p.add_argument("--language", default="German", help="fallback language of the output")
    p.add_argument("--all", action="store_true",
                   help="rephrase everything (default: only what is said to the car in front)")
    p.add_argument("--silence-ms", type=int, default=350,
                   help="pause that ends a sentence; lower = faster, but may cut the driver off")
    p.add_argument("--noise-reduction", choices=["far_field", "near_field", "off"], default="far_field",
                   help="far_field for a laptop/car mic, near_field for a headset")
    p.add_argument("--full-duplex", action="store_true",
                   help="keep the mic open while speaking (only with a headset, otherwise it hears itself)")
    p.add_argument("--no-transcript", action="store_true", help="do not print the input transcript")
    p.add_argument("--no-events", action="store_true", help="no FCK_PROJECTION / advertisement detection")
    p.add_argument("--event-model", default=os.environ.get("BMW2VW_EVENT_MODEL", "gpt-4.1-mini"),
                   help="text model that detects insults and food talk")
    p.add_argument("--bridge", default=os.environ.get("BITT_BRIDGE", "ws://10.42.0.1:8765"),
                   help="the car's Foxglove bridge for the topics")
    p.add_argument("--no-bridge", action="store_true", help="console only, publish nothing")
    p.add_argument("--swear-topic", default="/Swearword")
    p.add_argument("--ad-topic", default="/advertisement")
    p.add_argument("--pacman-topic", default="/pacman", help='"Pacman-Modus an/aus" -> enable / disable')
    p.add_argument("--input-device", help="microphone (index or name part, see --list-devices)")
    p.add_argument("--output-device", help="speaker (index or name part)")
    p.add_argument("--list-devices", action="store_true")
    once = p.add_mutually_exclusive_group()
    once.add_argument("--say", metavar="TEXT", help="rephrase a typed sentence once and exit (no microphone)")
    once.add_argument("--audio", metavar="WAV", help="rephrase a recorded sentence (24 kHz mono WAV) and exit")
    args = p.parse_args(argv)
    args.once = bool(args.say or args.audio)
    return args


def _device(value):
    return int(value) if value and value.isdigit() else value


class App:
    def __init__(self, args, ws, key, bridge=None):
        self.args = args
        self.ws = ws
        self.key = key
        self.pending: set[asyncio.Task] = set()
        publish = None
        if bridge:
            # In one-shot mode the bridge may still be connecting: wait a moment before giving up.
            wait_s = 3.0 if args.once else 0.0
            publish = lambda topic, value: bridge.publish(topic, encode_string(value), wait_s=wait_s)  # noqa: E731
        self.events = events.Events(publish, args.swear_topic, args.ad_topic,
                                    log=lambda m: print(f"\r{m}", flush=True), pacman_topic=args.pacman_topic)
        self.history = realtime.History()
        self.speaker = None
        self.mic = None
        self.t_speech_end = None
        self.t_first_audio = None
        self.silenced = False  # the current response decided: talk inside the car, say nothing
        self.done = asyncio.Event()
        self.heard = asyncio.Event()  # input transcript arrived (it may come after the answer)

    async def send(self, event: dict):
        await self.ws.send(json.dumps(event))

    async def start(self):
        from bmw_to_vw_speech.audio import Microphone, Speaker
        await self.send(realtime.session_update(
            instructions(self.args.language, front_only=not self.args.all), voice=self.args.voice,
            silence_ms=self.args.silence_ms, transcribe=True,
            noise_reduction=self.args.noise_reduction, vad=not self.args.once,
            tools=None if self.args.all else [silent_tool()]))
        self.speaker = Speaker(_device(self.args.output_device))
        if self.args.once:
            if self.args.say:
                self.check(self.args.say)
                turn = realtime.say_text(self.args.say)
            else:
                turn = realtime.say_audio(realtime.read_wav(self.args.audio))
            for event in turn:
                await self.send(event)
            self.t_speech_end = time.monotonic()
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_chunk(pcm):
            # Half duplex: drop the mic while we talk, or the car translates its own voice forever.
            if self.args.full_duplex or not self.speaker.buffer.busy():
                loop.call_soon_threadsafe(queue.put_nowait, pcm)

        self.mic = Microphone(on_chunk, _device(self.args.input_device))
        print("🎙️  Listening. Say something rude. Ctrl-C to quit.", flush=True)
        while True:
            await self.send(realtime.append_audio(await queue.get()))

    def check(self, text: str):
        """Detect insults / food talk in the background; the speech does not wait for it."""
        if self.args.no_events or not text.strip():
            return

        def work():
            try:
                self.events.handle(text, events.classify(text, self.key, self.args.event_model))
            except Exception as e:  # noqa: BLE001 - side events must never break the speech
                print(f"\r⚠️  event detection failed: {e}", file=sys.stderr, flush=True)

        task = asyncio.create_task(asyncio.to_thread(work))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def receive(self):
        async for raw in self.ws:
            await self.handle(json.loads(raw))

    async def handle(self, ev: dict):
        t = ev.get("type", "")
        if t == "input_audio_buffer.speech_started":
            print("…", end="", flush=True)
        elif t == "input_audio_buffer.speech_stopped":
            self.t_speech_end = time.monotonic()
            self.t_first_audio = None
        elif t in ("conversation.item.added", "conversation.item.created"):
            self.history.added(ev.get("item", {}).get("id", ""))
        elif t == "conversation.item.input_audio_transcription.completed":
            transcript = ev.get("transcript", "").strip()
            if not self.args.no_transcript:
                print(f"\r🤬 {transcript}", flush=True)
            self.check(transcript)
            self.heard.set()
        elif t == "response.created":
            self.silenced = False
        elif t == "response.output_item.added" and ev.get("item", {}).get("name") == SILENT_TOOL:
            # Talk inside the car: drop whatever audio this response produces. We never answer the
            # function call, so no follow-up response is created.
            self.silenced = True
            self.speaker.buffer.clear()
            print("\r🤫 (not for the car in front, stays silent)", flush=True)
        elif t in ("response.output_audio.delta", "response.audio.delta"):
            if self.silenced:
                return
            if self.t_first_audio is None:
                self.t_first_audio = time.monotonic()
            self.speaker.buffer.write(base64.b64decode(ev["delta"]))
        elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            if self.silenced:
                return
            latency = ""
            if self.t_speech_end and self.t_first_audio:
                latency = f"  ({(self.t_first_audio - self.t_speech_end) * 1000:.0f} ms to first audio"
                if not self.args.once:
                    latency += f" + {self.args.silence_ms} ms pause detection"
                latency += ")"
            print(f"\r😇 {ev.get('transcript', '').strip()}{latency}", flush=True)
        elif t == "response.done":
            # After a silent turn drop everything, the unanswered function call must not linger.
            for event in self.history.prune(keep=0 if self.silenced else None):
                await self.send(event)
            if self.args.once:
                self.done.set()
        elif t == "error":
            err = ev.get("error", {})
            if err.get("code") != "item_not_found":  # pruning raced with the server, harmless
                print(f"\n⚠️  {err.get('message', ev)}", file=sys.stderr, flush=True)

    def close(self):
        for part in (self.mic, self.speaker):
            if part:
                part.close()


async def run(args):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("No OPENAI_API_KEY: cp .env.example .env and put your key in.")
    url = realtime.URL.format(model=args.model)
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {key}"},
                                  max_size=None, ping_interval=20) as ws:
        bridge = None
        if not args.no_events and not args.no_bridge:
            bridge = FoxglovePublisher(args.bridge, log=lambda m: print(f"\r{m}", flush=True)).start()
            bridge.advertise(args.swear_topic)
            bridge.advertise(args.ad_topic)
            bridge.advertise(args.pacman_topic)
        app = App(args, ws, key, bridge)
        receiver = asyncio.create_task(app.receive())
        try:
            if args.once:
                await app.start()
                await asyncio.wait({receiver, asyncio.create_task(app.done.wait())},
                                   return_when=asyncio.FIRST_COMPLETED)
                if args.audio and not args.no_events:
                    try:
                        await asyncio.wait_for(app.heard.wait(), 5)
                    except TimeoutError:
                        pass
                while app.speaker.buffer.busy(tail_s=0.2) or app.pending:
                    await asyncio.sleep(0.05)
            else:
                sender = asyncio.create_task(app.start())
                done, _ = await asyncio.wait({receiver, sender}, return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
        finally:
            receiver.cancel()
            app.close()
            if bridge:
                bridge.close()


def main(argv=None):
    # .env from the current directory upwards; variables already set in the shell win.
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args(argv)
    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nBye 👋")
    except websockets.ConnectionClosedError as e:
        sys.exit(f"Connection closed: {e}")


if __name__ == "__main__":
    main()
