"""Side events from what the driver said, detected in parallel to the speech so they never slow it down.

- The driver insults someone          -> console FCK_PROJECTION, publish the swear words on the swearword topic
- Talk that fits an ad category        -> publish the advertiser (see ADS) on the advertisement topic
- "Pacman-Modus an" / "aus"            -> publish enable / disable on the pacman topic
"""

import json
import sys
import urllib.request

# Ad category -> value published on the advertisement topic. The descriptions go into the classifier prompt.
ADS = {
    "food": ("McDonalds", 'hunger, food, eating, snacks, restaurants ("Ich habe Hunger", "Wo gibt\'s hier Pizza?")'),
    "beer": ("Bayreuther", 'beer, wanting a beer, beer garden, "Feierabendbier" ("Jetzt ein kühles Bier!")'),
    "coffee": ("Starbucks", 'coffee, wanting a coffee, café, latte, cappuccino, espresso, being tired and needing '
                            'caffeine ("Ich brauch jetzt einen Kaffee", "Lust auf einen Latte?")'),
    "finance": ("Finanzberatung", "money, saving, debts, loans, rent, salary, pension, stocks, investing, "
                                  '"zu teuer" ("Das Benzin ist so teuer", "Ich bin pleite")'),
    "slow_car": ("Porsche", "the driver's OWN car is too slow, lacks power, wants to drive faster or overtake but "
                            'cannot ("Die Karre zieht nicht", "Mehr PS bräuchte man"). NOT when someone ELSE is slow '
                            '("Der vor mir schleicht", "Fahr schneller, du Schnecke")'),
}
URL = "https://api.openai.com/v1/chat/completions"

PROMPT = """\
You classify one sentence a car driver just said (transcribed speech, mostly German). Answer with JSON only:
{{"insult": bool, "swearwords": [str], "ads": [str], "pacman": "enable" | "disable" | null}}

insult: the driver insults, curses at or swears at another person (another driver, cyclist, pedestrian, passenger),
  e.g. "du Penner", "Idiot", "Arsch", "Trottel", "Vollpfosten", "fahr doch, du Schnecke". Mild everyday swearing that
  insults nobody ("Mist", "verdammt") is NOT an insult.
swearwords: the insulting/swear words exactly as said, empty if none.
ads: every category below the sentence is about, empty if none. Only these keys:
{categories}
pacman: someone wants the Pacman game/mode on ("enable") or off ("disable"). enable: "Pacman-Modus an", "Starte
  Pacman", "Aktiviere den Pacman Modus", "Ich will Pacman spielen", "Lass uns Pacman spielen", "Pacman bitte",
  "Zeit für Pacman". disable: "Pacman aus", "Beende den Pacman-Modus", "Schluss mit Pacman", "Genug Pacman gespielt",
  "Ich will nicht mehr Pacman spielen". Also when the transcription mangles the word ("Pac-Man", "Packman", "Pekmen",
  "Pacmen"). null when Pacman is only talked about without wanting it on or off ("Pacman war früher mein
  Lieblingsspiel", "Kennst du Pacman?"), or not mentioned at all.
"""


def prompt() -> str:
    categories = "\n".join(f"  {key}: {desc}" for key, (_, desc) in ADS.items())
    return PROMPT.format(categories=categories)


def classify(text: str, api_key: str, model: str, timeout_s: float = 10.0) -> dict:
    """Blocking; run it in a thread. Returns {"insult": bool, "swearwords": [...], "ads": [...]}."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": prompt()}, {"role": "user", "content": text}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={"Authorization": f"Bearer {api_key}",
                                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        content = json.load(resp)["choices"][0]["message"]["content"]
    return parse(content)


def parse(content: str) -> dict:
    data = json.loads(content)
    return {
        "insult": bool(data.get("insult")),
        "swearwords": [str(w) for w in data.get("swearwords") or []],
        "ads": [a for a in ADS if a in (data.get("ads") or [])],  # known keys only, fixed order
        "pacman": data.get("pacman") if data.get("pacman") in ("enable", "disable") else None,
    }


def fck_projection() -> str:
    text = "FCK_PROJECTION"
    return f"\033[1;31m{text}\033[0m" if sys.stdout.isatty() else text


class Events:
    """Turns a classification into console output and topic messages (publish may be None: console only)."""

    def __init__(self, publish=None, swear_topic="/Swearword", ad_topic="/advertisement", log=print,
                 pacman_topic="/pacman"):
        self.publish, self.swear_topic, self.ad_topic, self.log = publish, swear_topic, ad_topic, log
        self.pacman_topic = pacman_topic

    def messages(self, text: str, result: dict) -> list[tuple[str, str]]:
        """(topic, value) pairs for one classified sentence."""
        out = []
        if result["insult"]:
            out.append((self.swear_topic, ", ".join(result["swearwords"]) or text))
        for ad in result["ads"]:
            out.append((self.ad_topic, ADS[ad][0]))
        if result.get("pacman"):
            out.append((self.pacman_topic, result["pacman"]))
        return out

    def handle(self, text: str, result: dict) -> None:
        for topic, value in self.messages(text, result):
            if topic == self.swear_topic:
                self.log(f"{fck_projection()}  ({value})")
            elif topic == self.pacman_topic:
                self.log(f"👾 Pacman {'on' if value == 'enable' else 'off'}")
            else:
                self.log(f"📢 {value}")
            if self.publish:
                sent = self.publish(topic, value)
                self.log(f"   -> {topic}: {value!r}" + ("" if sent else "  (not sent, bridge not reachable)"))
