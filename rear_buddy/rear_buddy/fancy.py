"""Which kind of car is behind us? A check takes (frame, detection) and returns (tier, brand):

    tier   'premium' -> job pitch, 'budget' -> "old is gold", None -> normal hello
    brand  what was recognised ('BMW', 'a small cheap city car', ...) or None

Checks (make_fancy_check):
    openai  (default) ask an OpenAI vision model (OPENAI_API_KEY, model REAR_BUDDY_VISION_MODEL, default
            gpt-4.1-mini). Needs internet (car Wi-Fi + phone tethering). Runs in the background, at most one
            request per second; until an answer comes the last one counts.
    clip    recognise the brand with CLIP (open_clip, ViT-L-14), zero-shot: no training, runs offline once the
            weights are downloaded (~1.7 GB, first run only). Brand lists: CAR_BRANDS in messages.py.
    always  every car is premium (demo the job pitch with any car photo)
    budget  every car is budget
    never   every car is an ordinary car

Only the closest vehicle is checked (pipeline.analyse), so the cost is one small image per frame.
"""
import base64
import json
import os
import pathlib
import threading
import time
import urllib.request

from .messages import BUDGET_DESCRIPTIONS, CAR_BRANDS

MIN_CROP_PX = 32  # boxes smaller than this are too far away to recognise a brand


class FixedTier:
    """Always the same answer, for testing and demos."""

    def __init__(self, tier):
        self.tier = tier

    def __call__(self, frame, detection):
        return self.tier, None


def labels_from(brands=None, descriptions=None):
    """[(label, tier, prompts)] for the brand lists: brands get several phrasings, descriptions one."""
    brands = CAR_BRANDS if brands is None else brands
    descriptions = BUDGET_DESCRIPTIONS if descriptions is None else descriptions
    labels = []
    for tier, names in brands.items():
        for name in names:
            labels.append((name, None if tier == 'other' else tier,
                           [f'a photo of a {name} car', f'a {name}', f'the front of a {name}']))
    labels += [(d, 'budget', [f'a photo of {d}']) for d in descriptions]
    return labels


def decide(probs, labels, min_conf=0.5):
    """(tier, brand) from per-label probabilities: the best label, unless the model is unsure."""
    best = max(range(len(labels)), key=lambda i: probs[i])
    if probs[best] < min_conf:
        return None, None
    return labels[best][1], labels[best][0]


class ClipBrandCheck:
    def __init__(self, model='ViT-L-14', pretrained='laion2b_s32b_b82k', min_conf=0.5, brands=None,
                 descriptions=None, device=None):
        import open_clip
        import torch
        self.torch = torch
        self.device = device or ('mps' if torch.backends.mps.is_available()
                                 else 'cuda' if torch.cuda.is_available() else 'cpu')
        # Downloads on the first run, afterwards from the local cache (also without internet, e.g. on the car's Wi-Fi).
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(model, pretrained=pretrained,
                                                                               device=self.device)
        self.model.eval()
        self.min_conf = min_conf
        self.labels = labels_from(brands, descriptions)
        tokenizer = open_clip.get_tokenizer(model)
        with torch.no_grad():
            per_label = []
            for _, _, prompts in self.labels:
                f = self.model.encode_text(tokenizer(prompts).to(self.device))
                per_label.append((f / f.norm(dim=-1, keepdim=True)).mean(0))
            text = torch.stack(per_label)
            self.text = text / text.norm(dim=-1, keepdim=True)

    def __call__(self, frame, detection):
        from PIL import Image
        import cv2
        car = crop(frame, detection)
        if min(car.shape[:2]) < MIN_CROP_PX:
            return None, None
        image = self.preprocess(Image.fromarray(cv2.cvtColor(car, cv2.COLOR_BGR2RGB))).unsqueeze(0).to(self.device)
        with self.torch.no_grad():
            f = self.model.encode_image(image)
            f = f / f.norm(dim=-1, keepdim=True)
            probs = (100 * f @ self.text.T).softmax(-1)[0].cpu().tolist()
        return decide(probs, self.labels, self.min_conf)


