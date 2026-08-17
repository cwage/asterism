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
    labels, _, meta = verify.apply(path, star_labels(PREDICTED), [])
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
    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED), [])
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

    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED), [])
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
    labels, _, _ = verify.apply(path, star_labels(PREDICTED) + [body], [])
    jupiter = labels[-1]
    assert jupiter["status"] == "projected"
    dx, dy = warp(590.0, 430.0)
    assert np.hypot(jupiter["x"] - (590.0 + dx), jupiter["y"] - (430.0 + dy)) < 6.0


def test_constellation_segments_follow_the_field(warped_image):
    path, _ = warped_image
    figures = [{"name": "Testfig", "abbr": "Tst",
                "segments": [[150.0, 120.0, 620.0, 800.0]]}]
    _, out_figures, _ = verify.apply(path, star_labels(PREDICTED), figures)
    x1, y1, x2, y2 = out_figures[0]["segments"][0]
    d1 = warp(150.0, 120.0)
    d2 = warp(620.0, 800.0)
    assert np.hypot(x1 - (150.0 + d1[0]), y1 - (120.0 + d1[1])) < 6.0
    assert np.hypot(x2 - (620.0 + d2[0]), y2 - (800.0 + d2[1])) < 6.0


def test_faint_decoy_does_not_steal_a_bright_star(tmp_path):
    # The Arcturus failure: a faint cloud blob a few px nearer the predicted
    # position must not out-compete the real (warp-dragged) star.
    target = 8  # (1020, 760): large warp, true star ~27px from prediction
    true_pos = [(x + warp(x, y)[0], y + warp(x, y)[1]) for x, y in PREDICTED]
    px, py = PREDICTED[target]
    decoy = (px - 15.0, py + 12.0)  # closer than the true star, wrong way
    path = tmp_path / "decoy.jpg"
    synth.render_points(str(path), true_pos + [decoy], WIDTH, HEIGHT,
                        amps=[180.0] * len(true_pos) + [28.0])

    labels = star_labels(PREDICTED)
    labels[target]["mag"] = 0.0
    out, _, _ = verify.apply(str(path), labels, [])
    lab = out[target]
    tx, ty = true_pos[target]
    assert lab["status"] == "matched"
    assert np.hypot(lab["x"] - tx, lab["y"] - ty) < 2.0


def test_bright_star_matched_to_faint_blob_is_demoted(tmp_path):
    # A first-magnitude star whose only nearby source is far dimmer than
    # the frame's typical match is behind cloud: hidden, not matched.
    target = 4  # mid-frame
    true_pos = [(x + warp(x, y)[0], y + warp(x, y)[1]) for x, y in PREDICTED]
    amps = [180.0] * len(true_pos)
    amps[target] = 20.0  # only a dim smudge where the bright star should be
    path = tmp_path / "smudge.jpg"
    synth.render_points(str(path), true_pos, WIDTH, HEIGHT, amps=amps)

    labels = star_labels(PREDICTED)
    labels[target]["mag"] = -0.1
    out, _, meta = verify.apply(str(path), labels, [])
    assert out[target]["status"] == "hidden"
    assert meta["stars_hidden"] == 1


# A spot >100px from every PREDICTED star, so the DSO apertures see only
# sky (and the cluster tests' own members).
DSO_POS = (850.0, 250.0)


def dso_label(**over):
    lab = {"name": "Andromeda Galaxy (M31)", "x": DSO_POS[0], "y": DSO_POS[1],
           "mag": 3.6, "kind": "dso", "dso_type": "Gxy", "radius_px": 60.0}
    lab.update(over)
    return lab


def test_dso_over_empty_sky_is_hidden(warped_image):
    # The 2026-08-13 failure: WCS position exactly right, nothing there.
    path, _ = warped_image
    labels, _, meta = verify.apply(path, star_labels(PREDICTED) + [dso_label()], [])
    m31 = labels[-1]
    assert m31["status"] == "hidden"
    assert meta["dsos_hidden"] == 1
    assert meta["stars_hidden"] == 0  # DSOs don't inflate the star count
    # its position still follows the warp correction
    dx, dy = warp(*DSO_POS)
    assert np.hypot(m31["x"] - (DSO_POS[0] + dx),
                    m31["y"] - (DSO_POS[1] + dy)) < 6.0


