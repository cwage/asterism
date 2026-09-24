"""LLM caption (#12): a one-line headline for the results page and the
share card, naming the best of what the solve labeled. Failed solves get
a short note about what the photo appears to be instead (#109).

The caption is all the model writes for a solved photo. It used to write
a two-to-four sentence "what you captured" paragraph too, and after a
run of prompt and payload fixes (#112, #156, #162) that paragraph still
kept putting its own astronomy in: Deneb at the "center" of Cygnus,
"dawn twilight" read off a sun-altitude range. Everything true in it
already sat on the page as the labels, the night lines and the lore,
which are deterministic, so the paragraph went and the caption stayed.

Best-effort like the ephemeris and DSO layers: no API key, a network
failure, or a malformed reply just means no caption. The prompt sees a
trimmed copy of the already-public result JSON (object names, kinds,
magnitudes) plus the photo itself — the upload-page disclosure says
photos are sent to an AI service for this — but never pixel positions
or coordinates.
"""

import base64
import io
import json
import os

from PIL import Image

MODEL = os.environ.get("NARRATE_MODEL", "claude-haiku-4-5")
MAX_CAPTION_CHARS = 90  # the card footer is one line
MAX_TOKENS = 600
TIMEOUT_SECONDS = 30.0

# Haiku's vision tier downscales to a 1568px long edge anyway; doing it
# here keeps the request body small (a phone JPEG is ~1MB, ~1.4MB in
# base64) without changing what the model sees.
IMAGE_MAX_EDGE = 1568

_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "caption": {"type": "string"},
        },
        "required": ["caption"],
        "additionalProperties": False,
    },
}

_SYSTEM = """\
You write a one-line caption for a plate-solved night-sky photo. You get
the objects the solve identified in the frame and the constellations
drawn, and you may be shown the photo.

Rules:
- Name only what labels, constellations, or meteors list, spelled as
  given (a meteor by its shower, or just "a meteor" when sporadic). Never
  add a fact about them: no distances, types, positions, lore, or
  conditions.
- Lower magnitude = brighter. Prefer the most notable catch: a meteor,
  the Moon, planets, bright deep-sky objects, then bright stars and
  constellations.
- meteors lists streaks in the pixels judged to be meteors, with the
  shower it belongs to (or sporadic). Name a meteor only when that list
  has one.
- The labels are the authority on sky objects: never name one from the
  pixels alone. You may add the foreground scene from the photo (over a
  treeline, above a rooftop) when there is one.
- Never describe people, and never read or repeat text visible in the
  image.
- Plain words. No emoji, no exclamation marks, no hype.

Return JSON:
- caption: at most eight words, no trailing period.
"""


_FAILURE_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
}

# Failed uploads are the photos nobody meant to send here — food, thumbs,
# the occasional genuinely private mistake — so this prompt is written
# defensively: gentle humor for the obvious cases, and a hard stop on
# describing people, text, or anything sensitive. Failure pages are only
# visible by link, but the uploader still reads this about their own photo.
_FAILURE_SYSTEM = """\
You write one short, warm note about a photo that was uploaded to a
night-sky identification site but could not be plate-solved. You see the
photo and the solver's failure context.

Rules:
- If it looks like a sky photo, say in plain words what likely went wrong,
  using the failure context (clouds, twilight, too short an exposure, too
  few stars).
- The context is measured, not guessed: trust its numbers over how the
  pixels look. sun_alt_deg below -18 means the sky was fully dark whatever
  the photo's brightness suggests — a bright sky then is city glow or a
  nearby light, never twilight.
- advice, when present, names the diagnosed problem: "daylight" (sun was
  up), "twilight" (sky still washing out stars), "short_exposure" (the
  camera took a quick snap; night mode and holding the phone still is the
  fix), "dark_but_empty" (a long exposure under a dark sky — likely
  clouds, moon, or light pollution). Build on that diagnosis; never give
  advice that contradicts it.
- If it is clearly not a sky photo, gently name what it appears to be —
  one line of light, good-natured humor is welcome; tease the situation,
  never the person.
- If the context lists objects that were up, you may mention the brightest
  one and where to look for it.
- Never describe people beyond noting someone is in the frame: no
  appearance, age, or identity. Never read or repeat text from screens or
  documents. If the photo seems private or sensitive in any way, say only
  that it doesn't look like a night sky and stop there.
- Two sentences at most. No emoji, no exclamation marks.

Return JSON:
- text: the note.
"""


