"""Tests for the turn signal projection and the fake car: python3 -m pytest test/"""
import json
import queue
import struct
import time
import urllib.request

import pytest
from websockets.sync.client import connect

from projector_indicator.cdr import Codec, parse, schema_sections
from projector_indicator.display_server import DisplayServer
from projector_indicator.fake_car import FakeCar, indicator_cmd, ros2msg_schema
from projector_indicator.foxglove_client import SUBPROTOCOL, FoxgloveClient
from projector_indicator.indicator import PAGE, Indicator, page_url
from projector_indicator.screens import Screen, _xrandr_monitors, browser_command, pick_screen

codec = Codec()


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def lights_state(mode, period=500):
    return codec.decode('car_interfaces/LightsState', codec.encode('car_interfaces/LightsState', {
        'actual': indicator_cmd(mode, period), 'failsafe_active': False}))


# ---- what to project -------------------------------------------------------------------------------------------

@pytest.mark.parametrize('mode, direction', [(0, 'off'), (1, 'left'), (2, 'right'), (3, 'hazard'), (9, 'off')])
def test_modes_map_to_directions(mode, direction):
    ind = Indicator(clock=Clock())
    ind.on_lights(lights_state(mode))
    assert ind.state()['direction'] == direction


def test_dark_until_the_car_is_heard_and_when_it_goes_quiet():
    clock = Clock()
    ind = Indicator(stale_s=3.0, clock=clock)
    assert ind.state() == {'direction': 'off', 'period_ms': 500, 'live': False, 'failsafe': False, 'text': '', 'persons': []}
    ind.on_lights(lights_state(1, 700))
    assert ind.state() == {'direction': 'left', 'period_ms': 700, 'live': True, 'failsafe': False, 'text': '', 'persons': []}
    clock.t += 3.5
    assert ind.state()['direction'] == 'off' and not ind.state()['live']


def test_no_staleness_check_with_zero():
    clock = Clock()
    ind = Indicator(stale_s=0, clock=clock)
    ind.on_lights(lights_state(2))
    clock.t += 1000
    assert ind.state()['direction'] == 'right'


def test_dark_when_the_bridge_connection_is_lost():
    ind = Indicator(clock=Clock())
    ind.on_lights(lights_state(3))
    assert ind.state(connected=False)['direction'] == 'off'


def test_lights_cmd_works_too():
    ind = Indicator(clock=Clock())
    ind.on_lights(codec.decode('car_interfaces/LightsCmd', codec.encode('car_interfaces/LightsCmd', indicator_cmd(2))))
    assert ind.state()['direction'] == 'right'


def test_page_url():
    assert page_url(8081, 90, False, False) == 'http://127.0.0.1:8081/?rotate=90'
    assert page_url(8081, 270, True, True) == 'http://127.0.0.1:8081/?rotate=270&mirror=1&calibrate=1'
    assert page_url(8081, 90, True, False, 0.456) == 'http://127.0.0.1:8081/?rotate=90&mirror=1&keystone=0.46'


def test_indicator_page_is_served_with_its_state():
    s = DisplayServer(host='127.0.0.1', port=0, page=PAGE, state={'direction': 'off', 'live': False})
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{s.port}/?rotate=90', timeout=5) as r:
            assert b'id="world"' in r.read()
        s.set_state({'direction': 'left', 'live': True})
        with urllib.request.urlopen(f'http://127.0.0.1:{s.port}/state', timeout=5) as r:
            assert json.loads(r.read()) == {'direction': 'left', 'live': True}
    finally:
        s.close()


# ---- LightsState on the wire ------------------------------------------------------------------------------------

def test_lights_state_round_trip_and_layout():
    payload = codec.encode('car_interfaces/LightsState', {'actual': indicator_cmd(1, 400), 'failsafe_active': True})
    msg = codec.decode('car_interfaces/LightsState', payload)
    assert msg['actual']['mode'] == 1 and msg['actual']['blink_period_ms'] == 400 and msg['failsafe_active'] is True
    # header (4 + 4 + 4 + 1), pad 3 (the nested stamp is int32), nested LightsCmd (header 13 + 12 + 96 + mode 1
    # + uint16 2, already aligned + flags 1), bool 1
    assert len(payload) == 4 + 13 + 3 + 13 + 12 + 96 + 1 + 2 + 1 + 1


def test_advertised_schema_parses_like_ours():
    sections = schema_sections('car_interfaces/msg/LightsState', ros2msg_schema('car_interfaces/LightsState'))
    assert set(sections) == {'car_interfaces/LightsState', 'car_interfaces/LightsCmd', 'std_msgs/Header',
                             'builtin_interfaces/Time'}
    for name, text in sections.items():
        assert [f.name for f in parse(text, name.split('/')[0])] == [f.name for f in codec.fields(name)]


# ---- the fake car through the real client ---------------------------------------------------------------------

@pytest.fixture
def car():
    c = FakeCar(port=0, rate_hz=50, log=lambda m: None)
    yield c
    c.close()


