#!/usr/bin/env python3
"""A pretend car on your laptop: a Foxglove bridge that only knows the lights. For testing without the car.

    python3 -m projector_indicator.fake_car                 # ws://localhost:8765; type l, r, h, o + Enter to set the indicator
    python3 -m projector_indicator.fake_car --cycle 3       # change by itself every 3 s: left, right, hazard, off
    python3 -m projector_indicator.indicator --url ws://localhost:8765     # in a second terminal: the projection follows

Publishes /lights_state (car_interfaces/LightsState) 10 times per second, like a status topic of the firmware, and
/advertisement (std_msgs/String) whenever you type "a Some text".
Accepts /lights_cmd from clients and "shows" it, like the ESP32: so the Foxglove app (connect it to
ws://localhost:8765, the light buttons publish /lights_cmd) can
drive it too. Speaks just enough of the Foxglove WebSocket protocol for that.
"""
import argparse
import itertools
import json
import struct
import sys
import threading
import time

import numpy as np
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from .cdr import DEFINITIONS, Codec
from .foxglove_client import OP_MESSAGE_DATA, OP_SERVICE_CALL, OP_SERVICE_RESPONSE, SUBPROTOCOL

STATE_TOPIC, CMD_TOPIC, TEXT_TOPIC, PERSONS_TOPIC = '/lights_state', '/lights_cmd', '/advertisement', '/persons'
STATE_CHANNEL, TEXT_CHANNEL, PERSONS_CHANNEL, STEER_CHANNEL, ODOM_CHANNEL = 1, 2, 3, 4, 5
ODOM_TOPIC = '/wheel_odom'
STEER_TOPIC, STOP_SERVICE, STOP_SERVICE_ID = '/steer_state', '/control_room/cancel_navigation', 100
CAR_FRONT_M = 1.05  # base_link x of the front bumper
KEYS = {'o': 0, 'l': 1, 'r': 2, 'h': 3}   # LightsCmd.MODE_*
NAMES = {0: 'off', 1: 'left', 2: 'right', 3: 'hazard'}
AMBER = (255, 120, 0)


def ros2msg_schema(type_name):
    """Full schema text as foxglove_bridge advertises it: the definition, then every nested type once."""
    sections, seen, todo = [], set(), [type_name]
    while todo:
        name = todo.pop(0)
        if name in seen:
            continue
        seen.add(name)
        text = DEFINITIONS[name]
        sections.append(text if name == type_name else f'MSG: {name.replace("/", "/msg/")}\n{text}')
        todo += [line.split()[0] for line in text.splitlines() if '/' in line.split()[0]]
    return ('\n' + '=' * 80 + '\n').join(sections)


def indicator_cmd(mode, blink_period_ms=500):
    lit = AMBER if mode else (0, 0, 0)
    return {'front_rgb': list(lit) * 4, 'rear_rgb': list(lit) * 32, 'mode': mode, 'blink_period_ms': blink_period_ms}


