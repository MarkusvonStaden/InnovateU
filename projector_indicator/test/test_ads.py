"""Tests for the advertisement cards and the logo files: python3 -m pytest test/"""
import json
import urllib.error
import urllib.request

import pytest

from projector_indicator.ads import TURNS, advert
from projector_indicator.display_server import DisplayServer
from projector_indicator.indicator import PAGE


def test_known_brands_get_their_card(tmp_path):
    porsche = advert('Porsche', tmp_path)
    assert (porsche['title'], porsche['turn'], porsche['distance']) == ('Porsche Zentrum', 'right', '100 m')
    assert porsche['logo'] is None and porsche['initials'] == 'P'
    assert advert("Hunger? McDonald's!", tmp_path)['brand'] == 'mcdonalds'
    assert advert('McDonalds', tmp_path)['brand'] == 'mcdonalds'        # as it comes on the topic
    bayreuther = advert('Bayreuther', tmp_path)
    assert (bayreuther['title'], bayreuther['turn'], bayreuther['distance']) == ('Bayreuther Brauhaus', 'straight', '300 m')


def test_a_logo_file_is_used_when_there_is_one(tmp_path):
    (tmp_path / 'porsche.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert advert('porsche', tmp_path)['logo'] == '/assets/porsche.svg'


def test_other_texts_get_a_made_up_but_steady_direction(tmp_path):
    a, b = advert('Eisdiele Venezia', tmp_path), advert('Eisdiele Venezia', tmp_path)
    assert a == b and a['title'] == 'Eisdiele Venezia' and a['initials'] == 'EV' and a['turn'] in TURNS
    assert a['distance'].endswith(' m')
    assert advert('', tmp_path) is None and advert('   ', tmp_path) is None


def test_long_distances_in_km():
    from projector_indicator.ads import distance_text
    assert distance_text(1250) == '1,2 km' and distance_text(95) == '100 m'


@pytest.fixture
def server(tmp_path):
    (tmp_path / 'porsche.svg').write_bytes(b'<svg/>')
    (tmp_path / 'secret.txt').write_text('no')
    s = DisplayServer(host='127.0.0.1', port=0, page=PAGE, state={}, assets=tmp_path)
    yield s
    s.close()


def test_logos_are_served_and_nothing_else(server):
    with urllib.request.urlopen(f'http://127.0.0.1:{server.port}/assets/porsche.svg', timeout=5) as r:
        assert r.headers['Content-Type'] == 'image/svg+xml' and r.read() == b'<svg/>'
    for bad in ('secret.txt', '..%2Fsecret.txt', '../secret.txt', 'C:porsche.svg', 'missing.svg', '.hidden.svg'):
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f'http://127.0.0.1:{server.port}/assets/{bad}', timeout=5)


def test_finanzberatung_shows_the_bitcoin_logo(tmp_path):
    assert advert('Finanzberatung', tmp_path)['logo'] is None
    (tmp_path / 'bitcoin.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    card = advert('Finanzberatung', tmp_path)
    assert card['logo'] == '/assets/bitcoin.svg' and card['title'] == 'Finanzberatung' and 'logo_file' not in card
