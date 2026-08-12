"""Label verification against pixels: snap-to-source, residual-field warp
correction, hidden-star flagging. Synthetic images with known ground truth,
no solver involved."""

import numpy as np
import pytest

from app import verify
from tests import synth

WIDTH, HEIGHT = 1200, 900


def warp(x, y):
    """The synthetic 'Night Sight' warp: smooth, spatially varying, up to
    ~30px — the shape of the stack-alignment drag seen in issue #28."""
    return 30.0 * (x / WIDTH) ** 2, -20.0 * (y / HEIGHT)


# Predicted (WCS-projected) positions, spread across the frame.
PREDICTED = [(150.0, 120.0), (600.0, 100.0), (1050.0, 150.0),
             (120.0, 450.0), (580.0, 420.0), (1000.0, 480.0),
             (180.0, 780.0), (620.0, 800.0), (1020.0, 760.0),
             (380.0, 260.0), (820.0, 620.0), (400.0, 620.0)]


def star_labels(positions):
    return [{"name": f"S{i}", "x": x, "y": y, "mag": 2.0, "kind": "star"}
            for i, (x, y) in enumerate(positions)]


@pytest.fixture()
def warped_image(tmp_path):
    """Stars rendered where the warped image actually puts them."""
    true_pos = [(x + warp(x, y)[0], y + warp(x, y)[1]) for x, y in PREDICTED]
    path = tmp_path / "warped.jpg"
    synth.render_points(str(path), true_pos, WIDTH, HEIGHT)
    return str(path), true_pos


def test_matched_labels_snap_to_true_positions(warped_image):
    path, true_pos = warped_image
    labels, _, meta = verify.apply(path, star_labels(PREDICTED), [],
                                   WIDTH, HEIGHT)
    assert meta["verified"] is True
    assert all(l["status"] == "matched" for l in labels)
    for lab, (tx, ty) in zip(labels, true_pos):
        assert np.hypot(lab["x"] - tx, lab["y"] - ty) < 2.0
    # ~20-30px corrections on a 1200px frame must trip the warp flag
    assert meta["warped"] is True
    assert meta["median_correction_px"] > 6.0


def test_unwarped_image_reports_clean(tmp_path):
    path = tmp_path / "clean.jpg"
    synth.render_points(str(path), PREDICTED, WIDTH, HEIGHT)
    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED), [],
                                   WIDTH, HEIGHT)
    assert all(l["status"] == "matched" for l in labels)
    assert meta["warped"] is False
    assert meta["p90_correction_px"] < 3.0


def test_cloud_hidden_star_flagged_and_interpolated(tmp_path):
    # Render every star except one; the missing one simulates cloud cover.
    hidden_idx = 4  # (580, 420), mid-frame so the field interpolates around it
    true_pos = [(x + warp(x, y)[0], y + warp(x, y)[1]) for x, y in PREDICTED]
    rendered = [p for i, p in enumerate(true_pos) if i != hidden_idx]
    path = tmp_path / "cloud.jpg"
    synth.render_points(str(path), rendered, WIDTH, HEIGHT)

    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED), [],
                                   WIDTH, HEIGHT)
    hidden = labels[hidden_idx]
    assert hidden["status"] == "hidden"
    assert meta["stars_hidden"] == 1
    assert meta["stars_matched"] == len(PREDICTED) - 1
    # its position should follow the interpolated warp, not the raw WCS
    tx, ty = true_pos[hidden_idx]
    assert np.hypot(hidden["x"] - tx, hidden["y"] - ty) < 10.0


def test_bodies_are_warp_corrected_but_not_snapped(warped_image):
    path, _ = warped_image
    body = {"name": "Jupiter", "x": 590.0, "y": 430.0, "mag": -2.0,
            "kind": "planet"}
    labels, _, _ = verify.apply(path, star_labels(PREDICTED) + [body],
                                [], WIDTH, HEIGHT)
    jupiter = labels[-1]
    assert jupiter["status"] == "projected"
    dx, dy = warp(590.0, 430.0)
    assert np.hypot(jupiter["x"] - (590.0 + dx), jupiter["y"] - (430.0 + dy)) < 6.0


def test_constellation_segments_follow_the_field(warped_image):
    path, _ = warped_image
    figures = [{"name": "Testfig", "abbr": "Tst",
                "segments": [[150.0, 120.0, 620.0, 800.0]]}]
    _, out_figures, _ = verify.apply(path, star_labels(PREDICTED), figures,
                                     WIDTH, HEIGHT)
    x1, y1, x2, y2 = out_figures[0]["segments"][0]
    d1 = warp(150.0, 120.0)
    d2 = warp(620.0, 800.0)
    assert np.hypot(x1 - (150.0 + d1[0]), y1 - (120.0 + d1[1])) < 6.0
    assert np.hypot(x2 - (620.0 + d2[0]), y2 - (800.0 + d2[1])) < 6.0


def test_unreadable_image_returns_originals(tmp_path):
    labels = star_labels(PREDICTED)
    figures = [{"name": "F", "abbr": "F", "segments": [[0.0, 0.0, 1.0, 1.0]]}]
    out_labels, out_figures, meta = verify.apply(
        str(tmp_path / "missing.jpg"), labels, figures, WIDTH, HEIGHT)
    assert out_labels == labels
    assert out_figures == figures
    assert meta["verified"] is False
    # no statuses invented for labels we could not check
    assert all("status" not in l for l in out_labels)
