#!/usr/bin/env python3
"""Turn signal projection: shows on the ground in front of the car which way BITT is indicating.

    python3 -m projector_indicator.indicator                       # the car (ws://10.42.0.1:8765), projector = 2nd screen
    python3 -m projector_indicator.indicator --demo                # no car: left, right, hazard, off, one after another
    python3 -m projector_indicator.fake_car                        # no car: a pretend car on this laptop (other terminal) ...
    python3 -m projector_indicator.indicator --url ws://localhost:8765   # ... and the projection follows it
    python3 -m projector_indicator.indicator --calibrate           # the projection shows FRONT / LEFT / RIGHT to adjust it
    python3 -m projector_indicator.indicator --rotate 270 --no-mirror     # projector the other way round / not mirrored
    python3 -m projector_indicator.indicator --keystone 0.4        # less trapezoid correction (slanted projection, 0 = none)
    python3 -m projector_indicator.indicator --list-screens        # which screen is which
    python3 -m projector_indicator.projector_cal                   # measure the projection with a camera (then used here)

Only listens, never sends: reads /lights_state (what the firmware really shows, so also indicators from Foxglove,
controls.py, Nav2 code or the failsafe). The page is drawn in the car's frame (up = ahead of the car, left = the
car's left), pre-distorted into a trapezoid by --keystone (the projector shines onto the ground at about 45 degrees, so
its far edge comes out wider), mirrored and turned by --rotate to fit the projector: it is mounted turned by 90
degrees. On the page: R turns it by another 90 degrees, M mirrors, Up/Down change the keystone (Shift: faster),
C toggles the calibration picture, F full screen.
When the car is not heard from for --stale-s seconds the projection goes dark: better nothing than a wrong arrow.
"""
import argparse
import itertools
import json
import math
import pathlib
import threading
import time
import urllib.parse
import urllib.request

from .ads import LOGOS, advert
from .cdr import Codec
from .display_server import DisplayServer, local_ips
from .ground import Ground, Odometry, corridor_contains
from .screens import monitors, open_fullscreen, pick_screen

PAGE = pathlib.Path(__file__).with_name('indicator.html')
CALIBRATION = pathlib.Path(__file__).with_name('projector_local.json')  # written by projector_cal.py (our set-up is committed)
MODES = {1: 'left', 2: 'right', 3: 'hazard'}   # LightsCmd.MODE_*; MODE_SOLID (0) and anything else: off
TYPES = {'/lights_state': 'car_interfaces/LightsState', '/lights_cmd': 'car_interfaces/LightsCmd'}
DEMO = [('left', 4.0), ('off', 1.5), ('right', 4.0), ('off', 1.5), ('hazard', 4.0), ('off', 1.5)]


class Indicator:
    """What to project. Feed it decoded messages, ask state() regularly (also when no message comes)."""

    def __init__(self, stale_s=3.0, clock=time.monotonic, text='', text_s=0.0, persons_s=1.0):
        self.stale_s, self.clock = stale_s, clock
        self.direction, self.period_ms, self.failsafe = 'off', 500, False
        self.last = None
        self.text, self.text_s, self.text_since = text, text_s, clock()
        self.tracks = PersonTracks(hold_s=persons_s, clock=clock)

    def on_text(self, text):
        """A new message for the projection (the advertisement); '' clears it."""
        self.text, self.text_since = text.strip(), self.clock()

    def on_persons(self, text):
        """A /persons message (JSON, see parse_persons). A broken one is ignored, the last good one ages out."""
        persons = parse_persons(text)
        if persons is not None:
            self.tracks.update(persons)

    def on_lights(self, msg):
        """A decoded LightsState, or a LightsCmd (then it is used as it is)."""
        actual = msg.get('actual', msg)
        self.direction = MODES.get(actual.get('mode', 0), 'off')
        self.period_ms = actual.get('blink_period_ms') or 500
        self.failsafe = bool(msg.get('failsafe_active', False))
        self.last = self.clock()

    def alive(self):
        if self.last is None:
            return False
        return self.stale_s <= 0 or self.clock() - self.last <= self.stale_s

    def state(self, connected=True):
        live = connected and self.alive()
        fresh = self.text_s <= 0 or self.clock() - self.text_since <= self.text_s
        return {'direction': self.direction if live else 'off', 'period_ms': self.period_ms,
                'live': live, 'failsafe': self.failsafe and live, 'text': self.text if connected and fresh else '',
                'persons': self.tracks.current() if connected else []}


