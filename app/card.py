"""Server-rendered share cards (#13): the photo with labels burned in from
the same result JSON the canvas renders, as a PNG.

Two styles. The full card mirrors static/index.html's draw(): same
colors, same priority order (Moon, planets, DSOs, stars brightest-first),
same greedy right/left/above/below text placement with collision
avoidance, every label, and a caption footer. The social card is the one
link previews and posts use: a feed shows an image about 500px wide,
where the full card's 40 labels shrink to unreadable 5px text. It keeps
a dozen names drawn three times the size, the main constellations'
names, and puts the caption in a headline bar on top."""

import math
import os
import re

from . import beyond

CARD_WIDTH = 1600
FOOTER_H = 128

# The social card: a portrait photo goes out 1200 wide, a landscape one
# 1400, so the labels hold the same size against a ~500px feed either way.
SOCIAL_WIDTH_PORTRAIT = 1200
SOCIAL_WIDTH_LANDSCAPE = 1400
SOCIAL_HEADER = 0.13      # headline bar height, as a share of the width
SOCIAL_LABELS = 12        # names on the photo, Moon/planets/DSOs first
SOCIAL_FIGURE_NAMES = 3   # constellations whose names are written out

# Each style is cached beside the upload under its own suffix; the hide
# and the retention sweep remove all of them.
SUFFIXES = {"full": ".card.png", "social": ".social.png"}


def cached_paths(image_path):
    """Every rendered card an upload may have beside it."""
    return [image_path + suffix for suffix in SUFFIXES.values()]
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

# Frontend palette (index.html `colors`), as RGBA tuples.
COLORS = {
    "star": (120, 200, 255, 230),
    "planet": (255, 200, 90, 242),
    "moon": (255, 235, 170, 242),
    "dso": (200, 160, 255, 230),
}
# Pointers to objects just outside the frame (#118): one colour whatever
# the kind, since "not in the shot" is the fact the colour has to carry.
BEYOND_COLOR = (255, 140, 110, 242)
FIGURE_COLOR = (150, 170, 210, 90)
FIGURE_TEXT = (150, 170, 210, 153)
SATELLITE_COLOR = (140, 230, 190, 190)   # frontend track stroke
SATELLITE_TEXT = (140, 230, 190, 217)
STREAK_COLOR = (255, 120, 170, 217)      # frontend streak stroke
STREAK_TEXT = (255, 120, 170, 230)
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
    # A meteor leads the caption: it is the catch of the night, and
    # nothing else in the frame is one-of-a-kind.
    for streak in (result.get("streaks") or {}).get("streaks") or []:
        if streak.get("kind") == "meteor" and streak.get("confidence") != "low":
            bits.append("a " + streak_name(streak))
            break
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


def streak_name(streak):
    """What the overlay calls a detected streak: the verdict, and the
    name when there is one; a low-confidence verdict is just a streak.
    Mirrors streakName() in the frontend."""
    kind = streak.get("kind")
    if kind == "satellite" and streak.get("satellite"):
        return streak["satellite"]["name"]
    if kind == "meteor" and streak.get("shower"):
        return f"{streak['shower']['name']} meteor"
    if streak.get("confidence") == "low" or kind not in ("meteor", "satellite"):
        return "streak"
    return kind


