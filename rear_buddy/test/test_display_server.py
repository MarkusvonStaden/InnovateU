"""Tests for the rear display web server: python3 -m pytest test/"""
import json
import urllib.request

import pytest

from rear_buddy.display_server import DisplayServer


@pytest.fixture
def server():
    s = DisplayServer(host='127.0.0.1', port=0)
    yield s
    s.close()


def get(server, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{server.port}{path}', timeout=5) as r:
        return r.status, r.headers['Content-Type'], r.read()


def test_page_is_served(server):
    status, ctype, body = get(server, '/')
    assert status == 200 and ctype.startswith('text/html') and b'EventSource' in body


def test_state_follows_show(server):
    assert json.loads(get(server, '/state')[2])['scene'] == 'nobody'
    server.show({'scene': 'cyclist_flirt', 'text': 'Coffee?', 'animation': 'hearts', 'extra': 'ignored'})
    assert json.loads(get(server, '/state')[2]) == {'scene': 'cyclist_flirt', 'text': 'Coffee?', 'animation': 'hearts', 'qr': False,
                                                    'ad': None}


def test_events_stream_current_then_new_messages(server):
    with urllib.request.urlopen(f'http://127.0.0.1:{server.port}/events', timeout=5) as stream:
        assert stream.headers['Content-Type'] == 'text/event-stream'

        def next_event():
            line = stream.readline()
            assert stream.readline() == b'\n'
            return json.loads(line.removeprefix(b'data: '))

        assert next_event()['scene'] == 'nobody'
        server.show({'scene': 'car', 'text': 'Hello!', 'animation': 'breathing'})
        assert next_event() == {'scene': 'car', 'text': 'Hello!', 'animation': 'breathing', 'qr': False, 'ad': None}


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(server, '/nope')
    assert err.value.code == 404


def test_qr_code_only_on_its_scenes():
    s = DisplayServer(host='127.0.0.1', port=0, qr_url='https://www.linkedin.com/in/someone', qr_scenes={'fancy_car'})
    try:
        status, ctype, body = get(s, '/qr.svg')
        assert status == 200 and ctype == 'image/svg+xml' and b'viewBox' in body
        s.show({'scene': 'fancy_car', 'text': 'Hire me', 'animation': 'gold_shine'})
        assert json.loads(get(s, '/state')[2])['qr'] is True
        s.show({'scene': 'car', 'text': 'Hi', 'animation': 'breathing'})
        assert json.loads(get(s, '/state')[2])['qr'] is False
    finally:
        s.close()


def test_no_qr_code_without_url(server):
    server.show({'scene': 'fancy_car', 'text': 'Hire me', 'animation': 'gold_shine'})
    assert json.loads(get(server, '/state')[2])['qr'] is False
    with pytest.raises(urllib.error.HTTPError):
        get(server, '/qr.svg')


def test_pitch_page_details_and_photo(tmp_path):
    import json as _json
    import urllib.request as _url

    photo = tmp_path / 'me.jpg'
    photo.write_bytes(b'\xff\xd8fake')
    card = {'greeting': 'Hi, ich bin', 'first_name': 'Sascha', 'age': '29', 'role': 'Softwareentwickler',
            'skills': ['Python'], 'photo': str(photo)}
    s = DisplayServer(host='127.0.0.1', port=0, cards={'fancy_car': card, 'person_flirt': {'first_name': 'Markus'}})
    try:
        base = f'http://127.0.0.1:{s.port}'
        details = _json.loads(_url.urlopen(base + '/pitch.json').read())
        assert details['fancy_car']['first_name'] == 'Sascha' and details['fancy_car']['photo'] is True
        assert details['person_flirt'] == {'first_name': 'Markus', 'photo': False}
        assert _url.urlopen(base + '/photo/fancy_car').read() == b'\xff\xd8fake'
        assert b'fancy_car' in _url.urlopen(base + '/pitch').read()
    finally:
        s.close()


def test_pitch_without_photo_shows_initials_flag():
    s = DisplayServer(host='127.0.0.1', port=0, cards={'fancy_car': {'first_name': 'Sascha', 'photo': 'nope.jpg'}})
    try:
        assert s.cards['fancy_car']['photo'] is False and s.photos['fancy_car'] is None
    finally:
        s.close()


def test_advertisement_card_and_logo():
    import json as _json
    import time as _time
    import urllib.request as _url
    from rear_buddy import ads
    s = DisplayServer(host='127.0.0.1', port=0, assets=ads.LOGOS)
    try:
        s.show({'scene': 'car', 'text': 'hi', 'animation': 'breathing'})
        s.set_ad(ads.advert('Starbucks'), hold_s=0.2)
        assert s.state['ad']['title'] == 'Starbucks Coffee' and s.state['scene'] == 'car'
        s.show({'scene': 'nobody', 'text': '', 'animation': 'scanner'})   # a new scene keeps the ad
        assert s.state['ad']['brand'] == 'starbucks'
        base = f'http://127.0.0.1:{s.port}'
        assert _url.urlopen(base + s.state['ad']['logo']).read().startswith(b'<?xml')
        try:
            _url.urlopen(base + '/assets/..%2Fads.py')
            assert False, 'must not serve files outside the logos folder'
        except Exception:
            pass
        _time.sleep(0.4)
        assert s.state['ad'] is None    # gone after hold_s
        assert _json.loads(_url.urlopen(base + '/state').read())['ad'] is None
    finally:
        s.close()
