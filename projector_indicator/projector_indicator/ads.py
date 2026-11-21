"""What the projection shows for an advertisement message: brand, destination, logo and a made-up navigation hint.

    advert('Porsche', LOGOS)   # {'title': 'Porsche Zentrum', 'turn': 'right', 'distance': '100 m', 'logo': ..., ...}

A message naming a known brand gets that brand's card; any other text becomes its own destination, with a direction
and distance made up from the text (always the same for the same text, so it does not jump around).
Logos: put the brand's official logo file into projector_indicator/logos/ (<brand>.svg or .png, e.g. porsche.svg,
mcdonalds.svg; brands offer them in their press areas). That folder is git-ignored: logo files are the brands'
trademarks, keep them out of the repository. Without a file the card shows a plain badge in the brand's colours.
"""
import hashlib
import pathlib

LOGOS = pathlib.Path(__file__).with_name('logos')
LOGO_TYPES = ('.svg', '.png', '.webp', '.jpg')
TURNS = ('right', 'left', 'slight-right', 'slight-left', 'straight')

BRANDS = [
    {'brand': 'porsche', 'keys': ('porsche',), 'title': 'Porsche Zentrum', 'initials': 'P',
     'color': '#1a1a1a', 'accent': '#c8a45a', 'turn': 'right', 'distance_m': 100},
    {'brand': 'mcdonalds', 'keys': ('mcdonald', 'mc donald', "mcdonald's", 'mcdo'), 'title': "McDonald's",
     'initials': 'M', 'color': '#da291c', 'accent': '#ffc72c', 'turn': 'left', 'distance_m': 250},
    {'brand': 'bayreuther', 'keys': ('bayreuther',), 'title': 'Bayreuther Brauhaus', 'initials': 'B',
     'color': '#0d2c6c', 'accent': '#ffffff', 'turn': 'straight', 'distance_m': 300},
    {'brand': 'finanzberatung', 'keys': ('finanzberatung', 'finanz'), 'title': 'Finanzberatung', 'initials': '₿',
     'color': '#f7931a', 'accent': '#ffffff', 'turn': 'slight-right', 'distance_m': 450, 'logo': 'bitcoin'},
]
PALETTE = [('#1565c0', '#ffffff'), ('#2e7d32', '#ffffff'), ('#6a1b9a', '#ffffff'), ('#ef6c00', '#ffffff'),
           ('#00838f', '#ffffff'), ('#ad1457', '#ffffff')]


def distance_text(metres):
    if metres < 1000:
        return f'{int(round(metres / 10) * 10)} m'
    return f'{metres / 1000:.1f} km'.replace('.', ',')


def logo_url(brand, logos=LOGOS):
    for ext in LOGO_TYPES:
        if (pathlib.Path(logos) / f'{brand}{ext}').is_file():
            return f'/assets/{brand}{ext}'
    return None


def find_brand(text):
    t = text.lower()
    return next((b for b in BRANDS if any(k in t for k in b['keys'])), None)


def slug(text):
    return ''.join(c for c in text.lower() if c.isalnum())[:40] or 'ad'


def advert(text, logos=LOGOS):
    """The card for one advertisement message, or None for an empty one."""
    text = (text or '').strip()
    if not text:
        return None
    brand = find_brand(text)
    if brand:
        card = {k: brand[k] for k in ('brand', 'title', 'initials', 'color', 'accent', 'turn')}
        card['logo_file'] = brand.get('logo', brand['brand'])   # a brand may show another logo (bitcoin)
        metres = brand['distance_m']
    else:
        h = int.from_bytes(hashlib.sha256(text.lower().encode()).digest()[:4], 'big')
        color, accent = PALETTE[h % len(PALETTE)]
        words = [w for w in text.split() if w[:1].isalnum()]
        card = {'brand': slug(text), 'title': text, 'initials': ''.join(w[0] for w in words[:2]).upper() or '?',
                'color': color, 'accent': accent, 'turn': TURNS[(h >> 8) % len(TURNS)]}
        metres = 50 * (1 + (h >> 16) % 16)  # 50 .. 800 m
    card['distance'] = distance_text(metres)
    card['logo'] = logo_url(card.pop('logo_file', card['brand']), logos)
    return card
