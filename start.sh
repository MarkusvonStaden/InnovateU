#!/bin/bash
# Starts everything for the demo and keeps it running. Ctrl-C stops all of it (and closes the kiosk windows).
#
#   ./start.sh          real car (ws://10.42.0.1:8765) + projector
#   ./start.sh sim      fake BITT in Docker (bag replay) + a normal monitor instead of the projector
#
# What runs:
#   projector_indicator  full screen on the projector: indicator, path, ads, Pacman
#   person_finder        people in front of the car (front camera)
#   bmw_to_vw_speech     microphone -> friendly speech, /advertisement, /Swearword, /pacman
#   rear_buddy           rear camera -> job / flirt card in a kiosk window, texts
#
# On the car the laptops' messages go through a bridge on this laptop (Docker container laptop-bridge): the car's
# bridge does not pass on what laptops publish. Every service is restarted when it stops (e.g. the OpenAI
# connection drops). Logs: logs/<service>.log
#
# Settings (environment variables):
#   BEAMER_SCREEN=1        screen of the projector (see: cd projector_indicator && uv run python -m
#                          projector_indicator.indicator --list-screens)
#   PITCH_SCREEN=0         screen of rear_buddy's kiosk window; PITCH_WINDOW=0 for none
#   CALIBRATION=...        projector calibration (default: the latest measured one)
#   LEDS=1                 rear_buddy drives the car's rear lights (default off: they would fight the indicator)

set -u
MODE="${1:-car}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOGS="$ROOT/logs"
CAR=ws://10.42.0.1:8765
LOCAL=ws://localhost:8765
BEAMER_SCREEN="${BEAMER_SCREEN:-1}"
PITCH_SCREEN="${PITCH_SCREEN:-0}"
PITCH_WINDOW="${PITCH_WINDOW:-1}"
LEDS="${LEDS:-0}"
mkdir -p "$LOGS"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
port_open() { nc -z -G 2 "$1" "$2" >/dev/null 2>&1; }

# ---- the bridge ---------------------------------------------------------------------------------------------------
case "$MODE" in
  car)
    port_open 10.42.0.1 8765 || { say "The car's bridge (10.42.0.1:8765) is not reachable: on the BITT Wi-Fi?"; exit 1; }
    curl -s -o /dev/null --max-time 5 https://api.openai.com/v1/models \
      || say "Warning: no internet (OpenAI): speech, car brands and person check will not work. Phone tethering?"
    docker stop sim-fake-bitt-1 >/dev/null 2>&1
    if docker ps -a --format '{{.Names}}' | grep -qx laptop-bridge; then
      docker start laptop-bridge >/dev/null
    else   # the fake BITT image without a bag: just a foxglove_bridge
      docker run -d --name laptop-bridge -p 8765:8765 -e ROS_DOMAIN_ID=77 \
        -e ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST bmw-to-vw-speech-fake-bitt >/dev/null
    fi
    CALIBRATION="${CALIBRATION:-projector_indicator/projector_local_2026-11-21_neu.json}"
    ;;
  sim)
    docker stop laptop-bridge >/dev/null 2>&1
    docker start sim-fake-bitt-1 >/dev/null || { say "No sim-fake-bitt-1 container: see bmw_to_vw_speech/README.md"; exit 1; }
    CALIBRATION="${CALIBRATION:-projector_indicator/monitor_topview.json}"
    ;;
  *) echo "usage: $0 [car|sim]"; exit 1 ;;
esac
say "Waiting for the bridge on this laptop ..."
for _ in $(seq 1 30); do port_open localhost 8765 && break; sleep 1; done
port_open localhost 8765 || { say "The bridge on localhost:8765 did not come up (Docker running?)"; exit 1; }
sleep 2

# ---- services: each in a loop that restarts it -------------------------------------------------------------------
PIDS=()
run() {   # run <name> <dir> <command ...>
  local name=$1 dir=$2; shift 2
  (
    child=
    trap '[ -n "$child" ] && kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT
    cd "$ROOT/$dir" || exit 1
    while true; do
      echo "===== $(date '+%F %T') start: $*" >> "$LOGS/$name.log"
      PYTHONUNBUFFERED=1 "$@" >> "$LOGS/$name.log" 2>&1 &
      child=$!
      wait "$child"
      echo "===== $(date '+%F %T') stopped (exit $?), restarting in 3 s" >> "$LOGS/$name.log"
      sleep 3
    done
  ) &
  PIDS+=($!)
  say "started $name (log: logs/$name.log)"
}

stop_all() {
  trap - INT TERM
  say "Stopping ..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
  pkill -f bitt-indicator-browser 2>/dev/null       # the kiosk windows
  pkill -f rear-buddy-pitch-browser 2>/dev/null
  say "All stopped. (The Docker bridge keeps running: docker stop laptop-bridge / sim-fake-bitt-1)"
  exit 0
}
trap stop_all INT TERM

if [ "$MODE" = car ]; then
  run projector_indicator projector_indicator uv run python -m projector_indicator.indicator \
      --url "$CAR" --local-url "$LOCAL" --screen "$BEAMER_SCREEN" --calibration "$CALIBRATION"
  run person_finder person_finder uv run person-finder --bridge "$CAR" --publish-bridge "$LOCAL"
  REAR=(--url "$CAR")
else
  run projector_indicator projector_indicator uv run python -m projector_indicator.indicator \
      --url "$LOCAL" --screen "$BEAMER_SCREEN" --calibration "$CALIBRATION"
  run person_finder person_finder uv run person-finder --bridge "$LOCAL"
  REAR=(--url "$LOCAL" --topic /preview/left/compressed)   # the bag has no rear camera
fi
run bmw_to_vw_speech bmw_to_vw_speech uv run bmw-to-vw-speech --bridge "$LOCAL"
[ "$PITCH_WINDOW" = 1 ] && REAR+=(--pitch-window --pitch-screen "$PITCH_SCREEN")
[ "$LEDS" = 1 ] || REAR+=(--no-leds)
run rear_buddy rear_buddy .venv/bin/python -m rear_buddy.bridge "${REAR[@]}"

say "Everything runs ($MODE). Ctrl-C stops it. Follow a log with: tail -f logs/<service>.log"
wait