def _image_block(image_path):
    """The photo as an API content block, downscaled to the model's native
    resolution. None when the file can't be read — narration then proceeds
    (or is skipped) without pixels rather than failing the job."""
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            img.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
    except Exception:
        return None
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": data}}


def _client_or_none(client):
    if client is not None:
        return client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    return anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)


def _payload(result):
    """The trimmed, public-only view of the result the model gets to see.

    Hidden labels (in frame, not found in the pixels) are left out: the
    caption names what the photo shows. Streaks ride along only as
    meteors the detector stood behind — a caption has no room to hedge."""
    labels = []
    for lab in result.get("labels") or []:
        if lab.get("status") == "hidden":
            continue
        entry = {"name": lab.get("name"), "kind": lab.get("kind", "star"),
                 "mag": lab.get("mag")}
        if lab.get("dso_type"):
            entry["dso_type"] = lab["dso_type"]
        labels.append(entry)
    return {
        "labels": labels,
        "constellations": [c["name"] for c in result.get("constellations") or []],
        "meteors": [
            {"shower": (s.get("shower") or {}).get("name") or "sporadic"}
            for s in (result.get("streaks") or {}).get("streaks") or []
            if s.get("kind") == "meteor" and s.get("confidence") in ("high", "medium")
        ],
    }


def annotate(result, image_path=None, client=None):
    """Narration dict {caption, model} or None when unavailable.
    Raises on API/parse errors — the worker treats those as best-effort."""
    # Unverified labels carry no status fields, so the model couldn't be
    # honest about what was actually visible — skip the call entirely.
    if not (result.get("verification") or {}).get("verified"):
        return None
    payload = _payload(result)
    if not payload["labels"]:
        return None
    client = _client_or_none(client)
    if client is None:
        return None

    content = json.dumps(payload, sort_keys=True)
    if image_path:
        image = _image_block(image_path)
        if image:
            content = [image, {"type": "text", "text": content}]
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        output_config={"format": _FORMAT},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason != "end_turn":
        return None
    data = json.loads(next(b.text for b in response.content
                           if b.type == "text"))
    caption = " ".join(str(data.get("caption", "")).split())
    if not caption:
        return None
    return {"caption": caption[:MAX_CAPTION_CHARS], "model": MODEL}


def _failure_payload(result):
    """The failure context the model gets: reason and advice codes, star
    count, and the fallback guess's headline facts — never coordinates."""
    failure = result.get("failure") or {}
    guess = failure.get("guess") or {}
    return {
        "reason": failure.get("reason"),
        "advice": failure.get("advice"),
        "stars_detected": failure.get("stars_detected"),
        "sun_alt_deg": guess.get("sun_alt_deg"),
        "was_up": [{"name": c.get("name"), "direction": c.get("direction"),
                    "alt_deg": c.get("alt_deg")}
                   for c in (guess.get("candidates") or [])[:3]],
    }


def annotate_failure(result, image_path, client=None):
    """Narration dict {text, model} for a failed solve, or None. The photo
    is the whole point here — without readable pixels there is nothing to
    say that the failure copy doesn't already cover. Raises on API/parse
    errors — the worker treats those as best-effort."""
    image = _image_block(image_path)
    if image is None:
        return None
    client = _client_or_none(client)
    if client is None:
        return None

    payload = _failure_payload(result)
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_FAILURE_SYSTEM,
        output_config={"format": _FAILURE_FORMAT},
        messages=[{"role": "user",
                   "content": [image, {"type": "text",
                                       "text": json.dumps(payload,
                                                          sort_keys=True)}]}],
    )
    if response.stop_reason != "end_turn":
        return None
    data = json.loads(next(b.text for b in response.content
                           if b.type == "text"))
    text = " ".join(str(data.get("text", "")).split())
    if not text:
        return None
    return {"text": text, "model": MODEL}