OPENAI_PROMPT = (
    'You see the closest car behind our car, cut out of a rear camera picture. Which of these is it? '
    'Choose exactly one option or null if you cannot tell (too small, blurry, not a car, brand not visible and '
    'not recognisable from the shape). Options: {options}. '
    'Answer only JSON: {{"brand": "<one option or null>", "confidence": <0..1>}}'
)


def api_key():
    """OPENAI_API_KEY from the environment, else from a .env file in the working directory or next to the package."""
    if os.environ.get('OPENAI_API_KEY'):
        return os.environ['OPENAI_API_KEY']
    for env in (pathlib.Path.cwd() / '.env', pathlib.Path(__file__).resolve().parent.parent / '.env'):
        if env.is_file():
            for line in env.read_text().splitlines():
                key, _, value = line.partition('=')
                if key.strip() == 'OPENAI_API_KEY' and value.strip():
                    return value.strip().strip('"\'')
    return None


def parse_answer(content, labels, min_conf=0.5):
    """(tier, brand) from the model's JSON; None, None for null, unknown options or low confidence."""
    try:
        answer = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None, None
    brand, conf = answer.get('brand'), answer.get('confidence', 0)
    tiers = {name.lower(): (name, tier) for name, tier, _ in labels}
    if not isinstance(brand, str) or brand.lower() not in tiers or not isinstance(conf, (int, float)) \
            or conf < min_conf:
        return None, None
    name, tier = tiers[brand.lower()]
    return tier, name