ZONES = ('ahead', 'left', 'right')


class PersonTracks:
    """Follows the detected persons from message to message and smooths their positions, so that what is
    projected glides instead of jumping. A detection is matched to the nearest track of the previous messages
    (within gate_m); its position moves towards the new one with time constant tau_s. A track nobody is matched to
    any more is kept for hold_s (the page fades it out), then dropped."""

    def __init__(self, tau_s=0.35, gate_m=1.0, hold_s=1.0, clock=time.monotonic):
        self.tau_s, self.gate_m, self.hold_s, self.clock = tau_s, gate_m, hold_s, clock
        self.tracks, self.next_id = [], 1

    def update(self, persons):
        now = self.clock()
        free = list(self.tracks)
        for p in persons:  # nearest first, as they come
            best = min(free, key=lambda t: math.hypot(t['x'] - p['x'], t['y'] - p['y']), default=None)
            if best is not None and math.hypot(best['x'] - p['x'], best['y'] - p['y']) <= self.gate_m:
                free.remove(best)
                a = 1 - math.exp(-max(now - best['seen'], 0.0) / self.tau_s)
                for k in ('x', 'y', 'distance'):
                    best[k] += a * (p[k] - best[k])
                best.update(zone=p['zone'], conf=p['conf'], seen=now)
            else:
                self.tracks.append({**p, 'id': self.next_id, 'seen': now})
                self.next_id += 1
        self.tracks = [t for t in self.tracks if now - t['seen'] <= self.hold_s]

    def current(self):
        """The tracks still held, nearest first: id, x, y, zone, distance, conf and seen (1 = in the last message,
        falling to 0 until it is dropped)."""
        now = self.clock()
        out = []
        for t in sorted(self.tracks, key=lambda t: t['distance']):
            age = now - t['seen']
            if age <= self.hold_s:
                out.append({'id': t['id'], 'x': round(t['x'], 3), 'y': round(t['y'], 3), 'zone': t['zone'],
                            'distance': round(t['distance'], 2), 'conf': t['conf'],
                            'seen': round(max(0.0, 1 - age / self.hold_s), 2) if self.hold_s > 0 else 1.0})
        return out


def parse_persons(text, max_persons=5):
    """The persons of one /persons message: [{'x', 'y', 'zone', 'distance', 'conf'}], nearest first, in base_link
    metres (x ahead of the rear axle, y to the left). None when the message is not readable."""
    try:
        msg = json.loads(text)
        out = []
        for p in msg.get('persons', []):
            x, y = float(p['x']), float(p['y'])
            zone = p.get('zone') if p.get('zone') in ZONES else ('ahead' if abs(y) < 0.4 else 'left' if y > 0 else 'right')
            out.append({'x': round(x, 3), 'y': round(y, 3), 'zone': zone,
                        'distance': round(float(p.get('distance', x)), 2), 'conf': round(float(p.get('conf', 1.0)), 2)})
        return out[:max_persons]
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


GROUND_KEYS = ('world_to_screen', 'size_mm', 'footprint_mm', 'floor_mm', 'box_mm')


def page_query(rotate, mirror, calibrate, keystone=0.0, calibration=None):
    """calibration: measured by projector_cal.py (the ground in mm and how it maps onto the screen); it replaces
    rotate, mirror and keystone."""
    if calibration:
        query = {'ground': json.dumps({k: calibration[k] for k in GROUND_KEYS}, separators=(',', ':'))}
    else:
        query = {'rotate': rotate % 360}
        if mirror:
            query['mirror'] = 1
        if keystone:
            query['keystone'] = round(keystone, 2)
    if calibrate:
        query['calibrate'] = 1
    return '?' + urllib.parse.urlencode(query, safe=',')