class FakeCar:
    def __init__(self, host='127.0.0.1', port=8765, rate_hz=10.0, log=print):
        self.codec, self.log, self.period = Codec(), log, 1.0 / rate_hz
        self.lock = threading.Lock()
        self.actual = indicator_cmd(0)
        self.steer_rad = 0.0
        self.speed_mps = 0.0   # both wheels, + = forward
        self.stops = 0     # how often the stop service was called
        self.text_senders = set()
        self.server = serve(self._handler, host, port, subprotocols=[SUBPROTOCOL])
        self.port = self.server.socket.getsockname()[1]
        self.url = f'ws://{"localhost" if host in ("127.0.0.1", "0.0.0.0") else host}:{self.port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set_mode(self, mode):
        self.command(indicator_cmd(mode))

    def say(self, text, channel=TEXT_CHANNEL, quiet=False):
        """Publish one std_msgs/String message (/advertisement by default), like a node would."""
        payload = self.codec.encode('std_msgs/String', {'data': text})
        with self.lock:
            senders = list(self.text_senders)
        for send in senders:
            send(payload, channel)
        if not quiet:
            self.log(f'fake car: {channel} {text!r} to {len(senders)} connection(s)')

    def show_person(self, x, y):
        """Publish one /persons message with a person at (x, y) in base_link metres, like the person detection."""
        zone = 'ahead' if abs(y) < 0.4 else 'left' if y > 0 else 'right'
        msg = {'stamp': time.time(), 'frame_id': 'base_link', 'persons': [
            {'x': x, 'y': y, 'distance': round(max(0.0, x - CAR_FRONT_M), 2),
             'angle_deg': round(float(np.degrees(np.arctan2(y, x - 0.92))), 1), 'zone': zone, 'method': 'ground',
             'conf': 0.9, 'box': [0, 0, 0, 0]}]}
        self.say(json.dumps(msg), PERSONS_CHANNEL, quiet=True)
        return zone

    def command(self, cmd):
        """What a /lights_cmd would do: the firmware shows it and reports it on /lights_state."""
        with self.lock:
            before = self.actual.get('mode')
            self.actual = cmd
        if cmd.get('mode') != before:
            self.log(f'fake car: indicator {NAMES.get(cmd.get("mode"), cmd.get("mode"))}')

    def state_payload(self):
        with self.lock:
            actual = dict(self.actual)
        sec, nsec = divmod(time.time_ns(), 10 ** 9)
        return self.codec.encode('car_interfaces/LightsState', {
            'header': {'stamp': {'sec': sec, 'nanosec': nsec}, 'frame_id': ''},
            'actual': actual, 'failsafe_active': False})

    def close(self):
        self.server.shutdown()

    def _handler(self, ws):
        subscriptions = {}            # subscription id -> channel id
        client_channels = {}          # client channel id -> (topic, encoding)
        ws.send(json.dumps({'op': 'serverInfo', 'name': 'fake BITT', 'capabilities': ['clientPublish'],
                            'supportedEncodings': ['cdr', 'json']}))
        ws.send(json.dumps({'op': 'advertise', 'channels': [
            {'id': STATE_CHANNEL, 'topic': STATE_TOPIC, 'encoding': 'cdr',
             'schemaName': 'car_interfaces/msg/LightsState', 'schemaEncoding': 'ros2msg',
             'schema': ros2msg_schema('car_interfaces/LightsState')},
            {'id': TEXT_CHANNEL, 'topic': TEXT_TOPIC, 'encoding': 'cdr', 'schemaName': 'std_msgs/msg/String',
             'schemaEncoding': 'ros2msg', 'schema': 'string data'},
            {'id': PERSONS_CHANNEL, 'topic': PERSONS_TOPIC, 'encoding': 'cdr', 'schemaName': 'std_msgs/msg/String',
             'schemaEncoding': 'ros2msg', 'schema': 'string data'},
            {'id': STEER_CHANNEL, 'topic': STEER_TOPIC, 'encoding': 'cdr', 'schemaName': 'car_interfaces/msg/SteerState',
             'schemaEncoding': 'ros2msg', 'schema': ros2msg_schema('car_interfaces/SteerState')},
            {'id': ODOM_CHANNEL, 'topic': ODOM_TOPIC, 'encoding': 'cdr', 'schemaName': 'car_interfaces/msg/WheelOdom',
             'schemaEncoding': 'ros2msg', 'schema': ros2msg_schema('car_interfaces/WheelOdom')}]}))
        ws.send(json.dumps({'op': 'advertiseServices', 'services': [
            {'id': STOP_SERVICE_ID, 'name': STOP_SERVICE, 'type': 'std_srvs/srv/Trigger',
             'request': {'encoding': 'cdr', 'schemaName': 'std_srvs/srv/Trigger_Request', 'schemaEncoding': 'ros2msg',
                         'schema': 'uint8 structure_needs_at_least_one_member'},
             'response': {'encoding': 'cdr', 'schemaName': 'std_srvs/srv/Trigger_Response',
                          'schemaEncoding': 'ros2msg', 'schema': 'bool success\nstring message'}}]}))
        done = threading.Event()

        def stream():
            while not done.wait(self.period):
                payloads = {STATE_CHANNEL: self.state_payload(),
                            STEER_CHANNEL: self.codec.encode('car_interfaces/SteerState',
                                                             {'angle_rad': self.steer_rad, 'calibrated': True}),
                            ODOM_CHANNEL: self.codec.encode('car_interfaces/WheelOdom',
                                                            {'left_speed_mps': self.speed_mps,
                                                             'right_speed_mps': self.speed_mps})}
                for sub_id, channel in list(subscriptions.items()):
                    if channel in payloads:
                        try:
                            ws.send(struct.pack('<BIQ', OP_MESSAGE_DATA, sub_id, time.time_ns()) + payloads[channel])
                        except ConnectionClosed:
                            return

        def send_text(payload, wanted):
            for sub_id, channel in list(subscriptions.items()):
                if channel == wanted:
                    try:
                        ws.send(struct.pack('<BIQ', OP_MESSAGE_DATA, sub_id, time.time_ns()) + payload)
                    except ConnectionClosed:
                        pass

        with self.lock:
            self.text_senders.add(send_text)
        threading.Thread(target=stream, daemon=True).start()
        try:
            for raw in ws:
                if isinstance(raw, str):
                    msg = json.loads(raw)
                    op = msg.get('op')
                    if op == 'subscribe':
                        subscriptions.update({s['id']: s['channelId'] for s in msg.get('subscriptions', [])})
                    elif op == 'unsubscribe':
                        for sub_id in msg.get('subscriptionIds', []):
                            subscriptions.pop(sub_id, None)
                    elif op == 'advertise':
                        client_channels.update({c['id']: (c['topic'], c.get('encoding', 'cdr'))
                                                for c in msg.get('channels', [])})
                    elif op == 'unadvertise':
                        for cid in msg.get('channelIds', []):
                            client_channels.pop(cid, None)
                elif len(raw) >= 13 and raw[0] == OP_SERVICE_CALL:
                    _, service_id, call_id, enc_len = struct.unpack_from('<BIII', raw, 0)
                    enc = raw[13:13 + enc_len]
                    if service_id == STOP_SERVICE_ID:
                        self.stops += 1
                        self.log('fake car: navigation cancelled, the car stops')
                        answer = self.codec.encode('std_srvs/Trigger_Response',
                                                   {'success': True, 'message': 'Navigation cancelled (fake car)'})
                        ws.send(struct.pack('<BIII', OP_SERVICE_RESPONSE, service_id, call_id, len(enc)) + enc + answer)
                elif len(raw) >= 5 and raw[0] == OP_MESSAGE_DATA:
                    topic, encoding = client_channels.get(struct.unpack_from('<I', raw, 1)[0], (None, None))
                    if topic == CMD_TOPIC:
                        try:  # our client sends CDR, the Foxglove app's Publish panel JSON
                            self.command(json.loads(raw[5:]) if encoding == 'json'
                                         else self.codec.decode('car_interfaces/LightsCmd', raw[5:]))
                        except Exception as e:  # noqa: BLE001 - a broken message must not end the connection
                            self.log(f'fake car: could not decode /lights_cmd: {e}')
        except ConnectionClosed:
            pass
        finally:
            done.set()
            with self.lock:
                self.text_senders.discard(send_text)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--host', default='127.0.0.1', help='0.0.0.0 to let other devices connect')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--rate', type=float, default=10.0, help='/lights_state messages per second')
    p.add_argument('--cycle', type=float, help='change the indicator by itself every this many seconds')
    args = p.parse_args()

    car = FakeCar(args.host, args.port, args.rate)
    print(f'fake car on {car.url}: /lights_state out, /lights_cmd in. Ctrl-C to stop')
    stop = threading.Event()
    try:
        if args.cycle:
            for mode in itertools.cycle([1, 2, 3, 0]):
                car.set_mode(mode)
                if stop.wait(args.cycle):
                    break
        else:
            print('type l (left), r (right), h (hazard), o (off) and Enter; "a Some text" publishes /advertisement; '
                  '"p 2.5 -0.3" a person at x=2.5 m, y=-0.3 m (base_link) on /persons for 3 s; "s 0.3" steers '
                  '(rad, + left); "v 0.5" drives at 0.5 m/s (/wheel_odom, - = backwards, v 0 stops); q quits')
            for line in sys.stdin:
                if line.lower().startswith('a '):
                    car.say(line[2:].strip())
                    continue
                if line.lower().startswith('v '):
                    try:
                        car.speed_mps = float(line.split()[1])
                        print(f'fake car: driving at {car.speed_mps:+.2f} m/s')
                    except (ValueError, IndexError):
                        print('v speed_in_m_per_s, e.g. v 0.5 (forward), v -0.3 (backwards), v 0 (stop)')
                    continue
                if line.lower().startswith('s '):
                    try:
                        car.steer_rad = float(line.split()[1])
                        print(f'fake car: steering {car.steer_rad:+.2f} rad (+ = left)')
                    except (ValueError, IndexError):
                        print('s angle_in_rad, e.g. s 0.3 (left) or s -0.3 (right)')
                    continue
                if line.lower().startswith('p '):
                    try:
                        x, y = (float(v) for v in line.split()[1:3])
                    except ValueError:
                        print('p x y, e.g. p 2.5 -0.3')
                        continue
                    threading.Thread(target=lambda: [car.show_person(x, y) or time.sleep(0.1) for _ in range(30)],
                                     daemon=True).start()
                    print(f'fake car: person at x={x} y={y} for 3 s')
                    continue
                key = line.strip().lower()[:1]
                if key == 'q':
                    break
                if key in KEYS:
                    car.set_mode(KEYS[key])
                elif key:
                    print('l, r, h, o or q')
    except KeyboardInterrupt:
        pass
    finally:
        car.close()
        print('fake car stopped')


if __name__ == '__main__':
    main()
