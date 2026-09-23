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

from . import beyond

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
- Every object in labels is in the frame: the stars were confirmed in
  the pixels, and the Moon, planets, and deep-sky objects are placed
  there by the solve. Describe each as captured; never call one hidden,
  faint, or washed out. Objects that were in the field but did not show
  up are not listed, so never say anything was missing, obscured, or
  lost to the conditions.
- where is the part of the frame the object sits in (upper left, center,
  lower right, and so on). When you say where something is in the photo,
  use that word exactly. Never place an object from the pixels or from
  its neighbours, and never place one that has no where.
- kind "dso" is a deep-sky object; dso_type: OC = open cluster,
  Gxy = galaxy, Neb/OC+Neb = nebula, GC = globular cluster.
- Lower magnitude = brighter. Lead with the most notable catch: the Moon,
  planets, bright deep-sky objects, then bright stars and constellations.
- satellites_crossing lists satellites computed to have passed through the
  frame during the exposure. They were not detected in the pixels, so say
  they passed through, never that a streak is visible. Mention at most one,
  and only when the list is short enough for that to be interesting.
- streaks lists lines found in the pixels themselves, each with a verdict
  (meteor, satellite, or unknown), a confidence, and the measured reasons.
  A meteor is the best catch in almost any frame: lead with it, say how
  long it was and whether it belonged to a shower or was a sporadic, and
  hedge in proportion to the confidence ("likely a meteor" at medium,
  "a streak that may be a meteor" at low). An unknown streak is just that:
  a streak, origin not settled. Never call a streak a meteor or a
  satellite unless the verdict says so.
- also_in_frame lists deep-sky objects whose spot the page marks on the
  photo, though they were not confirmed in the pixels. Each says which
  part of the photo. You may mention one, in those words: the photo takes
  in that object, marked in that part of the frame, worth a closer look.
  It is inside the photo: never say it is outside, beyond, or off the
  edge, and never say it is hidden, missing, faint, or lost to haze or
  cloud.
- just_outside_frame lists bright objects the solve places outside the
  photo's edges, with how far and which way. They are not in the photo:
  you may mention one as being just off the edge, never as captured, and
  never as hidden, faint, or lost to the conditions.
- lore is the site's own sentence about each of the main constellations
  in frame. You may draw on its facts; never contradict them, and don't
  repeat a sentence word for word, since it is shown beside your text.
- night_notes are measured facts about the conditions: twilight or full
  dark, the Moon's phase and what is known of whether it was up, how faint
  a star the photo recorded, and where on Earth the phone's tilt and the
  sky geometry place the camera. You may weave one into the text, keeping
  its numbers and place names as given, and must never contradict them.
  Each note says all that was measured: never settle a question one
  leaves open, and never say the Moon was absent, out, risen, or set
  unless a note says so. Say nothing about the weather or the sky's
  clarity that night_notes doesn't say.
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


def _offset_phrase(pointer):
    """'Saturn, 8° to the right' — a just-outside-the-frame pointer (#118)
    as a sentence fragment, so the model has the fact and not the
    geometry."""
    side = pointer.get("side")
    where = {"left": "to the left", "right": "to the right"}.get(side, side)
    return f"{pointer['name']}, {beyond.format_deg(pointer['deg'])} {where}"


def _where(x, y, width, height):
    """Which third of the frame a label sits in: 'upper left', 'center',
    'lower right'. Coarse on purpose — the model once put a top-left M31
    at "lower left" with no position to go on, and a finer grid would
    just be a finer thing to misread."""
    if not (width and height) or x is None or y is None:
        return None
    col = min(2, max(0, int(3 * x / width)))
    row = min(2, max(0, int(3 * y / height)))
    vert = ("upper", "", "lower")[row]
    horiz = ("left", "", "right")[col]
    return " ".join(w for w in (vert, horiz) if w) or "center"


def _payload(result, width=None, height=None):
    """The trimmed, public-only view of the result the model gets to see.

    Hidden labels (in frame, not found in the pixels) are left out
    entirely. The model was allowed one, and three times running it wrote
    the hidden object up as lying just outside the frame, or lost to haze
    the input never mentioned — whatever the prompt said. The page shows
    the hidden label itself, so the blurb simply not mentioning it
    contradicts nothing. Everything that remains is treated as captured
    (stars snapped to a peak; the Moon, planets, and DSOs placed by the
    solve, with only DSOs pixel-checked), so no status field: the
    internal "projected" once read to the model as "computed but not
    seen", and it narrated a plainly visible M31 as lost to haze. Pixel positions
    become a coarse frame region, since the model places things in the
    text anyway."""
    labels = []
    also_in_frame = []
    for lab in result.get("labels") or []:
        if lab.get("status") == "hidden":
            # The page still circles a hidden DSO, dashed, at its spot,
            # so the blurb may point there too — as a plain fragment
            # like the outside-frame pointers, which the model has
            # handled better than fields it had to interpret.
            if lab.get("kind") == "dso":
                where = _where(lab.get("x"), lab.get("y"), width, height)
                if where is None:  # no frame size: marked, but not where
                    part = "on the photo"
                elif where == "center":
                    part = "in the center of the photo"
                else:
                    part = f"in the {where} part of the photo"
                also_in_frame.append(f"{lab.get('name')}, marked {part}")
            continue
        entry = {"name": lab.get("name"), "kind": lab.get("kind", "star"),
                 "mag": lab.get("mag")}
        if lab.get("dso_type"):
            entry["dso_type"] = lab["dso_type"]
        if lab.get("phase") is not None:
            entry["moon_phase"] = lab["phase"]
        where = _where(lab.get("x"), lab.get("y"), width, height)
        if where:
            entry["where"] = where
        labels.append(entry)
    return {
        "labels": labels,
        "constellations": [c["name"] for c in result.get("constellations") or []],
        "time_utc": (result.get("ephemeris") or {}).get("time_utc"),
        "satellites_crossing": [
            c["name"] for c in
            (result.get("satellites") or {}).get("crossings") or []
        ],
        # Streaks detected in the pixels, with the verdict and its reasons
        # so the model tells the story the numbers support.
        "streaks": [
            {k: v for k, v in {
                "kind": s.get("kind"), "confidence": s.get("confidence"),
                "length_deg": s.get("length_deg"),
                "shower": (s.get("shower") or {}).get("name"),
                "satellite": (s.get("satellite") or {}).get("name"),
                "reasons": s.get("reasons"),
            }.items() if v is not None}
            for s in (result.get("streaks") or {}).get("streaks") or []
        ],
        "also_in_frame": also_in_frame,
        # The model once narrated "the Pleiades just outside the frame"
        # with nothing to go on; now it is told (#118).
        "just_outside_frame": [
            _offset_phrase(p) for p in result.get("beyond") or []
        ],
        # The conditions (#121, #122) as the sentences the page shows, so
        # the model can echo them and never has to derive them.
        "night_notes": list((result.get("night") or {}).get("lines") or [])
        + [line for line in [(result.get("place") or {}).get("line")] if line],
        "lore": [entry["line"] for entry in result.get("lore") or []],
    }


def annotate(result, image_path=None, client=None, width=None, height=None):
    """Narration dict {caption, text, model} or None when unavailable.
    width/height are the upright frame's pixel size, for placing labels.
    Raises on API/parse errors — the worker treats those as best-effort."""
    # Unverified labels carry no status fields, so the model couldn't be
    # honest about what was actually visible — skip the call entirely.
    if not (result.get("verification") or {}).get("verified"):
        return None
    payload = _payload(result, width, height)
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