def _dashed_path(draw, points, color, width, dash=18.0, gap=12.0):
    """Dashed polyline: PIL has no dash support, so walk the path by arc
    length and stroke alternating runs. Satellite tracks (#11) are
    computed, never pixel-detected — dashes carry that the same way the
    frontend canvas does."""
    on, used = True, 0.0  # used: distance into the current dash/gap run
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            continue
        pos = 0.0
        while pos < length:
            run = (dash if on else gap) - used
            end = min(length, pos + run)
            if on:
                t0, t1 = pos / length, end / length
                draw.line([x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0,
                           x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1],
                          fill=color, width=width)
            used += end - pos
            if used >= (dash if on else gap) - 1e-9:
                on, used = not on, 0.0
            pos = end


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
    font_ptr = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 25)

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

    # Satellite crossings (#11) ride under the labels, like the figures.
    sat_names = []
    for crossing in (result.get("satellites") or {}).get("crossings") or []:
        pts = [(x * scale, y * scale) for x, y in crossing.get("points") or []]
        if len(pts) < 2:
            continue
        _dashed_path(draw, pts, SATELLITE_COLOR, 2)
        mid = pts[len(pts) // 2]
        if 0 <= mid[0] < CARD_WIDTH and 0 <= mid[1] < ph:
            sat_names.append((crossing["name"], mid[0], mid[1]))

    # Streaks found in the pixels: a solid bracket either side of the
    # line, an arrowhead at a meteor's terminal end, as the frontend draws.
    streak_names = []
    for streak in (result.get("streaks") or {}).get("streaks") or []:
        (x0, y0), (x1, y1) = streak["start"], streak["end"]
        x0, y0, x1, y1 = x0 * scale, y0 * scale, x1 * scale, y1 * scale
        length = math.hypot(x1 - x0, y1 - y0)
        if not length:
            continue
        nx, ny = -(y1 - y0) / length * 6, (x1 - x0) / length * 6
        for sign in (-1, 1):
            draw.line([x0 + sign * nx, y0 + sign * ny, x1 + sign * nx, y1 + sign * ny],
                      fill=STREAK_COLOR, width=2)
        if streak.get("kind") == "meteor" and streak.get("confidence") != "low":
            ux, uy, ah = (x1 - x0) / length, (y1 - y0) / length, 10
            draw.polygon([(x1 + ux * ah, y1 + uy * ah),
                          (x1 - uy * ah * 0.6, y1 + ux * ah * 0.6),
                          (x1 + uy * ah * 0.6, y1 - ux * ah * 0.6)],
                         fill=STREAK_COLOR)
        mid = ((x0 + x1) / 2, (y0 + y1) / 2)
        if 0 <= mid[0] < CARD_WIDTH and 0 <= mid[1] < ph:
            streak_names.append((streak_name(streak), mid[0], mid[1], nx, ny))

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

    # Satellite names last: the track is the point, the name is a bonus.
    for name, sx, sy in sat_names:
        tw = draw.textlength(name, font=font_it)
        th = 22
        spot = _place_text(placed, [
            (sx + 8, sy - th / 2),
            (sx - tw - 8, sy - th / 2),
            (sx - tw / 2, sy + th),
        ], tw, th, CARD_WIDTH, ph)
        if spot:
            draw.text((spot[0], spot[1]), name, font=font_it,
                      fill=SATELLITE_TEXT, stroke_width=2,
                      stroke_fill=(0, 0, 0, 140))

    # Streak names beside their line, whichever side has room.
    for name, sx, sy, nx, ny in streak_names:
        tw = draw.textlength(name, font=font_it)
        th = 22
        spot = _place_text(placed, [
            (sx + nx * 2.2 - tw / 2, sy + ny * 2.2 - th / 2),
            (sx - nx * 2.2 - tw / 2, sy - ny * 2.2 - th / 2),
            (sx - tw / 2, sy + th),
        ], tw, th, CARD_WIDTH, ph)
        if spot:
            draw.text((spot[0], spot[1]), name, font=font_it,
                      fill=STREAK_TEXT, stroke_width=2,
                      stroke_fill=(0, 0, 0, 140))

    # Bright objects just outside the frame (#118): an arrow at the edge
    # crossing, pointing out, with the name and distance inside the tail.
    # Last in line for space, like the canvas: a pointer never displaces a
    # label for something in the shot. Both arrow and text must fit before
    # drawing either or reserving any space.
    for p in result.get("beyond") or []:
        hx = min(max(p["edge_x"] * scale, 0), CARD_WIDTH - 1)
        hy = min(max(p["edge_y"] * scale, 0), ph - 1)
        ux, uy = p["ux"], p["uy"]
        tx, ty = hx - ux * 40, hy - uy * 40
        arrow = (min(hx, tx) - 5, min(hy, ty) - 5,
                 abs(hx - tx) + 10, abs(hy - ty) + 10)
        if any(_rects_overlap(r, arrow) for r in placed):
            continue
        text = f"{p['name']} {beyond.format_deg(p['deg'])}"
        tw = draw.textlength(text, font=font_ptr)
        th = 29
        upper = min(ty - th / 2, arrow[1]) - 5 - th
        lower = max(ty + th / 2, arrow[1] + arrow[3]) + 5
        left, right = arrow[0] - 5 - tw, arrow[0] + arrow[2] + 5
        spots = {
            "right": [(tx - 5 - tw, ty - th / 2), (tx - tw, lower), (tx - tw, upper)],
            "left": [(tx + 5, ty - th / 2), (tx, lower), (tx, upper)],
            "above": [(tx - tw / 2, ty + 5), (right, ty), (left, ty)],
            "below": [(tx - tw / 2, ty - 5 - th), (right, ty - th), (left, ty - th)],
        }
        spot = _place_text([*placed, arrow], spots.get(p.get("side"), spots["right"]),
                           tw, th, CARD_WIDTH, ph)
        if not spot:
            continue
        placed.extend((arrow, spot))
        color = BEYOND_COLOR
        draw.line([(tx, ty), (hx, hy)], fill=color, width=3)
        a = math.atan2(uy, ux)
        for da in (-0.5, 0.5):
            draw.line([(hx, hy), (hx - 13 * math.cos(a + da),
                                  hy - 13 * math.sin(a + da))],
                      fill=color, width=3)
        draw.text((spot[0], spot[1]), text, font=font_ptr, fill=color,
                  stroke_width=2, stroke_fill=(0, 0, 0, 160))

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
    return _publish(out, out_path)


def _publish(image, out_path):
    # Atomic publish: concurrent requests may render the same card; nobody
    # must ever be served a partially-written file.
    tmp_path = f"{out_path}.tmp{os.getpid()}"
    image.save(tmp_path, "PNG")
    os.replace(tmp_path, out_path)
    return out_path


_GREEK = re.compile("[\u0370-\u03ff]")


def social_labels(result, limit=SOCIAL_LABELS):
    """The labels the social card names, in drawing order: every visible
    Moon, planet and deep-sky object, then stars brightest-first, proper
    names ahead of Bayer letters — "Nunki" says something at a glance,
    "ξ² Sgr" only to someone who already knows. Hidden labels are left
    out: the card names what the photo shows."""
    labels = sorted((l for l in result.get("labels") or []
                     if l.get("status") != "hidden"), key=_priority)
    special = [l for l in labels if l.get("kind") in ("moon", "planet", "dso")]
    stars = [l for l in labels if l.get("kind", "star") == "star"]
    named = [l for l in stars if not _GREEK.search(l["name"])]
    bayer = [l for l in stars if _GREEK.search(l["name"])]
    return special + (named + bayer)[:max(0, limit - len(special))]


def render_social(image_path, result, share_host, out_path):
    """Compose the social card PNG at out_path: the whole photo under a
    headline bar, with a dozen names it can afford to draw large. A
    marker is drawn only when its name fits — a ring with no name is
    noise at this size — and nothing points off the frame."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(image_path) as src:
        photo = src.convert("RGB")
    width = (SOCIAL_WIDTH_LANDSCAPE if photo.width > photo.height
             else SOCIAL_WIDTH_PORTRAIT)
    scale = width / photo.width
    ph = round(photo.height * scale)
    head = round(width * SOCIAL_HEADER)
    unit = width / SOCIAL_WIDTH_PORTRAIT   # sizes below are for 1200 wide
    height = head + ph

    def bold(size):
        return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"),
                                  round(size))

    canvas = Image.new("RGB", (width, height), BG[:3])
    canvas.paste(photo.resize((width, ph), Image.LANCZOS), (0, head))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def at(x, y):
        return x * scale, y * scale + head

    def on_photo(x, y):
        return 0 <= x < width and head <= y < height

    placed = [(0, 0, width, head)]
    figures = result.get("constellations") or []
    for c in figures:
        for x1, y1, x2, y2 in c["segments"]:
            draw.line([*at(x1, y1), *at(x2, y2)], fill=(170, 190, 230, 110),
                      width=round(3 * unit))

    # Markers reserve their space first, so no name lands on another
    # object's ring; each is drawn only once its own name has a spot.
    marks = []
    for l in social_labels(result):
        x, y = at(l["x"], l["y"])
        if not on_photo(x, y):
            continue
        big = l.get("kind", "star") != "star"
        r = (26 if big else 16) * unit
        placed.append((x - r, y - r, 2 * r, 2 * r))
        marks.append((l, x, y, r, big))
    for l, x, y, r, big in marks:
        kind = l.get("kind", "star")
        color = COLORS.get(kind, COLORS["star"])[:3] + (255,)
        text = l["name"]
        if kind == "moon" and l.get("phase") is not None:
            text += f" ({round(l['phase'] * 100)}% lit)"
        font = bold((40 if big else 34) * unit)
        tw = draw.textlength(text, font=font)
        th = font.size * 1.2
        pad = 10 * unit
        spot = _place_text(placed, [
            (x + r + pad, y - th / 2),
            (x - r - pad - tw, y - th / 2),
            (x - tw / 2, y - r - pad - th),
            (x - tw / 2, y + r + pad),
        ], tw, th, width, height)
        if spot:
            draw.ellipse((x - r, y - r, x + r, y + r), outline=color,
                         width=round((5 if big else 4) * unit))
            draw.text(spot[:2], text, font=font, fill=color,
                      stroke_width=round(5 * unit), stroke_fill=(0, 0, 0, 200))

    # A meteor is the catch of the night: its streak and name go on too.
    for streak in (result.get("streaks") or {}).get("streaks") or []:
        if streak.get("kind") != "meteor" or streak.get("confidence") == "low":
            continue
        (x0, y0), (x1, y1) = at(*streak["start"]), at(*streak["end"])
        draw.line([x0, y0, x1, y1], fill=STREAK_COLOR[:3] + (255,),
                  width=round(5 * unit))
        name = streak_name(streak)
        font = bold(36 * unit)
        tw = draw.textlength(name, font=font)
        th = font.size * 1.2
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        spot = _place_text(placed, [
            (mx + 20 * unit, my - th / 2), (mx - 20 * unit - tw, my - th / 2),
            (mx - tw / 2, my + 20 * unit), (mx - tw / 2, my - 20 * unit - th),
        ], tw, th, width, height)
        if spot:
            draw.text(spot[:2], name, font=font, fill=STREAK_TEXT[:3] + (255,),
                      stroke_width=round(5 * unit), stroke_fill=(0, 0, 0, 200))

    # The main constellations' names, quiet capitals at each figure's
    # centre, last in line for space: on a photo with no planet or galaxy
    # in it the figures are the story, and their names are what reads.
    font = bold(30 * unit)
    for c in sorted(figures, key=lambda c: -len(c["segments"]))[:SOCIAL_FIGURE_NAMES]:
        pts = [p for x1, y1, x2, y2 in c["segments"]
               for p in (at(x1, y1), at(x2, y2)) if on_photo(*p)]
        if len(pts) < 4:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        name = c["name"].upper()
        tw = draw.textlength(name, font=font)
        th = font.size * 1.3
        spot = _place_text(placed, [
            (cx - tw / 2, cy - th / 2), (cx - tw / 2, cy + th),
            (cx - tw / 2, cy - 2 * th),
        ], tw, th, width, height)
        if spot:
            draw.text(spot[:2], name, font=font, fill=(215, 225, 245, 200),
                      stroke_width=round(4 * unit), stroke_fill=(0, 0, 0, 190))

    out = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    # Headline: the caption, shrunk to fit and then shortened by its
    # " · " parts, over the provenance line. The bar is painted over the
    # composite, so a figure line running off the top of the photo stops
    # at its edge.
    draw = ImageDraw.Draw(out)
    draw.rectangle((0, 0, width, head - 1), fill=BG[:3])
    title = _caption(result) or "A night sky, plate-solved"
    font = bold(head * 0.36)
    while draw.textlength(title, font=font) > width - 80:
        if font.size > head * 0.26:
            font = bold(font.size - 2)
        elif " · " in title:
            title = title.rsplit(" · ", 1)[0]
        else:
            break
    draw.text((40, head * 0.14), title, font=font, fill=INK[:3])
    sub_font = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"),
                                  round(head * 0.2))
    draw.text((40, head * 0.62),
              f"plate-solved from the stars alone · {share_host}",
              font=sub_font, fill=ACCENT[:3])
    return _publish(out, out_path)


RENDERERS = {"full": render, "social": render_social}
