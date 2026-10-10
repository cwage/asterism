"""The social share card: the version of the card a feed can show, and
the share-link previews that use it. Synthetic photos only."""

import json
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import card, db, main, worker

PHOTO_RGB = (5, 8, 16)


@pytest.fixture()
def portrait(tmp_path):
    path = tmp_path / "tall.jpg"
    Image.new("RGB", (1200, 1600), PHOTO_RGB).save(path, "JPEG")
    return path


@pytest.fixture()
def landscape(tmp_path):
    path = tmp_path / "wide.jpg"
    Image.new("RGB", (2000, 1500), PHOTO_RGB).save(path, "JPEG")
    return path


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "asterism.db"))
    db.init_db()


def _star(name, mag, x=600, y=800, **extra):
    return {"name": name, "mag": mag, "x": x, "y": y, "kind": "star",
            "status": "matched", **extra}


def test_social_labels_lead_with_bodies_then_proper_names():
    result = {"labels": [
        _star("ξ² Sgr", 3.5),
        _star("Nunki", 2.05),
        _star("Kaus Australis", 1.79, status="hidden"),
        {"name": "Saturn", "mag": 0.6, "x": 1, "y": 1, "kind": "planet"},
        _star("Albaldah", 2.9),
        {"name": "Lagoon Nebula (M8)", "mag": 6.0, "x": 1, "y": 1,
         "kind": "dso", "status": "projected"},
        _star("σ Sgr", 2.0),
    ]}
    names = [l["name"] for l in card.social_labels(result, limit=5)]
    # bodies and DSOs whatever their magnitude, then stars brightest
    # first with proper names ahead of Bayer letters; never a hidden one
    assert names == ["Saturn", "Lagoon Nebula (M8)", "Nunki", "Albaldah", "σ Sgr"]
    assert [l["name"] for l in card.social_labels(result, limit=3)] == [
        "Saturn", "Lagoon Nebula (M8)", "Nunki"]
    # the cap holds even when bodies and DSOs alone would pass it
    crowded = {"labels": [{"name": f"M{i}", "mag": 5.0, "x": 1, "y": 1,
                           "kind": "dso", "status": "projected"} for i in range(14)]}
    assert len(card.social_labels(crowded)) == card.SOCIAL_LABELS


def _bright(img, box):
    """Pixels in box (x0, y0, x1, y1) that are text-bright."""
    return [img.getpixel((x, y)) for x in range(*box[0::2]) for y in range(*box[1::2])
            if max(img.getpixel((x, y))) > 150]


def test_portrait_and_landscape_widths(tmp_path, portrait, landscape):
    # the card is the whole photo at the feed width, nothing above it: the
    # caption sits on a gradient inside the bottom edge
    out = tmp_path / "s.png"
    card.render_social(str(portrait), {"labels": []}, "host", str(out))
    img = Image.open(out)
    assert img.size == (card.SOCIAL_WIDTH_PORTRAIT, 1600)
    assert max(abs(a - b) for a, b in zip(img.getpixel((img.width - 5, 5)), PHOTO_RGB)) <= 2
    assert _bright(img, (0, 1600 - card.SOCIAL_CAPTION_H, 600, 1600))   # the caption
    assert not _bright(img, (0, 0, img.width, 1600 - card.SOCIAL_CAPTION_H))

    card.render_social(str(landscape), {"labels": []}, "host", str(out))
    img = Image.open(out)
    assert img.size == (card.SOCIAL_WIDTH_LANDSCAPE, 1050)


def _ring_pixel(img, x, y):
    # the right-hand edge of a star's ring, at the 1200-wide portrait scale
    return img.getpixel((x + 10, y))


def test_a_marker_is_drawn_only_with_its_name(tmp_path, portrait, monkeypatch):
    result = {"labels": [_star("Vega", 0.03)]}
    out = tmp_path / "s.png"
    card.render_social(str(portrait), result, "host", str(out))
    background = Image.open(out).getpixel((300, 1200))
    assert _ring_pixel(Image.open(out), 600, 800) != background

    # no room for the name: no ring either
    monkeypatch.setattr(card, "_place_text", lambda *a, **k: None)
    card.render_social(str(portrait), result, "host", str(out))
    assert _ring_pixel(Image.open(out), 600, 800) == background


