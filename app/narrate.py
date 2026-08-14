"""LLM narration (#12): turn the solved result into a short "what you
captured" writeup for the results page, plus a one-line caption that
replaces the deterministic one on the share card.

Best-effort like the ephemeris and DSO layers: no API key, a network
failure, or a malformed reply just means no narration. The prompt sees a
trimmed copy of the already-public result JSON (object names, kinds,
magnitudes, statuses) — never the photo, never pixel positions, never
coordinates.
"""

import json
import os

MODEL = os.environ.get("NARRATE_MODEL", "claude-haiku-4-5")
MAX_CAPTION_CHARS = 90  # the card footer is one line
MAX_TOKENS = 600
TIMEOUT_SECONDS = 30.0

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
- Warm, plain tone. No emoji, no exclamation marks, no hype.

Return JSON:
- caption: one line for the photo card — at most eight words, no trailing
  period, naming the best objects in the frame.
- text: two to four sentences for the results page — what the photo
  captured, plus one well-known fact about the most notable object.
"""


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


def annotate(result, client=None):
    """Narration dict {caption, text, model} or None when unavailable.
    Raises on API/parse errors — the worker treats those as best-effort."""
    # Unverified labels carry no status fields, so the model couldn't be
    # honest about what was actually visible — skip the call entirely.
    if not (result.get("verification") or {}).get("verified"):
        return None
    payload = _payload(result)
    if not payload["labels"]:
        return None
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        import anthropic
        client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        output_config={"format": _FORMAT},
        messages=[{"role": "user",
                   "content": json.dumps(payload, sort_keys=True)}],
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
