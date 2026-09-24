"""LLM caption (#12) and failure notes (#109): payload trimming, response
parsing, and the no-key/no-labels guards. The Claude call is stubbed — no
network."""

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

REPLY = {"caption": "Jupiter and the Moon over Orion"}


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


def test_returns_the_caption():
    out = narrate.annotate(RESULT, client=FakeClient())
    assert out == {"caption": REPLY["caption"], "model": narrate.MODEL}
    # a caption, never a paragraph: the paragraph kept adding its own
    # astronomy however the prompt was tightened
    assert narrate._FORMAT["schema"]["required"] == ["caption"]
    # prod captioned a Saturn/Vega/M31 frame "Saturn and Vega above
    # northern constellations including Andromeda": the galaxy lost to a
    # category. Deep-sky objects outrank stars, and categories are out.
    system = " ".join(narrate._SYSTEM.split())
    assert 'any deep-sky object in labels (kind "dso") come before stars' in system
    assert "never a category" in system


def test_payload_is_trimmed_to_what_a_caption_names():
    client = FakeClient()
    narrate.annotate({**RESULT,
                      "night": {"lines": ["The Moon was new."]},
                      "lore": [{"abbr": "Ori", "name": "Orion",
                                "line": "Orion is the hunter."}],
                      "beyond": [{"name": "Saturn", "deg": 8.3, "side": "right"}]},
                     client=client)
    payload = json.loads(client.calls[0]["messages"][0]["content"])
    # the conditions, lore, off-frame pointers and satellite tracks are on
    # the page in their own words; none of it goes to the model
    assert set(payload) == {"labels", "constellations", "meteors"}
    names = [l["name"] for l in payload["labels"]]
    assert names == ["Jupiter", "Moon", "Sirius"]
    # what remains carries no status and no position: the internal
    # "projected" once read to the model as "computed but not seen"
    assert not any("status" in l or "where" in l for l in payload["labels"])
    assert payload["constellations"] == ["Orion"]
    content = client.calls[0]["messages"][0]["content"]
    assert '"x"' not in content and '"radius_px"' not in content


def test_overlong_caption_is_truncated():
    reply = {"caption": "x" * 300}
    out = narrate.annotate(RESULT, client=FakeClient(reply=reply))
    assert len(out["caption"]) == narrate.MAX_CAPTION_CHARS


def test_truncated_response_returns_none():
    out = narrate.annotate(RESULT, client=FakeClient(stop_reason="max_tokens"))
    assert out is None


def test_empty_caption_returns_none():
    out = narrate.annotate(RESULT, client=FakeClient(reply={"caption": " "}))
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
    assert out is not None  # the caption still happens
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


def test_hidden_labels_never_reach_the_model():
    # Prod narrated a hidden in-frame M31 as "just outside the visible
    # pixels due to haze", whatever the prompt said. A caption names what
    # the photo shows, so hidden labels stay out entirely.
    hidden_only = dict(RESULT, labels=[
        {"name": "Andromeda Galaxy (M31)", "x": 30.0, "y": 10.0, "mag": 3.6,
         "kind": "dso", "dso_type": "Gxy", "status": "hidden"},
        {"name": "Capella", "x": 90.0, "y": 90.0, "mag": 0.08,
         "kind": "star", "status": "hidden"}])
    client = FakeClient()
    assert narrate.annotate(hidden_only, client=client) is None
    assert client.calls == []  # nothing seen, nothing to say


def test_only_confident_meteors_reach_the_model():
    # A caption has no room to hedge, so a low-confidence meteor or an
    # unknown streak never goes along; satellites are drawn on the photo.
    result = dict(RESULT, streaks={"streaks": [
        {"start": [10.0, 20.0], "end": [300.0, 400.0], "profile": [1, 2, 3],
         "kind": "meteor", "confidence": "medium", "length_deg": 5.21,
         "shower": None, "reasons": ["fades in, brightens along its path, and stops"]},
        {"kind": "meteor", "confidence": "high", "shower": {"name": "Perseids"}},
        {"kind": "meteor", "confidence": "low", "shower": {"name": "Perseids"}},
        {"kind": "unknown", "confidence": "low"},
        {"kind": "satellite", "confidence": "high",
         "satellite": {"name": "ISS (ZARYA)", "norad_id": "25544"}},
    ]})
    payload = narrate._payload(result)
    assert payload["meteors"] == [{"shower": "sporadic"}, {"shower": "Perseids"}]
    assert "profile" not in json.dumps(payload)
    assert narrate._payload(RESULT)["meteors"] == []
    # the rule that limits what the caption names must admit the meteors,
    # or it cancels the rule that asks for them
    system = " ".join(narrate._SYSTEM.split())
    assert "Name only what labels, constellations, or meteors list" in system