def test_a_name_at_the_edge_slides_inside_the_frame(tmp_path, portrait):
    # a star 15px from the right edge: every spot beside, above or below
    # it runs off the frame, so the name slides in and the ring is drawn
    result = {"labels": [_star("Vega", 0.03, x=1185, y=800)]}
    out = tmp_path / "s.png"
    card.render_social(str(portrait), result, "host", str(out))
    img = Image.open(out)
    assert _ring_pixel(img, 1185, 800) != img.getpixel((300, 1200))
    assert _bright(img, (900, 760, 1200, 840))
    # closer still, the ring itself would leave the frame: no marker
    result = {"labels": [_star("Vega", 0.03, x=1195, y=800)]}
    card.render_social(str(portrait), result, "host", str(out))
    img = Image.open(out)
    assert not _bright(img, (900, 760, 1200, 840))


def test_a_name_can_sit_on_the_gradient_above_the_caption(tmp_path, portrait):
    # the Moon low over the trees, inside the footer gradient but above
    # the caption's lines: it keeps its name
    band = round(1600 * card.SOCIAL_FOOTER)
    moon = {"name": "Moon", "mag": -10, "x": 300, "y": 1600 - band + 30,
            "kind": "moon", "status": "projected", "phase": 0.45}
    out = tmp_path / "s.png"
    card.render_social(str(portrait), {"labels": [moon]}, "host", str(out))
    img = Image.open(out)
    assert _bright(img, (320, 1600 - band, 700, 1600 - card.SOCIAL_CAPTION_H))


def test_the_caption_moves_to_the_top_when_the_names_are_low(tmp_path, portrait):
    # the Moon on the horizon, where the caption would cover it: the
    # caption takes the top edge instead and the Moon keeps its name
    moon = {"name": "Moon", "mag": -10, "x": 300, "y": 1560,
            "kind": "moon", "status": "projected", "phase": 0.45}
    out = tmp_path / "s.png"
    card.render_social(str(portrait), {"labels": [moon]}, "host", str(out))
    img = Image.open(out)
    assert _bright(img, (0, 0, 600, card.SOCIAL_CAPTION_H))          # the caption
    assert _bright(img, (320, 1520, 700, 1600))                       # Moon (45% lit)
    assert not _bright(img, (0, 1600 - card.SOCIAL_CAPTION_H, 200, 1600))


def _figure(name, *segments):
    return {"name": name, "abbr": name[:3], "segments": [list(s) for s in segments]}


def test_figures_are_judged_by_the_stars_the_photo_shows():
    # Sagittarius: the top named stars matched, the bottom ones hidden
    # behind the horizon, so the figure stays but its legs go. Scorpius:
    # one star matched and three hidden, so it goes entirely. Lupus: one
    # and one, a tie, which is a star matched in the haze, so it goes.
    result = {"labels": [
        _star("Nunki", 2.0, x=500, y=600), _star("Kaus Borealis", 2.8, x=600, y=620),
        _star("Ascella", 2.6, x=450, y=640),
        _star("Kaus Australis", 1.8, x=520, y=900, status="hidden"),
        _star("Fang", 2.6, x=100, y=700),
        _star("Antares", 1.0, x=150, y=850, status="hidden"),
        _star("Shaula", 1.6, x=200, y=950, status="hidden"),
        _star("Sargas", 1.9, x=250, y=980, status="hidden"),
        _star("δ Lup", 3.2, x=900, y=1000), _star("γ Lup", 2.8, x=950, y=1050, status="hidden"),
    ], "constellations": [
        _figure("Lupus", (900, 1000, 950, 1050), (950, 1050, 980, 900)),
        _figure("Sagittarius", (500, 600, 600, 620), (600, 620, 450, 640),
                (450, 640, 520, 900), (520, 900, 540, 1000)),
        _figure("Scorpius", (100, 700, 150, 850), (150, 850, 200, 950),
                (200, 950, 250, 980)),
    ]}
    kept = card.social_figures(result, snap=5)
    assert [(c["name"], round(share, 2)) for c, share in kept] == [("Sagittarius", 0.75)]
    assert kept[0][0]["segments"] == [[500, 600, 600, 620], [600, 620, 450, 640]]


def test_figures_without_named_stars_stay_above_the_sky_floor():
    # Delphinus has no star bright enough to be named; it is drawn when
    # it sits in the sky and dropped when it sits in the foreground. The
    # floor is a high percentile of the visible named stars, so one
    # star matched in the haze at the frame's edge doesn't pull it down.
    stars = [_star(f"s{i}", 2.0, x=300 + 50 * i, y=400 + 20 * i) for i in range(10)]
    stars.append(_star("Fang", 2.6, x=1190, y=1500))      # the one in the haze
    above = _figure("Delphinus", (800, 300, 850, 320), (850, 320, 820, 360))
    below = _figure("Telescopium", (800, 1300, 850, 1320), (850, 1320, 820, 1360))
    result = {"labels": stars, "constellations": [above, below]}
    assert [c["name"] for c, _ in card.social_figures(result, snap=5)] == ["Delphinus"]
    # with no visible named star at all there is no floor, and no figure
    assert card.social_figures({"labels": [], "constellations": [above]}, snap=5) == []