def test_dso_with_diffuse_glow_stays_projected(tmp_path):
    true_pos = [(x + warp(x, y)[0], y + warp(x, y)[1]) for x, y in PREDICTED]
    dx, dy = warp(*DSO_POS)
    glow = (DSO_POS[0] + dx, DSO_POS[1] + dy, 25.0, 30.0)
    path = tmp_path / "glow.jpg"
    synth.render_points(str(path), true_pos, WIDTH, HEIGHT, blobs=[glow])

    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED) + [dso_label()], [])
    assert labels[-1]["status"] == "projected"
    assert meta["dsos_hidden"] == 0


def test_visible_cluster_stays_projected(tmp_path):
    # An open cluster's light is resolved member stars, not diffuse glow:
    # the core median barely moves, yet the cluster is plainly visible.
    true_pos = [(x + warp(x, y)[0], y + warp(x, y)[1]) for x, y in PREDICTED]
    dx, dy = warp(*DSO_POS)
    cx, cy = DSO_POS[0] + dx, DSO_POS[1] + dy
    members = [(cx, cy), (cx + 25, cy + 10), (cx - 20, cy + 18),
               (cx + 15, cy - 25), (cx - 28, cy - 12)]
    path = tmp_path / "cluster.jpg"
    synth.render_points(str(path), true_pos + members, WIDTH, HEIGHT,
                        amps=[180.0] * len(true_pos) + [100.0] * len(members))

    pleiades = dso_label(name="Pleiades (M45)", dso_type="OC", mag=1.6)
    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED) + [pleiades], [])
    assert labels[-1]["status"] == "projected"
    assert meta["dsos_hidden"] == 0


def test_cluster_over_empty_sky_is_hidden(warped_image):
    path, _ = warped_image
    pleiades = dso_label(name="Pleiades (M45)", dso_type="OC", mag=1.6)
    labels, _, meta = verify.apply(path, star_labels(PREDICTED) + [pleiades], [])
    assert labels[-1]["status"] == "hidden"
    assert meta["dsos_hidden"] == 1


def test_dso_without_catalog_size_uses_default_aperture(warped_image):
    path, _ = warped_image
    lab = dso_label()
    del lab["radius_px"]
    labels, _, meta = verify.apply(path, star_labels(PREDICTED) + [lab], [])
    assert labels[-1]["status"] == "hidden"


def test_dso_mostly_off_frame_gets_benefit_of_the_doubt(tmp_path):
    # Apertures mostly off-frame -> can't judge -> stays projected.
    path = tmp_path / "corner.jpg"
    synth.render_points(str(path), PREDICTED, WIDTH, HEIGHT)
    corner = dso_label(x=5.0, y=5.0)
    labels, _, meta = verify.apply(str(path), star_labels(PREDICTED) + [corner], [])
    assert labels[-1]["status"] == "projected"
    assert meta["dsos_hidden"] == 0


def test_field_fit_rejects_wild_outlier():
    # A consistent (10, -5) shift plus one wild vector: the affine clip
    # must discard the outlier instead of letting the TPS bend through it.
    matches = [(x, y, 10.0, -5.0)
               for x in (100.0, 600.0, 1100.0) for y in (100.0, 450.0, 800.0)]
    matches.append((650.0, 500.0, -40.0, 60.0))
    field, n_used = verify._fit_field(matches, 1200.0)
    assert n_used == len(matches) - 1
    dx, dy = field(650.0, 500.0)
    assert abs(dx - 10.0) < 1.5 and abs(dy + 5.0) < 1.5


