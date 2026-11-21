"""Tests for the ground: base_link <-> light patch, the car path, and the whole chain with the fake car.
python3 -m pytest test/"""
import json
import subprocess
import sys
import time
import urllib.request

import pytest

from projector_indicator.fake_car import FakeCar
from projector_indicator.ground import Ground, corridor_contains

# a light patch like the measured one: 0.4 m wide at the car, 1.6 m wide 4.3 m ahead (world mm, y towards the car)
CAL = {'footprint_mm': [[1600, 0], [1000, 4300], [600, 4300], [0, 0]]}



def test_base_link_and_world_are_inverse_and_the_car_is_at_the_near_edge():
    g = Ground(CAL, start_m=1.05)
    assert g.to_world(1.05, 0.0) == (800, 4300)            # middle of the near edge
    assert g.to_world(2.05, 0.5) == (300, 3300)            # 1 m ahead, 0.5 m to the left
    assert g.to_base(*g.to_world(3.2, -0.4)) == pytest.approx((3.2, -0.4))
    assert g.contains(800, 4000) and not g.contains(800, 4400) and not g.contains(100, 4000)
    assert g.contains(800, 2000, margin=300) and not g.contains(400, 2000, margin=300)


@pytest.mark.parametrize('x, y, steer, inside', [
    (3.0, 0.0, 0.0, True), (3.0, 0.37, 0.0, True), (3.0, 0.45, 0.0, False),   # straight: as wide as the car
    (0.8, 0.0, 0.0, False),                                                  # behind the bumper
    (2.0, 1.3, 0.35, True), (2.0, -1.3, -0.35, True),                        # on the circle of a full left / right turn
    (3.0, 0.0, 0.35, False), (2.0, 1.3, -0.35, False),                       # turning away from it
])
def test_corridor(x, y, steer, inside):
    assert corridor_contains(x, y, steer) is inside


def test_a_person_in_the_path_turns_it_red(tmp_path):
    """The whole chain: fake car (steering, /persons) -> indicator -> the path in the page's state."""
    car = FakeCar(port=0, log=lambda m: None)
    cal = tmp_path / 'cal.json'
    cal.write_text(json.dumps({'world_to_screen': [1, 0, 0, 0, 1, 0, 0, 0, 1], 'size_mm': [1600, 4300],
                               'footprint_mm': CAL['footprint_mm'], 'floor_mm': CAL['footprint_mm'],
                               'box_mm': [500, 2000, 600, 240]}))
    ind = subprocess.Popen([sys.executable, '-u', '-m', 'projector_indicator.indicator', '--url', car.url, '--port', '0',
                            '--no-browser', '--calibration', str(cal)], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
    try:
        line = ''
        while 'projection page' not in line:
            line = ind.stdout.readline()
            assert line, 'the indicator stopped'
        port = int(line.split('127.0.0.1:')[1].split('/')[0])

        def state():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/state', timeout=3) as r:
                return json.loads(r.read())

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not state()['live']:
            time.sleep(0.2)
        car.steer_rad = 0.0
        for _ in range(10):                                  # beside the path: it stays white
            car.show_person(3.0, 0.9)
            time.sleep(0.05)
        s = state()
        assert s['corridor'] == {'steer_rad': 0.0, 'blocked': False} and 'buttons' not in s
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not state()['corridor']['blocked']:
            car.show_person(3.0, 0.1)                        # in it: red
            time.sleep(0.05)
        assert state()['corridor']['blocked']
    finally:
        ind.terminate()
        ind.communicate(timeout=10)
        car.close()