class BackgroundVision:
    """Asks an OpenAI vision model about a box of the frame in the background, so detection never waits: at most one
    request per interval_s, and until an answer comes the last one counts (for keep_s). Subclasses: prompt, parse."""

    URL = 'https://api.openai.com/v1/chat/completions'
    name = 'vision check'
    prompt = ''

    def __init__(self, key, model=None, interval_s=1.0, keep_s=6.0, timeout_s=10.0, log=print):
        self.key, self.model = key, model or os.environ.get('REAR_BUDDY_VISION_MODEL', 'gpt-4.1-mini')
        self.interval_s, self.keep_s, self.timeout_s, self.log = interval_s, keep_s, timeout_s, log
        self._latest = None              # jpeg bytes waiting to be asked
        self._answer = (self.nothing(), 0.0)
        self._wake = threading.Condition()
        self._last_error = None
        threading.Thread(target=self._worker, name=self.name, daemon=True).start()

    def nothing(self):
        return None

    def parse(self, content):
        raise NotImplementedError

    def __call__(self, frame, detection):
        import cv2
        box = crop(frame, detection)
        if min(box.shape[:2]) < MIN_CROP_PX:
            return self.nothing()
        scale = 512 / max(box.shape[:2])
        if scale < 1:
            box = cv2.resize(box, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode('.jpg', box, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            with self._wake:
                self._latest = jpeg.tobytes()
                self._wake.notify()
        answer, at = self._answer
        return answer if time.monotonic() - at < self.keep_s else self.nothing()

    def _worker(self):
        while True:
            with self._wake:
                while self._latest is None:
                    self._wake.wait()
                jpeg, self._latest = self._latest, None
            started = time.monotonic()
            try:
                self._answer = (self._ask(jpeg), time.monotonic())
                self._last_error = None
            except Exception as e:  # noqa: BLE001 - network or API trouble: keep the last answer, report once
                error = str(e) or type(e).__name__
                if error != self._last_error:
                    self.log(f'{self.name} (OpenAI): {error}')
                self._last_error = error
            time.sleep(max(0.0, self.interval_s - (time.monotonic() - started)))

    def _ask(self, jpeg):
        image = 'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()
        body = {
            'model': self.model, 'temperature': 0, 'max_tokens': 40,
            'response_format': {'type': 'json_object'},
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': self.prompt},
                {'type': 'image_url', 'image_url': {'url': image, 'detail': 'low'}}]}],
        }
        request = urllib.request.Request(self.URL, data=json.dumps(body).encode(), headers={
            'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            content = json.loads(response.read())['choices'][0]['message']['content']
        return self.parse(content)


class OpenAIBrandCheck(BackgroundVision):
    """Brand of the closest car -> (tier, brand)."""

    name = 'car brand check'

    def __init__(self, key, model=None, min_conf=0.5, brands=None, descriptions=None, **kw):
        self.min_conf = min_conf
        self.labels = labels_from(brands, descriptions)
        self.prompt = OPENAI_PROMPT.format(options=', '.join(name for name, _, _ in self.labels))
        super().__init__(key, model, **kw)

    def nothing(self):
        return None, None

    def parse(self, content):
        return parse_answer(content, self.labels, self.min_conf)


# Demo gags (on by default; --no-suit-pitch / --no-flirt-women): how the person behind the car comes across in the
# picture. One request answers both questions; nothing is stored.
PERSON_PROMPT = (
    'Demo of a fun car display. The picture shows one person standing behind our car. Judge only what is visible. '
    'suit: is the person wearing a business suit or blazer with formal clothes? '
    'woman: based only on how the person presents visually, does the person appear to be a woman? '
    'Use null when you cannot tell. '
    'Answer only JSON: {"suit": true | false | null, "woman": true | false | null, "confidence": <0..1>}'
)


def parse_person(content, min_conf=0.7, suit=True, woman=True):
    """'suit' or 'woman' for a confident yes (suit first: the job pitch wins), else None (no, unsure, refusal)."""
    try:
        answer = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    conf = answer.get('confidence', 0)
    if not isinstance(conf, (int, float)) or conf < min_conf:
        return None
    if suit and answer.get('suit') is True:
        return 'suit'
    if woman and answer.get('woman') is True:
        return 'woman'
    return None


class OpenAIPersonCheck(BackgroundVision):
    """The closest person on foot -> 'suit', 'woman' or None (demo gags)."""

    name = 'person check'
    prompt = PERSON_PROMPT

    def __init__(self, key, model=None, min_conf=0.7, keep_s=3.0, suit=True, woman=True, **kw):
        self.min_conf, self.suit, self.woman = min_conf, suit, woman
        super().__init__(key, model, keep_s=keep_s, **kw)

    def parse(self, content):
        return parse_person(content, self.min_conf, self.suit, self.woman)


def make_person_check(suit=True, woman=True, log=print):
    """For the suit pitch / flirt mode; None (both off) when neither is wanted or there is no API key."""
    if not (suit or woman):
        return None
    key = api_key()
    if not key:
        log('suit pitch / flirt mode need OPENAI_API_KEY (environment or .env): switched off')
        return None
    check = OpenAIPersonCheck(key, suit=suit, woman=woman, log=log)
    log('person check (OpenAI %s): %s' % (check.model, ', '.join(
        n for n, on in (('suit -> job pitch', suit), ('woman -> flirt card', woman)) if on)))
    return check


def make_fancy_check(name, log=print):
    if name == 'openai':
        key = api_key()
        if not key:
            log('car brand check unavailable: no OPENAI_API_KEY (environment or .env); every car gets the normal '
                'hello. --fancy clip works offline')
            return FixedTier(None)
        check = OpenAIBrandCheck(key, log=log)
        log(f'car brand check: OpenAI {check.model}')
        return check
    if name == 'clip':
        try:
            log('loading the car brand model (CLIP ViT-L-14) ...')
            return ClipBrandCheck()
        except Exception as e:  # noqa: BLE001 - missing package or weights: keep going without brands
            log(f'car brand check unavailable ({e}); every car gets the normal hello. pip install open_clip_torch')
            return FixedTier(None)
    fixed = {'always': 'premium', 'budget': 'budget', 'never': None}
    if name not in fixed:
        raise ValueError(f'unknown car check {name!r}, choose from openai, clip, {", ".join(fixed)}')
    return FixedTier(fixed[name])


def crop(frame, detection):
    """The detection's box cut out of the frame, for checks that look at the car itself."""
    x1, y1, x2, y2 = (int(v) for v in detection.box)
    return frame[max(y1, 0):y2, max(x1, 0):x2]
