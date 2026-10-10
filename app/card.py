"""Server-rendered share cards (#13): the photo with labels burned in from
the same result JSON the canvas renders, as a PNG.

Two styles. The full card mirrors static/index.html's draw(): same
colors, same priority order (Moon, planets, DSOs, stars brightest-first),
same greedy right/left/above/below text placement with collision
avoidance, every label, and a caption footer. The social card is the one
link previews and posts use: a feed shows an image about 500px wide,
where the full card's 40 labels shrink to unreadable 5px text. It keeps
ten names drawn three times the size at a regular weight, only the
figures the photo shows, the main constellations' names, and the caption
on a gradient along the bottom edge (#167)."""

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
SOCIAL_LABELS = 10        # names on the photo, Moon/planets/DSOs first
SOCIAL_FIGURE_NAMES = 3   # constellations whose names are written out
SOCIAL_FOOTER = 0.16      # caption gradient, as a share of the height...
SOCIAL_FOOTER_MAX = 0.2   # ...capped against the width for a tall panorama
SOCIAL_CAPTION_H = 120    # the caption's two lines and margins, at 1200 wide
# How close (as a share of the photo's width) a figure's vertex must be
# to a named star to count as that star (#167).
SOCIAL_SNAP = 0.012
# The sky floor for figures with no named star: this percentile of the
# visible named stars' heights, so one star matched in the haze at the
# edge of the frame doesn't pull it to the ground.
SOCIAL_FLOOR_PCT = 0.8
# Figure line alpha on a bright sky and on a black one, with the sky's
# mean brightness (0-255) below which it is "black".
SOCIAL_FIGURE_ALPHA = (105, 170)
SOCIAL_DARK_SKY = 40
SOCIAL_FIGURE = (205, 215, 235)
SOCIAL_FIGURE_TEXT = (215, 225, 240, 150)
# Names on the social card: off-white for stars, the full card's hues
# muted for the rest, all at a regular weight (#167).
SOCIAL_COLORS = {
    "star": (236, 240, 245, 235),
    "planet": (255, 215, 140, 235),
    "moon": (255, 235, 190, 235),
    "dso": (215, 190, 255, 235),
}

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
    """The labels the social card names, in drawing order, at most limit:
    the visible Moon, planets and deep-sky objects, then stars
    brightest-first, proper names ahead of Bayer letters — "Nunki" says something at a glance,
    "ξ² Sgr" only to someone who already knows. Hidden labels are left
    out: the card names what the photo shows."""
    labels = sorted((l for l in result.get("labels") or []
                     if l.get("status") != "hidden"), key=_priority)
    special = [l for l in labels if l.get("kind") in ("moon", "planet", "dso")]
    stars = [l for l in labels if l.get("kind", "star") == "star"]
    named = [l for l in stars if not _GREEK.search(l["name"])]
    bayer = [l for l in stars if _GREEK.search(l["name"])]
    return (special + named + bayer)[:limit]


def social_figures(result, snap=None):
    """The constellation figures the social card draws, each with the
    share of its named stars the photo shows (#167).

    Every figure the solve projects is a line somewhere in the frame, and
    most frames have a horizon: on a Milky Way shot the whole southern
    sky lands as a tangle of polygons across the field or the sea. The
    verifier already says which named stars are hidden, and the figures
    are judged by them. A figure is drawn only when more of its named
    stars are shown than hidden; one that stays loses the legs that end on a hidden star; a
    figure with no named star at all (Delphinus, Sagitta) is kept only
    if all of it sits above the sky floor, the height below which named
    stars stop being visible. The floor is a high percentile of the
    visible stars' positions, not the lowest one: a single star matched
    in the haze at the frame's edge must not drag the floor to the
    ground."""
    # Stars only: the verifier also marks planets matched and clusters
    # hidden, and neither is a vertex of any figure.
    labels = [l for l in result.get("labels") or []
              if l.get("kind", "star") == "star"]
    matched = [(l["x"], l["y"]) for l in labels if l.get("status") == "matched"]
    hidden = [(l["x"], l["y"]) for l in labels if l.get("status") == "hidden"]
    if snap is None:
        xs = [x for x, _ in matched + hidden]
        snap = max(xs, default=0) * SOCIAL_SNAP
    floor = None
    if matched:
        ys = sorted(y for _, y in matched)
        floor = ys[round(SOCIAL_FLOOR_PCT * (len(ys) - 1))]

    def near(pt, pts):
        return any(abs(pt[0] - x) <= snap and abs(pt[1] - y) <= snap
                   for x, y in pts)

    out = []
    for c in result.get("constellations") or []:
        segs = c.get("segments") or []
        verts = {(x, y) for x1, y1, x2, y2 in segs for x, y in ((x1, y1), (x2, y2))}
        if not verts:
            continue
        shown = sum(near(v, matched) for v in verts)
        lost = sum(near(v, hidden) for v in verts)
        if shown + lost == 0:
            if floor is None or any(y > floor for _, y in verts):
                continue
            out.append((c, 1.0))
            continue
        share = shown / (shown + lost)
        if share <= 0.5:
            # a tie is a figure the verifier matched one star of in the
            # haze (δ Lup in the sea off Cape San Blas): not shown
            continue
        kept = [s for s in segs
                if not near((s[0], s[1]), hidden) and not near((s[2], s[3]), hidden)]
        if kept:
            out.append((dict(c, segments=kept), share))
    return out


