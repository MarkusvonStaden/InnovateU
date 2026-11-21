"""Tests for the dot game switch (/pacman): enable / disable, and the whole chain with the fake car."""
import json
import subprocess
import sys
import time
import urllib.request

import pytest

from projector_indicator.fake_car import PACMAN_CHANNEL, FakeCar
from projector_indicator.indicator import parse_switch


@pytest.mark.parametrize('text, on', [
    ('enable', True), (' Enable\n', True), ('on', True), ('true', True), ('1', True), ('start', True),
    ('disable', False), ('OFF', False), ('false', False), ('0', False), ('stop', False),
    ('maybe', None), ('', None),
])
def test_parse_switch(text, on):
    assert parse_switch(text) is on


def test_the_game_switches_on_and_off_from_the_topic():
    car = FakeCar(port=0, log=lambda m: None)
    ind = subprocess.Popen([sys.executable, '-u', '-m', 'projector_indicator.indicator', '--url', car.url, '--port', '0',
                            '--no-browser', '--no-calibration'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    try:
        line = ''
        while 'projection page' not in line:
            line = ind.stdout.readline()
            assert line, 'the indicator stopped'
        port = int(line.split('127.0.0.1:')[1].split('/')[0])

        def state():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/state', timeout=3) as r:
                return json.loads(r.read())

        def wait_for(on):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and state().get('pacman') is not on:
                time.sleep(0.1)
            return state().get('pacman')

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not state()['live']:
            time.sleep(0.2)
        assert state()['pacman'] is False
        time.sleep(0.5)                                      # subscribed to /pacman by now
        car.say('enable', channel=PACMAN_CHANNEL)
        assert wait_for(True) is True
        car.say('nonsense', channel=PACMAN_CHANNEL)          # ignored
        time.sleep(0.3)
        assert state()['pacman'] is True
        car.say('disable', channel=PACMAN_CHANNEL)
        assert wait_for(False) is False
    finally:
        ind.terminate()
        ind.communicate(timeout=10)
        car.close()
