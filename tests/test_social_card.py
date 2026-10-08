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


def test_portrait_and_landscape_widths(tmp_path, portrait, landscape):
    out = tmp_path / "s.png"
    card.render_social(str(portrait), {"labels": []}, "host", str(out))
    img = Image.open(out)
    head = round(card.SOCIAL_WIDTH_PORTRAIT * card.SOCIAL_HEADER)
    assert img.size == (card.SOCIAL_WIDTH_PORTRAIT, head + 1600)
    # the headline bar is the app's dark panel, not photo pixels
    assert img.getpixel((img.width - 5, 5)) == card.BG[:3]

    card.render_social(str(landscape), {"labels": []}, "host", str(out))
    img = Image.open(out)
    head = round(card.SOCIAL_WIDTH_LANDSCAPE * card.SOCIAL_HEADER)
    assert img.size == (card.SOCIAL_WIDTH_LANDSCAPE, head + 1050)


def _ring_pixel(img, x, y):
    # the right-hand edge of a star's ring, at the 1200-wide portrait scale
    head = round(card.SOCIAL_WIDTH_PORTRAIT * card.SOCIAL_HEADER)
    return img.getpixel((x + 16, y + head))


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


def test_figure_lines_stop_at_the_headline_bar(tmp_path, portrait):
    # a figure running off the top of the photo would otherwise draw
    # across the headline
    result = {"labels": [], "constellations": [
        {"name": "Lyra", "abbr": "Lyr", "segments": [[1100, -400, 1100, 400]]}]}
    out = tmp_path / "s.png"
    card.render_social(str(portrait), result, "host", str(out))
    img = Image.open(out)
    assert img.getpixel((1100, 20)) == card.BG[:3]
    assert img.getpixel((1100, 300)) != img.getpixel((300, 300))


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
