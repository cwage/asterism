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
    assert "Sirius" in names and "Andromeda Galaxy (M31)" in names
    # statuses and types ride along so the model can be honest about clouds
    m31 = next(l for l in payload["labels"] if "M31" in l["name"])
    assert m31["status"] == "hidden" and m31["dso_type"] == "Gxy"
    moon = next(l for l in payload["labels"] if l["name"] == "Moon")
    assert moon["moon_phase"] == 0.42
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
