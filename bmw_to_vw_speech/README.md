# BMWtoVWSpeech

Aggressive speech in, friendly speech out. The driver curses, the car says it nicely, within a fraction of a second.

```text
🤬 "Fahr endlich, du Penner!"   ─▶  GPT-Realtime  ─▶  😇 "Würden Sie bitte losfahren? Danke!"
```

Runs on a laptop, independent of ROS and the car's software: microphone and speaker are streamed straight to the
[OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime) (`gpt-realtime`) over one WebSocket.

## Only what is said to the car in front

Only what the driver says **to the driver in front** is rephrased ("Fahr doch, es ist grün!", "Blinker!", "Was
bremst du denn?"). Everything else stays silent: passengers, phone calls, thinking aloud, navigation, comments about
the drive, and also cyclists, pedestrians and the car behind us.

```text
😇 Würden Sie bitte losfahren? Danke!            <- "Fahr endlich, du Penner!"
🤫 (not for the car in front, stays silent)       <- "Ich glaube, wir müssen noch tanken."
```

The model decides per sentence. When it is not for the car in front, it calls the function `stay_silent` instead of
speaking, and any audio of that turn is dropped. When unsure it stays silent: a missed rephrasing is better than
speaking normal talk out loud. `--all` rephrases everything again.

## Side events: swear words and advertisements

In parallel to the speech (it never waits for this), a small text model (`gpt-4.1-mini`) looks at the transcript:

| The driver ... | Console | Topic (`std_msgs/String`) |
|---|---|---|
| insults someone (outside or inside) | **FCK_PROJECTION** | `/Swearword`: the swear words, e.g. `"Penner"` |
| talks about hunger, food | 📢 McDonalds | `/advertisement`: `"McDonalds"` |
| talks about beer | 📢 Bayreuther | `/advertisement`: `"Bayreuther"` |
| talks about money, debts, prices, rent | 📢 Finanzberatung | `/advertisement`: `"Finanzberatung"` |
| finds the own car too slow (not someone else's) | 📢 Porsche | `/advertisement`: `"Porsche"` |

The topics are published on the car through its Foxglove bridge (`ws://10.42.0.1:8765`), with no ROS on the laptop,
the same way as [rear_buddy](../rear_buddy). Watch them in Foxglove or with
`ros2 topic echo /Swearword`. Without the car the console lines still appear and the bridge just keeps retrying
(`--no-bridge` silences that). To get topics **and** the OpenAI API at the same time, the laptop needs the `BITT`
Wi-Fi plus a second link with internet (Ethernet, USB tethering).

One sentence can trigger several ads ("Biergarten, Schnitzel und ein Helles" -> McDonalds and Bayreuther).
Categories, their values and descriptions are in `ADS` in [events.py](bmw_to_vw_speech/events.py): add a line there
for a new advertiser, the classifier prompt picks it up.

## Without the car: fake BITT in Docker

[sim/](sim) is the car as far as this project sees it: real ROS 2 Lyrical with a Foxglove bridge on port 8765. Works
with Docker Desktop on macOS/Windows (no host networking needed) and is isolated from a real car on the network.

```bash
docker compose -f sim/docker-compose.yml up --build -d          # first build takes a few minutes
uv run bmw-to-vw-speech --bridge ws://localhost:8765
docker compose -f sim/docker-compose.yml exec fake-bitt bash -ic "ros2 topic echo /Swearword"
docker compose -f sim/docker-compose.yml down
```

Or open Foxglove with `ws://localhost:8765` and add a Raw Messages panel for `/Swearword` and `/advertisement`.

With a recorded bag (rosbag2/MCAP directory in [bags/](../bags), ask your instructors), the fake car also replays
it in a loop, so Foxglove shows camera, TF, ... next to the topics:

```bash
BAG=<name> docker compose -f sim/docker-compose.yml up -d
BAG=<name> BAG_ARGS="--topics /tf /tf_static /preview/left/compressed" docker compose -f sim/docker-compose.yml up -d
```

For the full Gazebo simulation with Nav2 see the [main README](../README.md#-simulation) (dev container, Linux/WSL2).

## Setup

Needs [uv](https://docs.astral.sh/uv/).

```bash
cd bmw_to_vw_speech
uv sync                          # creates .venv from uv.lock
cp .env.example .env             # then put your OPENAI_API_KEY into .env (git-ignored)
uv run pytest                    # no key, mic or network needed
```

Variables already set in the shell take precedence over `.env`.

> [!IMPORTANT]
> The API needs **internet**. The car's `BITT` Wi-Fi has none, so the laptop needs a second link (phone hotspot,
> Ethernet/USB tethering) or simply stays off the car's network: this project does not talk to the car.

## Run

```bash
uv run bmw-to-vw-speech                                # microphone -> speaker, Ctrl-C to quit
uv run bmw-to-vw-speech --say "Bist du blind, du Idiot?"   # typed sentence, no microphone
uv run bmw-to-vw-speech --audio test.wav                 # recorded sentence (24 kHz mono WAV)
uv run bmw-to-vw-speech --list-devices                # pick a mic / speaker
uv run bmw-to-vw-speech --input-device 2 --output-device "JBL"
```

A spoken test sentence on macOS:
`say -v Anna -o t.aiff "Fahr endlich, du Penner!" && afconvert -f WAVE -d LEI16@24000 -c 1 t.aiff test.wav`

The console shows what was heard, what was said and how long it took:

```text
🤬 Fahr endlich, du Penner!
😇 Würden Sie bitte losfahren? Danke!  (420 ms to first audio + 350 ms pause detection)
```

| Option | Default | |
|---|---|---|
| `--all` | off | Rephrase everything, not only what is said to the car in front |
| `--silence-ms` | `350` | Pause that ends a sentence. Lower is faster but may cut the driver off mid-sentence |
| `--voice` | `marin` | Realtime voice (`marin`, `cedar`, `alloy`, `ash`, `coral`, `sage`, `verse`, ...) |
| `--model` | `gpt-realtime` | Or set `BMW2VW_MODEL`, e.g. a newer or cheaper realtime model |
| `--language` | `German` | Fallback language; it answers in whatever language it hears |
| `--noise-reduction` | `far_field` | `near_field` for a headset, `off` to save a few ms |
| `--full-duplex` | off | Keep the mic open while speaking: only with a headset |
| `--no-transcript` | off | Skip the "🤬" log line (does not change latency) |
| `--no-events` | off | No swear word / advertisement detection |
| `--event-model` | `gpt-4.1-mini` | Or set `BMW2VW_EVENT_MODEL` |
| `--bridge` | `ws://10.42.0.1:8765` | Foxglove bridge for the topics, or set `BITT_BRIDGE` |
| `--no-bridge` | off | Console only, publish nothing |
| `--swear-topic`, `--ad-topic` | `/Swearword`, `/advertisement` | Topic names |

## Why it is fast

- **One hop.** Speech-to-speech in one model: no separate STT → LLM → TTS chain.
- **Streaming both ways.** The mic is sent in 20 ms chunks, the answer plays from the first audio chunk on.
- **Server VAD with a short pause** (`--silence-ms`) starts the answer as soon as the driver stops; no button.
- **Tiny context.** Each turn stands alone; older conversation items are deleted after every response.
- **Short answers.** The prompt caps the output at the length of the original.

## Things to know

- **Half duplex.** While the car speaks, the microphone is muted (plus 300 ms echo tail), otherwise it would hear and
  "translate" itself forever. Shouting over the car is therefore not heard; use a headset and `--full-duplex` if you
  need that.
- **It always speaks.** Friendly sentences are repeated almost unchanged. The rules are in
  [prompt.py](bmw_to_vw_speech/prompt.py): tone, length and examples are the main knobs.
- **Costs.** The Realtime API is billed per audio minute in and out; stop it when you are not demoing.

## Files

| File | |
|---|---|
| [\_\_main\_\_.py](bmw_to_vw_speech/__main__.py) | CLI, WebSocket loop, event handling, latency log |
| [realtime.py](bmw_to_vw_speech/realtime.py) | Realtime API messages (session config, audio, history pruning), no I/O |
| [audio.py](bmw_to_vw_speech/audio.py) | Low-latency mic and speaker streams (PCM16, 24 kHz, `sounddevice`) |
| [events.py](bmw_to_vw_speech/events.py) | Insult / food detection, FCK_PROJECTION, which topic gets what |
| [foxglove.py](bmw_to_vw_speech/foxglove.py) | Publish-only Foxglove bridge client, `std_msgs/String` as CDR |
| [prompt.py](bmw_to_vw_speech/prompt.py) | The instructions that make the model a filter instead of a chat partner |