def page_url(port, rotate, mirror, calibrate, keystone=0.0, calibration=None):
    return f'http://127.0.0.1:{port}/' + page_query(rotate, mirror, calibrate, keystone, calibration)


def load_calibration(path):
    try:
        cal = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(cal, dict) or not all(k in cal for k in GROUND_KEYS) or len(cal['world_to_screen']) != 9:
        print(f'{path}: not a calibration of this version, measure again with projector_indicator.projector_cal')
        return None
    return cal


def run_demo(display, stop, period_ms, text=''):
    for direction, seconds in itertools.cycle(DEMO):
        display.set_state({'direction': direction, 'period_ms': period_ms, 'live': True, 'failsafe': False,
                           'text': text, 'ad': advert(text)})
        print(f'demo: {direction}')
        if stop.wait(seconds):
            return


STEER_TAU_S = 0.12   # smoothing of the steering angle for the drawn path (time constant)

def run_car(display, stop, args, calibration=None):
    from .foxglove_client import FoxgloveClient  # needs websockets, the demo does not

    codec = Codec()
    ground = Ground(calibration, args.light_start_m, args.light_offset_m) if calibration else None
    steer = {'rad': None, 'raw': 0.0, 'at': None, 'glided': 0.0}
    odometry = Odometry()
    indicator = Indicator(args.stale_s, text=args.text, text_s=args.text_s, persons_s=args.persons_s)
    type_name = TYPES[args.topic]
    shown = {}
    light = {'start_m': args.light_start_m, 'offset_m': args.light_offset_m}
    topics = {args.topic} | {t for t in (args.text_topic, args.persons_topic, args.steer_topic, args.odom_topic) if t}

    def on_message(topic, payload, _channel):
        if topic == args.topic:
            indicator.on_lights(codec.decode(type_name, payload))
        elif topic == args.persons_topic:
            indicator.on_persons(codec.decode('std_msgs/String', payload)['data'])
        elif topic == args.steer_topic:
            steer['raw'], steer['at'] = codec.decode('car_interfaces/SteerState', payload)['angle_rad'], time.monotonic()
        elif topic == args.odom_topic:
            wheels = codec.decode('car_interfaces/WheelOdom', payload)
            odometry.update((wheels['left_speed_mps'] + wheels['right_speed_mps']) / 2,
                            steer['raw'] if steer['at'] is not None else 0.0, time.monotonic())
        else:
            text = codec.decode('std_msgs/String', payload)['data']
            print(f'{topic}: {text!r}')
            indicator.on_text(text)

    # --local-url: /persons and /advertisement from a bridge on this laptop (the car's bridge may not pass on what
    # other laptops publish), the car's own topics still from the car.
    local_topics = {t for t in (args.text_topic, args.persons_topic) if t} if args.local_url else set()
    client = FoxgloveClient(args.url, topics - local_topics, on_message=on_message).start()
    local = FoxgloveClient(args.local_url, local_topics, on_message=on_message).start() if local_topics else None
    print(f'connecting to {args.url} (on the car network?), listening to {", ".join(sorted(topics - local_topics))}'
          + (f'; to {args.local_url} for {", ".join(sorted(local_topics))}' if local else '') + ' ... Ctrl-C to stop')
    warned = False
    started = time.monotonic()
    try:
        while not stop.is_set():
            state = indicator.state(client.connected.is_set())
            now = time.monotonic()
            # held as long as the indicator: the bridge's stream stalls for up to a few seconds now and then
            has_steer = steer['at'] is not None and now - steer['at'] < max(args.stale_s, 1.0) and state['live']
            if not has_steer or steer['rad'] is None:
                steer['rad'] = steer['raw']        # first value (or after a gap): as it is
            else:                                  # else glide towards it: the path swings over, it does not jump
                steer['rad'] += (steer['raw'] - steer['rad']) * (1 - math.exp(-(now - steer['glided']) / STEER_TAU_S))
            steer['glided'] = now
            in_path = [p['id'] for p in state['persons'] if has_steer and corridor_contains(p['x'], p['y'], steer['rad'])]
            state['corridor'] = ({'steer_rad': round(steer['rad'], 3), 'blocked': bool(in_path)}
                                 if has_steer and ground else None)
            # the car's motion: the page pins the advertisement to the ground with it
            state['pose'] = odometry.pose(now) if state['live'] else None
            if state != shown:
                if state['direction'] != shown.get('direction') or state['live'] != shown.get('live'):
                    print(f"indicator: {state['direction']}" + ('' if state['live'] else ' (no data from the car)'))
                if len(state['persons']) != len(shown.get('persons', [])):
                    print(f"persons: {len(state['persons'])}"
                          + ''.join(f" [{p['zone']} x={p['x']:.1f} y={p['y']:.1f}]" for p in state['persons']))
                shown = state
                display.set_state({**state, 'ad': advert(state['text']), 'light': light})
            if not warned and client.connected.is_set() and indicator.last is None and time.monotonic() - started > 8:
                warned = True
                topics = client.advertised_topics()
                print(f'no {args.topic} yet. ' + ('The car does not offer it. ' if args.topic not in topics else '')
                      + 'Try --topic /lights_cmd, or --stale-s 0 if the car only sends it on changes.')
            stop.wait(0.05)
    finally:
        client.close()
        if local:
            local.close()