def test_fake_car_streams_lights_state_and_obeys_lights_cmd(car):
    got = queue.Queue()
    client = FoxgloveClient(car.url, {'/lights_state'}, log=lambda m: None,
                            on_message=lambda t, p, c: got.put(codec.decode('car_interfaces/LightsState', p)))
    client.advertise('/lights_cmd', 'car_interfaces/msg/LightsCmd')
    client.start()
    try:
        assert client.connected.wait(5)

        def wait_for(mode):
            for _ in range(200):
                if got.get(timeout=5)['actual']['mode'] == mode:
                    return True
            return False

        assert wait_for(0)
        car.set_mode(1)                       # as if someone pressed "left" on the car
        assert wait_for(1)
        client.publish('/lights_cmd', codec.encode('car_interfaces/LightsCmd', indicator_cmd(3)))  # like Foxglove
        assert wait_for(3)
    finally:
        client.close()


def test_fake_car_takes_json_like_the_foxglove_app(car):
    with connect(car.url, subprotocols=[SUBPROTOCOL]) as ws:
        ws.recv(), ws.recv()  # serverInfo, advertise
        ws.send(json.dumps({'op': 'advertise', 'channels': [
            {'id': 7, 'topic': '/lights_cmd', 'encoding': 'json', 'schemaName': 'car_interfaces/msg/LightsCmd'}]}))
        ws.send(struct.pack('<BI', 1, 7) + json.dumps(indicator_cmd(2)).encode())
        for _ in range(100):
            if codec.decode('car_interfaces/LightsState', car.state_payload())['actual']['mode'] == 2:
                break
            ws.ping().wait(1)
        assert codec.decode('car_interfaces/LightsState', car.state_payload())['actual']['mode'] == 2


# ---- screens ---------------------------------------------------------------------------------------------------

def test_projector_is_the_first_non_primary_screen():
    screens = [Screen(0, -1920, 0, 1920, 1080, False), Screen(1, 0, 0, 2560, 1440, True)]
    assert pick_screen(screens, None).index == 0
    assert pick_screen(screens, 1).index == 1
    assert pick_screen([Screen(0, 0, 0, 1920, 1080, True)], None).primary
    assert pick_screen([], None) is None
    with pytest.raises(SystemExit):
        pick_screen(screens, 5)


def test_xrandr_parsing():
    text = ('eDP-1 connected primary 1920x1200+0+0 (normal) 300mm x 190mm\n'
            'HDMI-1 connected 1280x800+1920+0 (normal)\nDP-1 disconnected (normal)\n')
    assert _xrandr_monitors(text) == [Screen(0, 0, 0, 1920, 1200, True), Screen(1, 1920, 0, 1280, 800, False)]


def test_browser_opens_as_kiosk_on_the_screen():
    cmd = browser_command('msedge', 'http://127.0.0.1:8081/', Screen(1, 1920, 0, 1280, 800, False))
    assert '--window-position=1920,0' in cmd and '--kiosk' in cmd and cmd[-1] == '--app=http://127.0.0.1:8081/'
    assert any(c.startswith('--user-data-dir=') for c in cmd)


def test_a_new_indicator_replaces_the_running_one():
    import threading
    from projector_indicator.indicator import start_server
    old_stop, new_stop = threading.Event(), threading.Event()
    old = start_server(0, old_stop, page=PAGE, state={'direction': 'off'})
    threading.Thread(target=lambda: (old_stop.wait(), old.close()), daemon=True).start()  # what its main loop does
    new = start_server(old.port, new_stop, page=PAGE, state={'direction': 'off'})   # same port: the old one is asked
    assert old_stop.is_set()                                                         # ...and it stopped
    try:
        assert not new_stop.is_set()
        with urllib.request.urlopen(f'http://127.0.0.1:{new.port}/state', timeout=5) as r:
            assert json.loads(r.read()) == {'direction': 'off'}
    finally:
        new.close()


def test_quit_is_only_offered_when_asked_for():
    s = DisplayServer(host='127.0.0.1', port=0, page=PAGE, state={})
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f'http://127.0.0.1:{s.port}/quit', timeout=5)
    finally:
        s.close()


def test_the_indicator_server_serves_the_indicator_page():
    import threading
    from projector_indicator.indicator import start_server
    s = start_server(0, threading.Event(), state={})   # as main() starts it: no page given
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{s.port}/', timeout=5) as r:
            assert b'id="chevrons"' in r.read()
    finally:
        s.close()


def test_the_advertisement_text_is_shown_while_connected():
    clock = Clock()
    ind = Indicator(clock=clock, text='Hello')
    assert ind.state()['text'] == 'Hello'               # until the first message
    ind.on_text('  BMW  ')
    assert ind.state()['text'] == 'BMW'
    assert ind.state(connected=False)['text'] == ''      # no car, no text
    clock.t += 1000
    assert ind.state()['text'] == 'BMW'                  # kept by default ...
    timed = Indicator(clock=clock, text_s=5)
    timed.on_text('Sale')
    clock.t += 6
    assert timed.state()['text'] == ''                   # ... or hidden after --text-s