def test_count_stars_on_starfield_and_starless_images(tmp_path):
    starry = tmp_path / "starry.jpg"
    synth.render_points(str(starry), PREDICTED, WIDTH, HEIGHT)
    n = verify.count_stars(str(starry))
    assert n >= len(PREDICTED) - 2  # JPEG may eat a marginal one

    gradient = tmp_path / "gradient.jpg"
    synth.render_gradient(str(gradient), WIDTH, HEIGHT)
    assert verify.count_stars(str(gradient)) < 5

    black = tmp_path / "black.jpg"
    synth.render_black(str(black), WIDTH, HEIGHT)
    assert verify.count_stars(str(black)) < 5

    assert verify.count_stars(str(tmp_path / "missing.jpg")) is None


def test_gate_normalizes_scale_before_counting(tmp_path, monkeypatch):
    """Real phone uploads are ~12MP with night-mode-fattened stars, and the
    isolation test is written in fixed pixels — 10px out is nothing on a
    4000px frame, so every real star failed it. Measured 2026-08-14 on
    Pixel 9 shots: the same photo counted 0 stars natively and 67 at
    1600px, and was rejected as "not a sky photo" despite solving in under
    6 seconds."""
    path = tmp_path / "stacked.jpg"
    points = [(x, y) for x in range(400, 3800, 420)
              for y in range(400, 2800, 420)]
    synth.render_points(str(path), points, width=4000, height=3000, sigma=5.0)

    assert verify.count_stars(str(path)) >= 10, "12MP frame must pass the gate"

    # ...and that is entirely down to the normalization: skip it and the
    # same image reads as starless, which is the bug this guards.
    monkeypatch.setattr(verify, "GATE_WIDTH", 99999)
    assert verify.count_stars(str(path)) < 10


def test_unreadable_image_returns_originals(tmp_path):
    labels = star_labels(PREDICTED)
    figures = [{"name": "F", "abbr": "F", "segments": [[0.0, 0.0, 1.0, 1.0]]}]
    out_labels, out_figures, meta = verify.apply(
        str(tmp_path / "missing.jpg"), labels, figures)
    assert out_labels == labels
    assert out_figures == figures
    assert meta["verified"] is False
    # no statuses invented for labels we could not check
    assert all("status" not in l for l in out_labels)


# ---- declining to correct at all (#85 follow-up) ----


def test_correct_false_leaves_every_label_exactly_where_it_was(warped_image):
    """After an anchor registration there was no star match, so pairing a
    label with the nearest peak is not evidence — every label finds *some*
    peak in a noisy night frame. Measured on a real anchored job, the fit
    reported 28 confident star matches and dragged every label by a median
    of 92px on a photo the solver could not solve at all."""
    path, _ = warped_image
    before = star_labels(PREDICTED)
    labels, _, meta = verify.apply(path, before, [], correct=False)

    assert meta["verified"] is True
    assert meta["corrected"] is False
    assert meta["field_matches"] == 0
    assert meta["stars_matched"] == 0
    # Not a pixel of movement, and nothing claimed to be hidden either:
    # "no peak nearby" would be a statement about the registration, not
    # about cloud.
    for original, out in zip(before, labels):
        assert (out["x"], out["y"]) == (original["x"], original["y"])
        assert out["status"] == "projected"


def test_correct_false_does_not_move_the_figures_either(warped_image):
    path, _ = warped_image
    figures = [{"name": "Orion", "segments": [[100.0, 100.0, 500.0, 400.0]]}]
    _, out_figures, _ = verify.apply(path, star_labels(PREDICTED), figures,
                                     correct=False)
    assert out_figures[0]["segments"] == [[100.0, 100.0, 500.0, 400.0]]


def test_correcting_is_still_the_default(warped_image):
    """The star-solve path must be untouched by the opt-out."""
    path, true_pos = warped_image
    labels, _, meta = verify.apply(path, star_labels(PREDICTED), [])
    assert meta["corrected"] is True
    assert all(l["status"] == "matched" for l in labels)
    for lab, (tx, ty) in zip(labels, true_pos):
        assert lab["x"] == pytest.approx(tx, abs=2)
        assert lab["y"] == pytest.approx(ty, abs=2)
