"""LLM narration (#12): payload trimming, response parsing, and the
no-key/no-labels guards. The Claude call is stubbed — no network."""

import base64
import io
import json
import types

from PIL import Image

from app import narrate

RESULT = {
    "labels": [
        {"name": "Jupiter", "x": 50.0, "y": 60.0, "mag": -2.5,
         "kind": "planet", "status": "projected"},
        {"name": "Moon", "x": 70.0, "y": 80.0, "mag": -12.0, "kind": "moon",
         "status": "projected", "phase": 0.42},
        {"name": "Sirius", "x": 10.0, "y": 20.0, "mag": -1.44,
         "kind": "star", "status": "matched"},
        {"name": "Andromeda Galaxy (M31)", "x": 30.0, "y": 40.0, "mag": 3.6,
         "kind": "dso", "dso_type": "Gxy", "status": "hidden",
         "radius_px": 52.1},
    ],
    "constellations": [{"name": "Orion", "abbr": "Ori", "segments": []}],
    "ephemeris": {"time_utc": "2026-08-13T04:16:00Z"},
    "verification": {"verified": True},
    "satellites": {"crossings": [
        {"name": "Iss (Zarya)", "norad_id": "25544",
         "points": [[1.0, 2.0], [3.0, 4.0]], "t_enter_s": 0.0,
         "t_exit_s": 16.0}]},
}