def ask_to_quit(port):
    """Ask an indicator already serving on this port (an older start) to stop. True when one agreed."""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/quit', timeout=2) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def start_server(port, stop, page=PAGE, **kwargs):
    """The page server. A running indicator on the same port is asked to stop first (it closes its projector
    window), so restarting never leaves the old one running."""
    try:
        return DisplayServer(port=port, page=page, on_quit=stop.set, assets=LOGOS, **kwargs)
    except OSError:
        pass
    replaced = ask_to_quit(port)
    if replaced:
        print(f'stopping the indicator that was already running on port {port} ...')
    deadline = time.monotonic() + 20   # it closes its bridge connection and projector window first
    while True:
        try:
            server = DisplayServer(port=port, page=page, on_quit=stop.set, assets=LOGOS, **kwargs)
            if replaced:
                time.sleep(1.5)  # let its projector window close before ours opens
            return server
        except OSError:
            if time.monotonic() > deadline:
                raise SystemExit(f'port {port} is still in use (an indicator that did not stop, or another '
                                 'program): stop it, or use --port') from None
            time.sleep(0.3)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--url', default='ws://10.42.0.1:8765', help="the car's Foxglove bridge")
    p.add_argument('--topic', default='/lights_state', choices=sorted(TYPES), help='where the indicator state comes from')
    p.add_argument('--demo', action='store_true', help='no car: cycle through left, right, hazard and off')
    p.add_argument('--port', type=int, default=8081, help='port of the projection page')
    p.add_argument('--rotate', type=int, default=90, choices=[0, 90, 180, 270], help='turn the picture (degrees, clockwise)')
    p.add_argument('--mirror', action=argparse.BooleanOptionalAction, default=True,
                   help='mirror the picture (default: on, as the projector is mounted)')
    p.add_argument('--keystone', type=float, default=0.5,
                   help='trapezoid correction -0.9..0.9: how much narrower the front edge is drawn (0: none)')
    p.add_argument('--calibrate', action='store_true', help='start with the FRONT / LEFT / RIGHT picture')
    p.add_argument('--calibration', default=str(CALIBRATION),
                   help='camera calibration from projector_cal.py (replaces --rotate/--mirror/--keystone if present)')
    p.add_argument('--no-calibration', action='store_true', help='ignore the camera calibration')
    p.add_argument('--screen', type=int, help='screen number for the projection (default: the first non-primary one)')
    p.add_argument('--list-screens', action='store_true', help='print the screens and quit')
    p.add_argument('--no-browser', action='store_true', help='only serve the page, open it yourself')
    p.add_argument('--stale-s', type=float, default=3.0, help='go dark after this long without data (0: never)')
    p.add_argument('--text-topic', default='/advertisement',
                   help="std_msgs/String topic whose message is written onto the ground ('' for none)")
    p.add_argument('--text', default='', help='text to show until the first message comes (and in demo mode)')
    p.add_argument('--text-s', type=float, default=0.0, help='hide a message after this many seconds (0: keep it)')
    p.add_argument('--local-url', default='',
                   help='a second bridge (e.g. ws://localhost:8765 on this laptop) for --text-topic and --persons-topic')
    p.add_argument('--persons-topic', default='/persons',
                   help="std_msgs/String with the detected persons as JSON: a soft red patch at their place ('' for none)")
    p.add_argument('--steer-topic', default='/steer_state',
                   help="car_interfaces/SteerState: the path the car is about to drive is drawn on the ground ('' for none)")
    p.add_argument('--odom-topic', default='/wheel_odom',
                   help="car_interfaces/WheelOdom: the advertisement stays put on the ground while the car drives ('' for "
                        "none: it rides along)")
    p.add_argument('--persons-s', type=float, default=1.0,
                   help='fade a person out over this long when the detection no longer reports them')
    p.add_argument('--light-start-m', type=float, default=1.05,
                   help='where the light patch begins: metres from the rear axle (base_link x) to the middle of its '
                        'edge next to the car (default: at the front bumper, 1.05 m)')
    p.add_argument('--light-offset-m', type=float, default=0.0,
                   help='sideways offset of that point from the car\'s centre line in metres, left positive')
    p.add_argument('--period-ms', type=int, default=500, help='blink period in demo mode')
    args = p.parse_args()

    screens = monitors()
    if args.list_screens:
        for s in screens:
            print(f'screen {s.index}: {s.width}x{s.height} at ({s.x}, {s.y})' + (' primary' if s.primary else ''))
        if not screens:
            print('could not list the screens on this system')
        return

    stop = threading.Event()
    display = start_server(args.port, stop, page=PAGE,
                           state={'direction': 'off', 'period_ms': args.period_ms, 'live': False, 'failsafe': False})
    if not -0.9 <= args.keystone <= 0.9:
        p.error('--keystone must be between -0.9 and 0.9')
    calibration = None if args.no_calibration else load_calibration(args.calibration)
    if calibration:
        width, length = calibration['size_mm']
        print(f'using the camera calibration {args.calibration}: {width:.0f} x {length:.0f} mm of ground '
              '(--no-calibration to ignore it)')
    url = page_url(display.port, args.rotate, args.mirror, args.calibrate, args.keystone, calibration)
    print(f'projection page: {url}  (from other devices: '
          + ', '.join(f'http://{ip}:{display.port}/' for ip in local_ips()) + ')')
    browser = None
    if not args.no_browser:
        screen = pick_screen(screens, args.screen)
        if screen is None or (screen.primary and args.screen is None):
            print('no second screen found: opening on the main screen (is the projector switched on and set to '
                  '"Extend" with Win+P?)')
        else:
            print(f'opening on screen {screen.index} ({screen.width}x{screen.height}); Alt+F4 closes it')
        browser = open_fullscreen(url, screen)

    try:
        if args.demo:
            run_demo(display, stop, args.period_ms, args.text)
        else:
            run_car(display, stop, args, calibration)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        display.set_state({'direction': 'off', 'period_ms': args.period_ms, 'live': False, 'failsafe': False})
        time.sleep(0.2)  # let the page go dark before the server stops
        display.close()
        if browser is not None:
            browser.terminate()
        print('stopped')


if __name__ == '__main__':
    main()
