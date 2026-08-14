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