def test_figure_lines_brighten_on_a_black_sky():
    bright, dark = card.SOCIAL_FIGURE_ALPHA
    assert card._figure_alpha(0) == dark
    assert card._figure_alpha(card.SOCIAL_DARK_SKY) == bright
    assert card._figure_alpha(255) == bright
    assert bright < card._figure_alpha(card.SOCIAL_DARK_SKY / 2) < dark


def test_a_long_caption_is_cut_to_fit_the_footer(tmp_path, portrait):
    # The narrator can return up to 90 characters with no " · " to split
    # on; at the smallest size it must still end inside the right margin.
    caption = "Extraordinarily luminous constellations stretching gloriously across " * 2
    out = tmp_path / "s.png"
    card.render_social(str(portrait), {"labels": [], "narration": {"caption": caption}},
                       "host", str(out))
    img = Image.open(out)
    band = round(1600 * card.SOCIAL_FOOTER)
    assert _bright(img, (0, 1600 - band, 600, 1600))
    assert not _bright(img, (img.width - 30, 1600 - band, img.width, 1600))


def test_endpoint_serves_and_caches_each_style(fresh_db, portrait):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, image_path, status, result_json) "
            "VALUES ('solved', ?, 'done', ?)",
            (str(portrait), json.dumps({"labels": [_star("Vega", 0.03)]})))
    client = TestClient(main.app)
    social = client.get("/jobs/solved/card?style=social")
    assert social.status_code == 200
    assert social.headers["content-type"] == "image/png"
    assert "asterism-solved-social.png" in social.headers["content-disposition"]
    assert Image.open(f"{portrait}.social.png").width == card.SOCIAL_WIDTH_PORTRAIT
    # the full card is unchanged, and the two are cached apart
    full = client.get("/jobs/solved/card")
    assert full.status_code == 200
    assert Image.open(f"{portrait}.card.png").width == card.CARD_WIDTH
    assert client.get("/jobs/solved/card?style=poster").status_code == 422


def test_share_links_preview_the_social_card_titled_by_caption(fresh_db, portrait):
    result = {"labels": [], "narration": {"caption": 'Saturn & "Vega" <rising>'}}
    with db.get_conn() as conn:
        for job_id, status, hidden in (("a" * 32, "done", 0), ("b" * 32, "done", 1),
                                       ("c" * 32, "queued", 0)):
            conn.execute(
                "INSERT INTO jobs (id, image_path, status, result_json, hidden) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, str(portrait), status, json.dumps(result), hidden))
    client = TestClient(main.app)

    page = client.get("/?job=" + "a" * 32).text
    assert f'/jobs/{"a" * 32}/card?style=social"' in page
    # the caption titles the preview, escaped for the attribute it sits in
    assert ('<meta property="og:title" content="Saturn &amp; &quot;Vega&quot; '
            '&lt;rising&gt; — asterism">') in page

    # hidden, unsolved and unknown jobs give nothing away
    for job_id in ("b" * 32, "c" * 32, "d" * 32):
        page = client.get("/?job=" + job_id).text
        assert 'content="asterism — what you saw"' in page
        assert "rising" not in page


def test_hide_and_sweep_collect_both_cards(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "s3cret")
    shown = tmp_path / "shown.jpg"
    old = tmp_path / "old.jpg"
    for path in (shown, old):
        path.write_bytes(b"x")
        for cached in card.cached_paths(str(path)):
            open(cached, "wb").write(b"png")
    with db.get_conn() as conn:
        conn.execute("INSERT INTO jobs (id, image_path, status) "
                     "VALUES ('bad', ?, 'done')", (str(shown),))
        conn.execute("INSERT INTO jobs (id, image_path, status, created_at) "
                     "VALUES ('old', ?, 'done', datetime('now', '-400 hours'))",
                     (str(old),))

    class Req:
        headers = {"authorization": "Bearer s3cret"}
    main.hide_job("bad", Req())
    assert not any(os.path.exists(p) for p in card.cached_paths(str(shown)))

    assert worker.sweep_expired() >= 1
    assert not old.exists()
    assert not any(os.path.exists(p) for p in card.cached_paths(str(old)))
