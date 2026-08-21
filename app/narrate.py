"""LLM narration (#12): turn the solved result into a short "what you
captured" writeup for the results page, plus a one-line caption that
replaces the deterministic one on the share card. Failed solves get a
shorter note about what the photo appears to be instead (#109).

Best-effort like the ephemeris and DSO layers: no API key, a network
failure, or a malformed reply just means no narration. The prompt sees a
trimmed copy of the already-public result JSON (object names, kinds,
magnitudes, statuses) plus the photo itself — the upload-page disclosure
says photos are sent to an AI service for this — but never pixel
positions or coordinates.
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
            "text": {"type": "string"},
        },
        "required": ["caption", "text"],
        "additionalProperties": False,
    },
}

_SYSTEM = """\
You write about plate-solved night-sky photos for the person who took them,
often on a phone. You get the structured result of a solve: the objects
identified in the frame and the constellations drawn.

Rules:
- Mention only objects present in the input. Never invent objects, and never
  state a fact (distance, type, lore) unless you are certain of it.
- status "hidden" means the object was in frame but not actually visible in
  the pixels (cloud or haze, usually). You may mention at most one notable
  hidden object, clearly as being there but not visible tonight.
- kind "dso" is a deep-sky object; dso_type: OC = open cluster,
  Gxy = galaxy, Neb/OC+Neb = nebula, GC = globular cluster.
- Lower magnitude = brighter. Lead with the most notable catch: the Moon,
  planets, bright deep-sky objects, then bright stars and constellations.
- satellites_crossing lists satellites computed to have passed through the
  frame during the exposure. They were not detected in the pixels, so say
  they passed through, never that a streak is visible. Mention at most one,
  and only when the list is short enough for that to be interesting.
- You may also be shown the photo. The labels above stay the authority on
  sky objects — never claim a sky object from the pixels alone. You may
  mention the foreground scene (a treeline, a rooftop, someone silhouetted
  watching the sky) when it adds warmth.
- Never describe people beyond noting a presence: no appearance, age, or
  identity. Never read or repeat text visible in the image.
- Warm, plain tone. No emoji, no exclamation marks, no hype.

Return JSON:
- caption: one line for the photo card — at most eight words, no trailing
  period, naming the best objects in the frame.
- text: two to four sentences for the results page — what the photo
  captured, plus one well-known fact about the most notable object.
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
    """The trimmed, public-only view of the result the model gets to see."""
    labels = []
    for lab in result.get("labels") or []:
        entry = {"name": lab.get("name"), "kind": lab.get("kind", "star"),
                 "mag": lab.get("mag"), "status": lab.get("status")}
        if lab.get("dso_type"):
            entry["dso_type"] = lab["dso_type"]
        if lab.get("phase") is not None:
            entry["moon_phase"] = lab["phase"]
        labels.append(entry)
    return {
        "labels": labels,
        "constellations": [c["name"] for c in result.get("constellations") or []],
        "time_utc": (result.get("ephemeris") or {}).get("time_utc"),
        "satellites_crossing": [
            c["name"] for c in
            (result.get("satellites") or {}).get("crossings") or []
        ],
    }


def annotate(result, image_path=None, client=None):
    """Narration dict {caption, text, model} or None when unavailable.
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
    text = " ".join(str(data.get("text", "")).split())
    if not caption or not text:
        return None
    return {"caption": caption[:MAX_CAPTION_CHARS], "text": text,
            "model": MODEL}


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