def _sky_luma(image):
    """Mean brightness of the photo's upper half, 0–255: the sky, on
    most frames, before the horizon."""
    from PIL import ImageStat
    half = image.crop((0, 0, image.width, max(1, image.height // 2)))
    return ImageStat.Stat(half.convert("L")).mean[0]


def _figure_alpha(luma):
    """Figure lines at a fixed alpha vanish on a near-black phone sky
    and shout on a bright one; this scales them between the two."""
    dark = SOCIAL_FIGURE_ALPHA[1]
    bright = SOCIAL_FIGURE_ALPHA[0]
    t = min(1.0, max(0.0, luma / SOCIAL_DARK_SKY))
    return round(dark + (bright - dark) * t)


def _slid(candidates, w, h, frame_w, frame_h):
    """The same spots, then each slid just inside the frame: a label for
    a star at the edge is clipped otherwise, and a half name is worse
    than a shifted one."""
    inside = [(min(max(cx, 0), frame_w - w), min(max(cy, 0), frame_h - h))
              for cx, cy in candidates]
    return list(candidates) + [c for c in inside if c not in candidates]


def render_social(image_path, result, share_host, out_path):
    """Compose the social card PNG at out_path: the whole photo, a
    handful of names it can afford to draw large, the figures the
    photo actually shows, and the caption on a gradient along the
    bottom edge (or the top, when the bottom is where the names are). Everything on it is drawn to recede: the photo is the
    subject and the chart is a whisper over it (#167). A marker is
    drawn only when its name fits — a ring with no name is noise at this
    size — and nothing points off the frame."""
    from PIL import Image, ImageDraw, ImageFont

    with Image.open(image_path) as src:
        photo = src.convert("RGB")
    width = (SOCIAL_WIDTH_LANDSCAPE if photo.width > photo.height
             else SOCIAL_WIDTH_PORTRAIT)
    scale = width / photo.width
    height = round(photo.height * scale)
    unit = width / SOCIAL_WIDTH_PORTRAIT   # sizes below are for 1200 wide

    def face(size):
        return ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"),
                                  round(size))

    canvas = photo.resize((width, height), Image.LANCZOS)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def at(x, y):
        return x * scale, y * scale

    def on_photo(x, y):
        return 0 <= x < width and 0 <= y < height

    # The caption's lines are reserved first, so no name lands under
    # them. Only the lines: the gradient beyond them is still sky, and a
    # Moon low over the trees wants its name. The caption takes the
    # bottom edge unless more of the names would be under it there than
    # at the top: a Moon or a planet is usually low, and it is the reason
    # for the photo.
    words = round(SOCIAL_CAPTION_H * unit)
    # The gradient is a share of the height, but never shorter than the
    # caption it carries: on a 3:1 panorama the share alone would leave
    # the words on bare photo.
    band = min(height, max(round(min(height * SOCIAL_FOOTER, width * SOCIAL_FOOTER_MAX)),
                           round(words * 1.3)))
    names = [at(l["x"], l["y"]) for l in social_labels(result)]
    low = sum(y >= height - words for _, y in names)
    high = sum(y < words for _, y in names)
    on_top = low > high
    placed = [(0, 0, width, words) if on_top else (0, height - words, width, words)]

    figures = social_figures(result, snap=photo.width * SOCIAL_SNAP)
    alpha = _figure_alpha(_sky_luma(canvas))
    for c, share in figures:
        color = SOCIAL_FIGURE[:3] + (round(alpha * (0.6 + 0.4 * share)),)
        for x1, y1, x2, y2 in c["segments"]:
            draw.line([*at(x1, y1), *at(x2, y2)], fill=color,
                      width=max(1, round(2 * unit)))

    # Markers reserve their space first, so no name lands on another
    # object's ring; each is drawn only once its own name has a spot.
    marks = []
    for l in social_labels(result):
        x, y = at(l["x"], l["y"])
        big = l.get("kind", "star") != "star"
        r = (16 if big else 10) * unit
        # a ring half off the frame looks like a mistake: a star that
        # close to the edge goes unnamed
        if not (on_photo(x - r, y - r) and on_photo(x + r, y + r)):
            continue
        placed.append((x - r, y - r, 2 * r, 2 * r))
        marks.append((l, x, y, r, big))
    for l, x, y, r, big in marks:
        kind = l.get("kind", "star")
        color = SOCIAL_COLORS.get(kind, SOCIAL_COLORS["star"])
        text = l["name"]
        if kind == "moon" and l.get("phase") is not None:
            text += f" ({round(l['phase'] * 100)}% lit)"
        font = face((30 if big else 26) * unit)
        tw = draw.textlength(text, font=font)
        th = font.size * 1.2
        pad = 8 * unit
        spot = _place_text(placed, _slid([
            (x + r + pad, y - th / 2),
            (x - r - pad - tw, y - th / 2),
            (x - tw / 2, y - r - pad - th),
            (x - tw / 2, y + r + pad),
        ], tw, th, width, height), tw, th, width, height)
        if spot:
            draw.ellipse((x - r, y - r, x + r, y + r), outline=color,
                         width=max(1, round(1.5 * unit)))
            draw.text(spot[:2], text, font=font, fill=color,
                      stroke_width=round(2 * unit), stroke_fill=(0, 0, 0, 160))

    # A meteor is the catch of the night: its streak and name go on too.
    for streak in (result.get("streaks") or {}).get("streaks") or []:
        if streak.get("kind") != "meteor" or streak.get("confidence") == "low":
            continue
        (x0, y0), (x1, y1) = at(*streak["start"]), at(*streak["end"])
        draw.line([x0, y0, x1, y1], fill=STREAK_COLOR[:3] + (230,),
                  width=round(3 * unit))
        name = streak_name(streak)
        font = face(28 * unit)
        tw = draw.textlength(name, font=font)
        th = font.size * 1.2
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        spot = _place_text(placed, _slid([
            (mx + 16 * unit, my - th / 2), (mx - 16 * unit - tw, my - th / 2),
            (mx - tw / 2, my + 16 * unit), (mx - tw / 2, my - 16 * unit - th),
        ], tw, th, width, height), tw, th, width, height)
        if spot:
            draw.text(spot[:2], name, font=font, fill=STREAK_TEXT[:3] + (240,),
                      stroke_width=round(2 * unit), stroke_fill=(0, 0, 0, 160))

    # The main constellations' names, quiet letterspaced capitals at each
    # figure's centre, last in line for space: on a photo with no planet
    # or galaxy in it the figures are the story, and their names are
    # what reads.
    font = face(21 * unit)
    for c, _ in sorted(figures, key=lambda f: -len(f[0]["segments"]))[:SOCIAL_FIGURE_NAMES]:
        pts = [p for x1, y1, x2, y2 in c["segments"]
               for p in (at(x1, y1), at(x2, y2)) if on_photo(*p)]
        if len(pts) < 4:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        name = " ".join(c["name"].upper())
        tw = draw.textlength(name, font=font)
        th = font.size * 1.3
        spot = _place_text(placed, [
            (cx - tw / 2, cy - th / 2), (cx - tw / 2, cy + th),
            (cx - tw / 2, cy - 2 * th),
        ], tw, th, width, height)
        if spot:
            draw.text(spot[:2], name, font=font, fill=SOCIAL_FIGURE_TEXT,
                      stroke_width=round(2 * unit), stroke_fill=(0, 0, 0, 140))

    out = Image.alpha_composite(canvas.convert("RGBA"), overlay)

    # The caption on a gradient up from the bottom edge, shrunk to fit,
    # then shortened by its " · " parts, then cut at a word with an
    # ellipsis, over the provenance line. The gradient is painted over
    # the composite, so a figure line running off the bottom fades with
    # the photo instead of crossing the words.
    grad = Image.new("RGBA", (width, band), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for i in range(band):
        t = (band - 1 - i if on_top else i) / band
        gd.line([(0, i), (width, i)], fill=BG[:3] + (round(215 * t ** 1.4),))
    out.alpha_composite(grad, (0, 0 if on_top else height - band))
    draw = ImageDraw.Draw(out)
    title = _caption(result) or "A night sky, plate-solved"
    margin = 40 * unit
    font = face(34 * unit)
    while draw.textlength(title, font=font) > width - 2 * margin:
        if font.size > 26 * unit:
            font = face(font.size - 2)
        elif " · " in title:
            title = title.rsplit(" · ", 1)[0]
        elif " " in title.rstrip("…"):
            title = title.rstrip("…").rsplit(" ", 1)[0].rstrip(" ,;:—") + "…"
        else:
            break
    sub = face(19 * unit)
    title_y = 28 * unit if on_top else height - 36 * unit - sub.size - font.size * 1.2 - 8 * unit
    draw.text((margin, title_y), title, font=font, fill=INK)
    draw.text((margin, title_y + font.size * 1.2 + 8 * unit),
              f"{share_host}  ·  plate-solved from the stars alone",
              font=sub, fill=DIM)
    return _publish(out.convert("RGB"), out_path)


RENDERERS = {"full": render, "social": render_social}