def test_fake_car_publishes_the_advertisement(car):
    got = queue.Queue()
    client = FoxgloveClient(car.url, {'/advertisement'}, log=lambda m: None,
                            on_message=lambda t, p, c: got.put(codec.decode('std_msgs/String', p)['data']))
    client.start()
    try:
        assert client.connected.wait(5)
        for _ in range(50):                     # wait until the subscription is in place
            if client.subscriptions:
                break
            time.sleep(0.1)
        time.sleep(0.3)
        car.say('Hallo BITT')
        assert got.get(timeout=5) == 'Hallo BITT'
    finally:
        client.close()


PERSONS = json.dumps({'stamp': 1795224154.03, 'frame_id': 'base_link', 'persons': [
    {'x': 2.3, 'y': -1.1, 'distance': 1.4, 'angle_deg': -19.0, 'zone': 'right', 'method': 'ground', 'conf': 0.87,
     'box': [512, 20, 600, 300]},
    {'x': 3.0, 'y': 0.1, 'distance': 2.0, 'angle_deg': 2.0, 'zone': 'ahead', 'method': 'height', 'conf': 0.6,
     'box': [1, 2, 3, 4]}]})


def test_persons_are_read_and_forgotten_after_a_second():
    from projector_indicator.indicator import parse_persons
    assert parse_persons(PERSONS) == [{'x': 2.3, 'y': -1.1, 'zone': 'right', 'distance': 1.4, 'conf': 0.87},
                                      {'x': 3.0, 'y': 0.1, 'zone': 'ahead', 'distance': 2.0, 'conf': 0.6}]
    assert parse_persons('{"persons": []}') == []
    assert parse_persons('not json') is None and parse_persons('{"persons": [{"y": 1}]}') is None
    clock = Clock()
    ind = Indicator(clock=clock, persons_s=1.0)
    ind.on_persons(PERSONS)
    assert [p['zone'] for p in ind.state()['persons']] == ['right', 'ahead']
    ind.on_persons('garbage')                     # a broken message changes nothing ...
    assert len(ind.state()['persons']) == 2
    assert ind.state(connected=False)['persons'] == []
    clock.t += 1.5                                # ... and without new messages they are gone
    assert ind.state()['persons'] == []


def test_fake_car_publishes_persons(car):
    got = queue.Queue()
    client = FoxgloveClient(car.url, {'/persons'}, log=lambda m: None,
                            on_message=lambda t, p, c: got.put(json.loads(codec.decode('std_msgs/String', p)['data'])))
    client.start()
    try:
        assert client.connected.wait(5)
        for _ in range(50):
            if client.subscriptions:
                break
            time.sleep(0.1)
        time.sleep(0.3)
        assert car.show_person(2.5, 0.1) == 'ahead'
        msg = got.get(timeout=5)
        assert msg['frame_id'] == 'base_link' and msg['persons'][0]['x'] == 2.5 and msg['persons'][0]['zone'] == 'ahead'
    finally:
        client.close()


def test_person_positions_are_smoothed_and_keep_their_id():
    from projector_indicator.indicator import PersonTracks
    clock = Clock()
    tracks = PersonTracks(tau_s=0.35, clock=clock)
    tracks.update([{'x': 2.0, 'y': 0.0, 'zone': 'ahead', 'distance': 1.0, 'conf': 0.9}])
    first = tracks.current()[0]
    assert (first['x'], first['y'], first['seen']) == (2.0, 0.0, 1.0)
    clock.t += 0.1                                                  # a jump of 30 cm in one frame ...
    tracks.update([{'x': 2.3, 'y': 0.0, 'zone': 'ahead', 'distance': 1.3, 'conf': 0.9}])
    p = tracks.current()[0]
    assert p['id'] == first['id'] and 2.0 < p['x'] < 2.15          # ... moves it only part of the way
    for _ in range(20):                                             # standing there, it gets there
        clock.t += 0.1
        tracks.update([{'x': 2.3, 'y': 0.0, 'zone': 'ahead', 'distance': 1.3, 'conf': 0.9}])
    assert tracks.current()[0]['x'] == pytest.approx(2.3, abs=0.01)


def test_two_persons_stay_apart_and_a_lost_one_fades():
    from projector_indicator.indicator import PersonTracks
    clock = Clock()
    tracks = PersonTracks(hold_s=1.0, clock=clock)
    a = {'x': 2.0, 'y': 1.0, 'zone': 'left', 'distance': 1.0, 'conf': 0.9}
    b = {'x': 3.0, 'y': -1.0, 'zone': 'right', 'distance': 2.0, 'conf': 0.9}
    tracks.update([a, b])
    ids = {p['zone']: p['id'] for p in tracks.current()}
    clock.t += 0.1
    tracks.update([b, a])                                           # order does not matter
    assert {p['zone']: p['id'] for p in tracks.current()} == ids
    clock.t += 0.1
    tracks.update([a])                                              # b is no longer seen ...
    clock.t += 0.5
    faded = {p['zone']: p['seen'] for p in tracks.current()}
    assert faded['left'] > faded['right'] > 0                       # ... it fades
    clock.t += 0.6
    assert [p['zone'] for p in tracks.current()] == []              # and both are gone after hold_s
