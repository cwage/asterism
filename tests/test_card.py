"""Share-card rendering (#13): composition, caption, and placement rules.
Self-contained: synthetic photo + result JSON, no real jobs involved."""

import pytest
from PIL import Image

from app import card

RESULT = {
    "labels": [
        {"name": "Vega", "x": 400, "y": 300, "mag": 0.03, "kind": "star",
         "status": "matched"},
        {"name": "Albireo", "x": 700, "y": 450, "mag": 3.05, "kind": "star",
         "status": "hidden"},
        {"name": "Moon", "x": 200, "y": 200, "mag": None, "kind": "moon",
         "phase": 0.42},
        {"name": "Saturn", "x": 900, "y": 150, "mag": 0.7, "kind": "planet"},
        {"name": "Andromeda Galaxy (M31)", "x": 600, "y": 520, "mag": 3.6,
         "kind": "dso", "radius_px": 60},
        {"name": "Sulafat", "x": 500, "y": 350, "mag": 3.25, "kind": "star",
         "status": "matched"},
        {"name": "OffFrame", "x": 5000, "y": 300, "mag": 1.0, "kind": "star"},
    ],
    "constellations": [
        {"name": "Lyra", "segments": [[380, 280, 420, 320], [420, 320, 460, 300]]},
    ],
    "ephemeris": {"time_utc": "2026-08-13T04:12:18+00:00",
                  "time_source": "exif_offset"},
}


@pytest.fixture()
def photo(tmp_path):
    path = tmp_path / "shot.jpg"
    Image.new("RGB", (1200, 900), (5, 8, 16)).save(path, "JPEG")
    return path


def test_render_composes_card(tmp_path, photo):
    out = tmp_path / "card.png"
    card.render(str(photo), RESULT, "asterism.fly.dev", str(out))
    img = Image.open(out)
    assert img.format == "PNG"
    assert img.width == card.CARD_WIDTH
    # photo scaled to 1600x1200, plus the footer strip
    assert img.height == 1200 + card.FOOTER_H
    # footer background is the app's dark panel color, not photo pixels
    assert img.getpixel((5, img.height - 5)) == card.BG[:3]


def test_render_handles_minimal_result(tmp_path, photo):
    out = tmp_path / "card.png"
    card.render(str(photo), {"labels": []}, "host", str(out))
    assert Image.open(out).height == 1200 + card.FOOTER_H


def test_caption_orders_bodies_dsos_stars_constellations():
    text = card._caption(RESULT)
    # bodies first, DSO stripped of its catalog suffix, stars with count,
    # constellations last
    assert text.index("Moon") < text.index("Andromeda Galaxy")
    assert "Andromeda Galaxy (M31)" not in text
    assert "Vega" in text and "+ 1 more stars" in text
    assert text.rstrip().endswith("Lyra")


def test_caption_empty_result():
    assert card._caption({}) == ""


def test_llm_caption_wins_when_present():
    result = dict(RESULT, narration={"caption": "Saturn beside a waxing Moon",
                                     "text": "…", "model": "test"})
    assert card._caption(result) == "Saturn beside a waxing Moon"
    # an empty LLM caption falls back to the deterministic one
    result["narration"] = {"caption": "", "text": "…", "model": "test"}
    assert "Moon" in card._caption(result)


def test_satellite_tracks_are_drawn_dashed(tmp_path, photo):
    # The card mirrors the canvas: computed tracks appear, dashed.
    result = dict(RESULT, satellites={"crossings": [
        {"name": "Starlink-4634", "norad_id": "53967",
         "points": [[100, 100], [600, 400], [1100, 700]],
         "t_enter_s": 0.0, "t_exit_s": 16.0}]})
    out = tmp_path / "sat.png"
    card.render(str(photo), result, "host", str(out))
    img = Image.open(out).convert("RGB")

    # Dashes mean gaps: sample along the track and require both painted
    # and unpainted pixels, which a solid line could not produce.
    scale = card.CARD_WIDTH / 1200
    hits = 0
    for i in range(1, 60):
        t = i / 60
        x = round((100 + 500 * t) * scale)
        y = round((100 + 300 * t) * scale)
        patch = [img.getpixel((x + dx, y + dy))
                 for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)]
        if any(g > 150 and b > 120 and r < 200 for r, g, b in patch):
            hits += 1
    assert hits > 5, "track should be visible along its path"
    assert hits < 55, "a dashed track must leave gaps"


def test_satellite_track_needs_two_points(tmp_path, photo):
    result = dict(RESULT, satellites={"crossings": [
        {"name": "Blip", "norad_id": "1", "points": [[10, 10]],
         "t_enter_s": 0.0, "t_exit_s": 0.0}]})
    out = tmp_path / "blip.png"
    card.render(str(photo), result, "host", str(out))  # must not raise
    assert Image.open(out).height == 1200 + card.FOOTER_H


def test_dashed_path_alternates_and_skips_zero_length():
    drawn = []
    class FakeDraw:
        def line(self, xy, fill, width):
            drawn.append(xy)
    # A 200px run at dash 18 / gap 12 gives 7 dashes (200 = 6*30 + 20).
    card._dashed_path(FakeDraw(), [(0, 0), (200, 0)], (1, 2, 3, 4), 2)
    assert len(drawn) == 7
    assert drawn[0] == [0.0, 0.0, 18.0, 0.0]
    assert drawn[1] == [30.0, 0.0, 48.0, 0.0]
    # repeated points have no direction and must not divide by zero
    drawn.clear()
    card._dashed_path(FakeDraw(), [(5, 5), (5, 5)], (1, 2, 3, 4), 2)
    assert drawn == []


def test_placement_matches_frontend_semantics():
    placed = []
    assert card._place_text(placed, [(10, 10), (50, 50)], 20, 10, 200, 200) \
        == (10, 10, 20, 10)
    # collision falls through; off-frame rejected; exhaustion returns None
    assert card._place_text(placed, [(15, 12), (50, 50)], 20, 10, 200, 200) \
        == (50, 50, 20, 10)
    assert card._place_text(placed, [(-5, 0), (190, 0)], 20, 10, 200, 200) is None
    # edge-adjacent is not overlap (same rule as the JS harness asserts)
    assert not card._rects_overlap((0, 0, 10, 10), (10, 0, 10, 10))


def test_priority_matches_frontend_semantics():
    labels = [
        {"name": "faint", "mag": 4.4},
        {"name": "Moon", "kind": "moon", "mag": None},
        {"name": "M31", "kind": "dso", "mag": 3.6},
        {"name": "Vega", "mag": 0.03},
        {"name": "Mars", "kind": "planet", "mag": 1.2},
    ]
    order = [l["name"] for l in sorted(labels, key=card._priority)]
    assert order == ["Moon", "Mars", "M31", "Vega", "faint"]
