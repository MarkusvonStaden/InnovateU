import base64
import time

from bmw_to_vw_speech import realtime
from bmw_to_vw_speech.audio import PcmBuffer
from bmw_to_vw_speech.prompt import instructions


def test_session_update_is_audio_only_with_fast_vad():
    s = realtime.session_update("x", silence_ms=300)["session"]
    assert s["type"] == "realtime"
    assert s["output_modalities"] == ["audio"]
    vad = s["audio"]["input"]["turn_detection"]
    assert vad["type"] == "server_vad" and vad["silence_duration_ms"] == 300 and vad["create_response"]
    assert s["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}


def test_session_update_options():
    s = realtime.session_update("x", transcribe=False, noise_reduction="off")["session"]
    assert "transcription" not in s["audio"]["input"]
    assert "noise_reduction" not in s["audio"]["input"]


def test_append_audio_is_base64():
    ev = realtime.append_audio(b"\x01\x02")
    assert base64.b64decode(ev["audio"]) == b"\x01\x02"


def test_say_text_creates_item_then_response():
    events = realtime.say_text("Fahr endlich!")
    assert events[0]["item"]["content"][0]["text"] == "Fahr endlich!"
    assert events[1]["type"] == "response.create"


def test_history_keeps_only_the_last_turn():
    h = realtime.History(keep=2)
    for i in "abcd":
        h.added(i)
    h.added("d")  # duplicates are ignored
    assert [e["item_id"] for e in h.prune()] == ["a", "b"]
    assert h.prune() == []
    assert h.ids == ["c", "d"]


def test_instructions_mention_language():
    assert "French" in instructions("French")


def test_pcm_buffer_pads_and_tracks_busy():
    b = PcmBuffer()
    assert not b.busy()
    b.write(b"\x01\x02\x03")
    assert b.busy()
    assert b.read(4) == b"\x01\x02\x03\x00"
    assert b.busy(tail_s=10)  # just played, the echo still rings
    time.sleep(0.02)
    assert not b.busy(tail_s=0.01)
    b.write(b"\x05")
    b.clear()
    assert b.read(2) == b"\x00\x00"


def test_say_audio_appends_commits_and_responds():
    events = realtime.say_audio(b"\x00" * 70000, chunk=32000)
    assert [e["type"] for e in events] == ["input_audio_buffer.append"] * 3 + ["input_audio_buffer.commit",
                                                                              "response.create"]


def test_one_shot_session_has_no_vad():
    assert realtime.session_update("x", vad=False)["session"]["audio"]["input"]["turn_detection"] is None


def test_outside_only_adds_silent_tool_and_rules():
    from bmw_to_vw_speech.prompt import SILENT_TOOL, silent_tool
    assert SILENT_TOOL in instructions(front_only=True)
    assert SILENT_TOOL not in instructions(front_only=False)
    s = realtime.session_update("x", tools=[silent_tool()])["session"]
    assert s["tools"][0]["name"] == SILENT_TOOL and s["tool_choice"] == "auto"
    assert "tools" not in realtime.session_update("x")["session"]


def test_history_prune_all_after_silent_turn():
    h = realtime.History(keep=2)
    for i in "abc":
        h.added(i)
    assert len(h.prune(keep=0)) == 3 and h.ids == []


def test_encode_string_is_cdr():
    from bmw_to_vw_speech.foxglove import encode_string
    assert encode_string("Mc") == b"\x00\x01\x00\x00" + b"\x03\x00\x00\x00" + b"Mc\x00"


def test_events_map_to_topics():
    from bmw_to_vw_speech import events
    e = events.Events()
    assert events.parse('{"insult": true, "swearwords": ["Penner"], "ads": ["beer", "bogus", "food"]}') == {
        "insult": True, "swearwords": ["Penner"], "ads": ["food", "beer"]}
    assert e.messages("Fahr, du Penner", {"insult": True, "swearwords": ["Penner"], "ads": []}) == [
        ("/Swearword", "Penner")]
    ads = ["food", "beer", "finance", "slow_car"]
    assert [v for _, v in e.messages("x", {"insult": False, "swearwords": [], "ads": ads})] == [
        "McDonalds", "Bayreuther", "Finanzberatung", "Porsche"]
    assert e.messages("Hallo", {"insult": False, "swearwords": [], "ads": []}) == []
    assert all(key in events.prompt() for key in ads)


def test_events_publish_and_print():
    from bmw_to_vw_speech import events
    sent, logged = [], []
    e = events.Events(publish=lambda t, v: sent.append((t, v)) or True, log=logged.append)
    e.handle("Du Idiot, ich hab Hunger", {"insult": True, "swearwords": ["Idiot"], "ads": ["food"]})
    assert sent == [("/Swearword", "Idiot"), ("/advertisement", "McDonalds")]
    assert any("FCK_PROJECTION" in line for line in logged)
