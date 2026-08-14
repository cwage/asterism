"""Server-rendered share card (#13): the photo with labels burned in from
the same result JSON the canvas renders, plus a caption footer, as a PNG.

Layout mirrors static/index.html's draw(): same colors, same priority
order (Moon, planets, DSOs, stars brightest-first), same greedy
right/left/above/below text placement with collision avoidance."""

import os

CARD_WIDTH = 1600
FOOTER_H = 128
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# Frontend palette (index.html `colors`), as RGBA tuples.
COLORS = {
    "star": (120, 200, 255, 230),
    "planet": (255, 200, 90, 242),
    "moon": (255, 235, 170, 242),
    "dso": (200, 160, 255, 230),
}
FIGURE_COLOR = (150, 170, 210, 90)
FIGURE_TEXT = (150, 170, 210, 153)
BG = (11, 14, 20, 255)          # --bg
ACCENT = (120, 200, 255, 255)   # --accent
INK = (205, 214, 224, 255)      # --ink
DIM = (143, 161, 179, 255)      # --dim


def _rects_overlap(a, b):
    return (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def _place_text(placed, candidates, w, h, frame_w, frame_h):
    """First candidate spot where a w×h box fits on-frame without overlap
    (edge-adjacent boxes are fine); None when everything is taken."""
    for cx, cy in candidates:
        rect = (cx, cy, w, h)
        if cx < 0 or cy < 0 or cx + w > frame_w or cy + h > frame_h:
            continue
        if any(_rects_overlap(p, rect) for p in placed):
            continue
        placed.append(rect)
        return rect
    return None


def _priority(label):
    kind = label.get("kind", "star")
    tier = {"moon": -3, "planet": -2, "dso": -1}.get(kind, 0)
    return tier * 100 + (label.get("mag") or 0)


def _caption(result):
    """One-line caption: the LLM one (#12) when the worker produced it,
    else assembled deterministically from the label list."""
    llm = (result.get("narration") or {}).get("caption")
    if llm:
        return llm
    labels = result.get("labels") or []
    stars = [l for l in labels if l.get("kind", "star") == "star"]
    bodies = [l for l in labels if l.get("kind") in ("moon", "planet")]
    dsos = [l for l in labels if l.get("kind") == "dso"]
    bits = []
    if bodies:
        bits.append(", ".join(b["name"] for b in bodies))
    if dsos:
        bits.append(", ".join(d["name"].split(" (")[0] for d in dsos[:2]))
    if stars:
        top = ", ".join(s["name"] for s in stars[:3])
        more = len(stars) - 3
        bits.append(top + (f" + {more} more stars" if more > 0 else ""))
    cons = result.get("constellations") or []
    if cons:
        names = ", ".join(c["name"] for c in cons[:3])
        extra = len(cons) - 3
        bits.append(names + (f" + {extra} more" if extra > 0 else ""))
    return " · ".join(bits)


def _dashed_ellipse(draw, box, color, width, dashes=14):
    # PIL has no dashed outline: alternate short arcs around the circle.
    step = 360 / dashes
    for i in range(dashes):
        start = i * step
        draw.arc(box, start, start + step * 0.55, fill=color, width=width)


def render(image_path, result, share_host, out_path):
    """Compose the card PNG at out_path. Raises on unreadable input; the
    endpoint treats that as a 500 it can log."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(image_path) as src:
        photo = src.convert("RGB")
    scale = CARD_WIDTH / photo.width
    photo = photo.resize((CARD_WIDTH, round(photo.height * scale)),
                         Image.LANCZOS)
    ph = photo.height

    card = Image.new("RGB", (CARD_WIDTH, ph + FOOTER_H), BG[:3])
    card.paste(photo, (0, 0))
    overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # fonts-dejavu-core ships Sans + Sans-Bold only (no Oblique variant);
    # constellation names settle for the smaller regular face.
    font = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 22)
    font_it = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 19)
    font_title = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), 30)
    font_cap = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 21)

    placed = []
    con_names = []
    for c in result.get("constellations") or []:
        pts = []
        for x1, y1, x2, y2 in c["segments"]:
            draw.line([x1 * scale, y1 * scale, x2 * scale, y2 * scale],
                      fill=FIGURE_COLOR, width=2)
            pts += [(x1 * scale, y1 * scale), (x2 * scale, y2 * scale)]
        pts = [(x, y) for x, y in pts if 0 <= x < CARD_WIDTH and 0 <= y < ph]
        if pts:
            con_names.append((c["name"],
                              sum(p[0] for p in pts) / len(pts),
                              sum(p[1] for p in pts) / len(pts)))

    labels = sorted(result.get("labels") or [], key=_priority)
    markers = []
    for l in labels:
        kind = l.get("kind", "star")
        x, y = l["x"] * scale, l["y"] * scale
        if not (0 <= x < CARD_WIDTH and 0 <= y < ph):
            continue
        dim = l.get("status") == "hidden"
        color = COLORS.get(kind, COLORS["star"])
        if dim:
            color = color[:3] + (100,)
        r = max(9, l["radius_px"] * scale) if l.get("radius_px") \
            else (10 if kind == "star" else 15)
        box = (x - r, y - r, x + r, y + r)
        if dim:
            _dashed_ellipse(draw, box, color, 2)
        else:
            draw.ellipse(box, outline=color, width=2)
        markers.append((l, x, y, r, color))
        placed.append((x - r, y - r, 2 * r, 2 * r))

    for l, x, y, r, color in markers:
        text = l["name"]
        if l.get("kind") == "moon" and l.get("phase") is not None:
            text += f" ({round(l['phase'] * 100)}% lit)"
        tw = draw.textlength(text, font=font)
        th = 26
        spot = _place_text(placed, [
            (x + r + 5, y - th / 2),
            (x - r - 5 - tw, y - th / 2),
            (x - tw / 2, y - r - 5 - th),
            (x - tw / 2, y + r + 5),
        ], tw, th, CARD_WIDTH, ph)
        if spot:
            draw.text((spot[0], spot[1]), text, font=font, fill=color,
                      stroke_width=2, stroke_fill=(0, 0, 0, 160))

    for name, cx, cy in con_names:
        tw = draw.textlength(name, font=font_it)
        th = 24
        spot = _place_text(placed, [
            (cx - tw / 2, cy - th / 2),
            (cx - tw / 2, cy + th),
            (cx - tw / 2, cy - 2 * th),
        ], tw, th, CARD_WIDTH, ph)
        if spot:
            draw.text((spot[0], spot[1]), name, font=font_it,
                      fill=FIGURE_TEXT, stroke_width=2,
                      stroke_fill=(0, 0, 0, 140))

    # Footer: brand, caption, provenance.
    draw.text((28, ph + 18), "asterism", font=font_title, fill=ACCENT)
    brand_w = draw.textlength("asterism", font=font_title)
    caption = _caption(result)
    if caption:
        cap = caption
        while draw.textlength(cap, font=font_cap) > CARD_WIDTH - brand_w - 90 and " · " in cap:
            cap = cap.rsplit(" · ", 1)[0]
        draw.text((28 + brand_w + 26, ph + 26), cap, font=font_cap, fill=INK)
    when = (result.get("ephemeris") or {}).get("time_utc")
    line2 = f"{when[:16].replace('T', ' ')} UTC · " if when else ""
    draw.text((28, ph + 74),
              f"{line2}plate-solved from the star pattern · {share_host}",
              font=font_cap, fill=DIM)

    out = Image.alpha_composite(card.convert("RGBA"), overlay).convert("RGB")
    # Atomic publish: concurrent requests may render the same card; nobody
    # must ever be served a partially-written file.
    tmp_path = f"{out_path}.tmp{os.getpid()}"
    out.save(tmp_path, "PNG")
    os.replace(tmp_path, out_path)
    return out_path