REPLY = {"caption": "Jupiter and the Moon over Orion",
         "text": "Your photo caught Jupiter beside a crescent Moon."}


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request, returns a
    canned structured-output response."""

    def __init__(self, reply=REPLY, stop_reason="end_turn"):
        self.calls = []

        def create(**kwargs):
            self.calls.append(kwargs)
            block = types.SimpleNamespace(type="text", text=json.dumps(reply))
            return types.SimpleNamespace(content=[block],
                                         stop_reason=stop_reason)

        self.messages = types.SimpleNamespace(create=create)


def test_returns_caption_and_text():
    out = narrate.annotate(RESULT, client=FakeClient())
    assert out == {"caption": REPLY["caption"], "text": REPLY["text"],
                   "model": narrate.MODEL}


def test_payload_is_trimmed_to_public_fields():
    client = FakeClient()
    narrate.annotate(RESULT, client=client)
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    names = [l["name"] for l in payload["labels"]]
    assert "Sirius" in names and "Jupiter" in names
    # hidden labels (in frame, not found in the pixels) stay out: given
    # one, the model kept writing it up as just outside the frame
    assert "Andromeda Galaxy (M31)" not in names
    moon = next(l for l in payload["labels"] if l["name"] == "Moon")
    assert moon["moon_phase"] == 0.42
    # and what remains carries no status at all: the internal "projected"
    # and "matched" once read to the model as "computed but not seen"
    assert not any("status" in l for l in payload["labels"])
    assert payload["constellations"] == ["Orion"]
    # satellite crossings (#11) ride along as names only
    assert payload["satellites_crossing"] == ["Iss (Zarya)"]
    # pixel geometry never leaves the app
    content = client.calls[0]["messages"][0]["content"]
    assert '"x"' not in content and '"radius_px"' not in content


def test_overlong_caption_is_truncated():
    reply = {"caption": "x" * 300, "text": "Some text."}
    out = narrate.annotate(RESULT, client=FakeClient(reply=reply))
    assert len(out["caption"]) == narrate.MAX_CAPTION_CHARS


def test_truncated_response_returns_none():
    out = narrate.annotate(RESULT, client=FakeClient(stop_reason="max_tokens"))
    assert out is None


def test_empty_reply_fields_return_none():
    out = narrate.annotate(RESULT, client=FakeClient(reply={"caption": "",
                                                            "text": " "}))
    assert out is None


def test_no_labels_skips_the_call():
    client = FakeClient()
    result = {"labels": [], "verification": {"verified": True}}
    assert narrate.annotate(result, client=client) is None
    assert client.calls == []


def test_unverified_result_skips_the_call():
    # Verification failed -> labels carry no status fields, so the model
    # couldn't be honest about clouds. No call, no spend.
    client = FakeClient()
    result = dict(RESULT, verification={"verified": False,
                                        "error": "image unreadable"})
    assert narrate.annotate(result, client=client) is None
    assert client.calls == []


def test_missing_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert narrate.annotate(RESULT) is None


def test_photo_rides_along_downscaled(tmp_path):
    path = str(tmp_path / "sky.jpg")
    Image.new("RGB", (4000, 3000)).save(path)
    client = FakeClient()
    narrate.annotate(RESULT, image_path=path, client=client)
    content = client.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    # Downscaled to the model's native long edge before upload: sending
    # more pixels costs bandwidth without changing what the model sees.
    sent = Image.open(io.BytesIO(
        base64.b64decode(content[0]["source"]["data"])))
    assert max(sent.size) == narrate.IMAGE_MAX_EDGE
    assert json.loads(content[1]["text"])["labels"]


def test_unreadable_photo_falls_back_to_text_only(tmp_path):
    client = FakeClient()
    out = narrate.annotate(RESULT, image_path=str(tmp_path / "gone.jpg"),
                           client=client)
    assert out is not None  # the writeup still happens
    assert isinstance(client.calls[0]["messages"][0]["content"], str)


FAILED = {"failure": {
    "reason": "no_stars", "stars_detected": 3, "advice": "short_exposure",
    "guess": {"sun_alt_deg": -30, "candidates": [
        {"name": "Venus", "direction": "W", "alt_deg": 10, "az_deg": 270}]}}}


def test_failure_narration_sends_photo_and_context(tmp_path):
    path = str(tmp_path / "blt.jpg")
    Image.new("RGB", (640, 480)).save(path)
    client = FakeClient(reply={"text": "That appears to be a sandwich."})
    out = narrate.annotate_failure(FAILED, path, client=client)
    assert out == {"text": "That appears to be a sandwich.",
                   "model": narrate.MODEL}
    content = client.calls[0]["messages"][0]["content"]
    assert content[0]["type"] == "image"
    payload = json.loads(content[1]["text"])
    assert payload["reason"] == "no_stars"
    assert payload["advice"] == "short_exposure"
    assert payload["sun_alt_deg"] == -30
    # candidates carry name/direction/altitude only — no azimuth, nothing
    # positional beyond what the guess panel already shows
    assert payload["was_up"] == [{"name": "Venus", "direction": "W",
                                  "alt_deg": 10}]


def test_failure_narration_needs_readable_pixels(tmp_path):
    # Without the photo there is nothing the failure copy doesn't already
    # say — no call, no spend.
    client = FakeClient()
    assert narrate.annotate_failure(
        FAILED, str(tmp_path / "gone.jpg"), client=client) is None
    assert client.calls == []


def test_failure_narration_missing_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = str(tmp_path / "x.jpg")
    Image.new("RGB", (64, 64)).save(path)
    assert narrate.annotate_failure(FAILED, path) is None


def test_payload_carries_just_outside_frame_as_phrases():
    result = dict(RESULT, beyond=[
        {"name": "Saturn", "kind": "planet", "mag": 0.7, "edge_x": 1000.0,
         "edge_y": 400.0, "ux": 1.0, "uy": 0.0, "deg": 8.3, "side": "right"},
        {"name": "Pleiades (M45)", "kind": "dso", "mag": 1.6, "edge_x": 500.0,
         "edge_y": 0.0, "ux": 0.0, "uy": -1.0, "deg": 11.6, "side": "above"},
    ])
    payload = narrate._payload(result)
    assert payload["just_outside_frame"] == [
        "Saturn, 8° to the right", "Pleiades (M45), 12° above"]
    # facts only: the edge geometry never leaves the app
    assert "edge_x" not in json.dumps(payload) and "ux" not in payload
    assert narrate._payload(RESULT)["just_outside_frame"] == []


def test_payload_places_labels_in_a_coarse_frame_region():
    # Prod narrated a top-left M31 as "lower left": labels went to the
    # model with no position at all, and it placed the galaxy anyway.
    result = dict(RESULT, labels=[
        {"name": "Andromeda Galaxy (M31)", "x": 900.0, "y": 545.5,
         "mag": 3.6, "kind": "dso", "status": "projected"},
        {"name": "Vega", "x": 1500.0, "y": 2000.0, "mag": 0.03,
         "kind": "star", "status": "matched"},
        {"name": "Deneb", "x": 3000.0, "y": 4000.0, "mag": 1.25,
         "kind": "star", "status": "matched"},
        {"name": "Altair", "x": 200.0, "y": 2000.0, "mag": 0.76,
         "kind": "star", "status": "matched"},
    ])
    payload = narrate._payload(result, width=3024, height=4032)
    assert [l["where"] for l in payload["labels"]] == [
        "upper left", "center", "lower right", "left"]
    # coordinates still never leave the app
    assert "x" not in payload["labels"][0]
    # no frame size, no placement — and no wrong one
    assert "where" not in narrate._payload(result)["labels"][0]

    client = FakeClient()
    narrate.annotate(result, client=client, width=3024, height=4032)
    sent = json.loads(client.calls[0]["messages"][0]["content"])
    assert sent["labels"][0]["where"] == "upper left"
    assert "use that word exactly" in narrate._SYSTEM
    assert "Never place an object from the pixels" in narrate._SYSTEM


def test_hidden_labels_never_reach_the_model():
    # Prod narrated a hidden in-frame M31 as "just outside the visible
    # pixels due to haze", then as "just beyond the frame above" once it
    # had a coarse position — whatever the prompt said. So the model is
    # never told about hidden objects at all, and the prompt has no
    # "hidden" status left to misread.
    hidden_only = dict(RESULT, labels=[
        {"name": "Andromeda Galaxy (M31)", "x": 30.0, "y": 10.0, "mag": 3.6,
         "kind": "dso", "dso_type": "Gxy", "status": "hidden"},
        {"name": "Capella", "x": 90.0, "y": 90.0, "mag": 0.08,
         "kind": "star", "status": "hidden"}])
    client = FakeClient()
    assert narrate.annotate(hidden_only, client=client) is None
    assert client.calls == []  # nothing seen, nothing to say
    system = narrate._SYSTEM
    assert 'status "hidden"' not in system
    # stars are confirmed; the Moon and planets are only projected, and
    # the prompt must not claim more than that
    assert "the stars were confirmed in the pixels" in " ".join(system.split())
    # A hidden DSO is still circled on the page, so it goes along as a
    # plain fragment saying where the mark is — inside the photo. Hidden
    # stars don't: "Capella didn't show" is nothing anyone needs to read.
    payload = narrate._payload(hidden_only, width=100, height=100)
    assert payload["also_in_frame"] == [
        "Andromeda Galaxy (M31), marked in the upper left part of the photo"]
    assert narrate._payload(RESULT, width=100, height=100)["also_in_frame"] == [
        "Andromeda Galaxy (M31), marked in the left part of the photo"]
    # no frame size: say it is marked, not where
    assert narrate._payload(RESULT)["also_in_frame"] == [
        "Andromeda Galaxy (M31), marked on the photo"]
    also_rule = system[system.index("- also_in_frame"):system.index("- just_outside")]
    assert "never say it is outside, beyond" in also_rule
    outside_rule = system[system.index("- just_outside_frame"):system.index("- lore")]
    assert "never as hidden" in outside_rule


def test_night_notes_reach_the_model_as_facts():
    client = FakeClient()
    lines = ["Taken in twilight, about 40 minutes before the sky was fully dark.",
             "Stars down to magnitude 4.2 show in this photo, about what the "
             "eye picks out from the suburbs, from a 2-second exposure."]
    narrate.annotate({**RESULT, "night": {"lines": lines}}, client=client)
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert payload["night_notes"] == lines
    assert "night_notes" in client.calls[0]["system"]
    # a result from before the feature, or one whose night pass failed
    client = FakeClient()
    narrate.annotate({**RESULT, "night": None}, client=client)
    assert json.loads(client.calls[0]["messages"][0]["content"])["night_notes"] == []


def test_the_place_line_rides_with_the_night_notes():
    client = FakeClient()
    line = "The phone recorded its tilt, so sky geometry puts this near 36°N, 74°E: northern Pakistan."
    narrate.annotate({**RESULT, "night": {"lines": ["The Moon was new, so it added no light to the sky."]},
                      "place": {"source": "tilt", "line": line}}, client=client)
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert payload["night_notes"] == ["The Moon was new, so it added no light to the sky.", line]


def test_lore_reaches_the_model_as_its_own_list():
    client = FakeClient()
    narrate.annotate({**RESULT, "lore": [{"abbr": "Ori", "name": "Orion", "line": "Orion is the hunter."}]},
                     client=client)
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    assert payload["lore"] == ["Orion is the hunter."]
    assert "lore" in client.calls[0]["system"]
    client = FakeClient()
    narrate.annotate(RESULT, client=client)
    assert json.loads(client.calls[0]["messages"][0]["content"])["lore"] == []


def test_payload_carries_streak_verdicts_not_pixels():
    result = dict(RESULT, streaks={"streaks": [
        {"start": [10.0, 20.0], "end": [300.0, 400.0], "profile": [1, 2, 3],
         "kind": "meteor", "confidence": "medium", "length_deg": 5.21,
         "shower": None, "reasons": ["fades in, brightens along its path, and stops"]},
        {"start": [0.0, 0.0], "end": [50.0, 50.0], "kind": "satellite",
         "confidence": "high", "length_deg": 2.0,
         "satellite": {"name": "ISS (ZARYA)", "norad_id": "25544"},
         "reasons": ["lies on the computed track of ISS (ZARYA)"]},
    ]})
    payload = narrate._payload(result)
    assert payload["streaks"] == [
        {"kind": "meteor", "confidence": "medium", "length_deg": 5.21,
         "reasons": ["fades in, brightens along its path, and stops"]},
        {"kind": "satellite", "confidence": "high", "length_deg": 2.0,
         "satellite": "ISS (ZARYA)",
         "reasons": ["lies on the computed track of ISS (ZARYA)"]},
    ]
    assert "profile" not in json.dumps(payload)
    assert narrate._payload(RESULT)["streaks"] == []
