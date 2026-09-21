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


def test_planet_without_its_own_source_keeps_the_warp_correction(warped_image):
    path, _ = warped_image
    body = {"name": "Jupiter", "x": 590.0, "y": 430.0, "mag": -2.0,
            "kind": "planet"}
    labels, _, _ = verify.apply(path, star_labels(PREDICTED) + [body], [])
    jupiter = labels[-1]
    assert jupiter["status"] == "projected"
    dx, dy = warp(590.0, 430.0)
    assert np.hypot(jupiter["x"] - (590.0 + dx), jupiter["y"] - (430.0 + dy)) < 6.0


def test_planet_near_edge_snaps_to_dominant_source_without_moving_stars(tmp_path):
    # The Saturn failure: the planet is inside the frame, but its source
    # is further from the projection than the ordinary star snap allows.
    # A much fainter, closer source must not steal the label.
    path = tmp_path / "saturn.jpg"
    target = (985.0, 34.0)
    synth.render_points(str(path), PREDICTED + [target, (995.0, 9.0)], WIDTH, HEIGHT,
                        amps=[180.0] * (len(PREDICTED) + 1) + [25.0])
    body = {"name": "Saturn", "x": 1000.0, "y": 6.0, "mag": 0.6, "kind": "planet"}
    stars, figures, original = verify.apply(str(path), star_labels(PREDICTED), [])
    labels, out_figures, meta = verify.apply(str(path), [body] + star_labels(PREDICTED), [])
    assert labels[0]["status"] == "matched"
    assert np.hypot(labels[0]["x"] - target[0], labels[0]["y"] - target[1]) < 2.0
    assert labels[1:] == stars
    assert out_figures == figures
    assert meta == original  # planets must not inflate star counts or warp statistics
    assert body["x"] == 1000.0  # the input/cached ephemeris stays untouched


@pytest.mark.parametrize("points,amps", [
    ([], []),                                      # nothing detected
    ([(985.0, 34.0)], [25.0]),                      # too faint for a secure match
    ([(985.0, 34.0), (1015.0, 34.0)], [180., 160.]), # ambiguous bright sources
    ([(1035.0, 40.0)], [180.0]),                    # window corner, outside radius
])
def test_planet_keeps_projection_without_a_secure_source(tmp_path, points, amps):
    path = tmp_path / "uncertain-planet.jpg"
    synth.render_points(str(path), PREDICTED + points, WIDTH, HEIGHT,
                        amps=[180.0] * len(PREDICTED) + amps)
    body = {"name": "Saturn", "x": 1000.0, "y": 6.0, "mag": 0.6, "kind": "planet"}
    labels, _, _ = verify.apply(str(path), star_labels(PREDICTED) + [body], [])
    assert labels[-1]["status"] == "projected"
    assert labels[-1]["x"] == pytest.approx(body["x"], abs=2)
    assert labels[-1]["y"] == pytest.approx(body["y"], abs=2)


def test_planet_does_not_claim_a_matched_star(tmp_path):
    path = tmp_path / "near-star.jpg"
    synth.render_points(str(path), PREDICTED, WIDTH, HEIGHT)
    body = {"name": "Saturn", "x": 1035.0, "y": 130.0, "mag": 0.6, "kind": "planet"}
    labels, _, meta = verify.apply(str(path), [body] + star_labels(PREDICTED), [])
    assert labels[0]["status"] == "projected"
    assert meta["stars_matched"] == len(PREDICTED)


def test_planet_needs_verified_stars_and_moon_never_snaps(tmp_path):
    path = tmp_path / "bodies.jpg"
    synth.render_points(str(path), [(985.0, 34.0)] + PREDICTED, WIDTH, HEIGHT)
    body = {"name": "Saturn", "x": 1000.0, "y": 6.0, "mag": 0.6, "kind": "planet"}
    labels, _, _ = verify.apply(str(path), [body], [])
    assert labels[0]["status"] == "projected"
    moon = dict(body, name="Moon", kind="moon")
    labels, _, _ = verify.apply(str(path), star_labels(PREDICTED) + [moon], [])
    assert labels[-1]["status"] == "projected"


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


# ---- limiting magnitude (#122) ----

def _graded_field(seed=3, n=300, lo=2.0, hi=8.0):
    """Stars at least 30px apart with magnitudes spread over [lo, hi],
    rendered on the same 255-at-magnitude-3 scale as synth.render_starfield
    so a 12-ADU detection floor sits at magnitude 6.3."""
    rng = np.random.default_rng(seed)
    pts, mags = [], []
    while len(pts) < n:
        x, y = rng.uniform(40, WIDTH - 40), rng.uniform(40, HEIGHT - 40)
        if all((x - px) ** 2 + (y - py) ** 2 > 30 ** 2 for px, py in pts):
            pts.append((x, y))
            mags.append(float(rng.uniform(lo, hi)))
    amps = [255.0 * 10 ** (-0.4 * (m - 3.0)) for m in mags]
    return pts, mags, amps


def test_limiting_magnitude_reads_where_stars_stop_being_detected(tmp_path):
    pts, mags, amps = _graded_field()
    path = tmp_path / "deep.jpg"
    synth.render_points(str(path), pts, WIDTH, HEIGHT, amps=amps)
    bright = [(x, y, m) for (x, y), m in zip(pts, mags) if m <= 3.5]
    labels = [{"name": f"S{i}", "x": x, "y": y, "mag": m, "kind": "star"}
              for i, (x, y, m) in enumerate(bright)]
    deep = [(x, y, m) for (x, y), m in zip(pts, mags)]
    _, _, meta = verify.apply(str(path), labels, [], deep=deep)
    d = meta["depth"]
    assert 5.8 <= d["limiting_mag"] <= 6.8
    assert d["catalog_limited"] is False
    assert d["control_frac"] <= 0.15
    assert d["curve"][0][2] >= 0.9        # the bright end is all there
    assert d["curve"][-1][2] < 0.5         # and the walk ended in failure
    assert all(n >= verify.DEPTH_MIN_PER_BIN for _, n, _ in d["curve"])


def test_depth_is_catalog_limited_when_every_star_shows(tmp_path):
    pts, mags, amps = _graded_field(lo=2.0, hi=4.5)
    path = tmp_path / "shallow.jpg"
    synth.render_points(str(path), pts, WIDTH, HEIGHT, amps=amps)
    deep = [(x, y, m) for (x, y), m in zip(pts, mags)]
    _, _, meta = verify.apply(str(path), star_labels(pts[:12]), [], deep=deep)
    d = meta["depth"]
    assert d["catalog_limited"] is True
    # the faint edge of the last *full* bin: the catalog's own cut leaves
    # 4.0-4.5 partial, and a half-empty bin is not walked
    assert d["limiting_mag"] == 4.0
    assert d["baseline"] >= 0.9


def test_no_limit_when_the_catalog_points_at_empty_sky(tmp_path):
    path = tmp_path / "sparse.jpg"
    synth.render_points(str(path), PREDICTED, WIDTH, HEIGHT)
    rng = np.random.default_rng(5)
    deep = [(float(rng.uniform(60, WIDTH - 60)), float(rng.uniform(60, HEIGHT - 60)), float(m))
            for m in np.linspace(3.0, 7.9, 80)]
    _, _, meta = verify.apply(str(path), star_labels(PREDICTED), [], deep=deep)
    assert meta["depth"]["limiting_mag"] is None
    assert meta["depth"]["curve"]          # it looked, and found nothing
    assert meta["depth"]["baseline"] < verify.DEPTH_BASELINE_MIN


def test_a_third_of_the_sky_behind_a_treeline_does_not_move_the_limit(tmp_path):
    # Every star in the bottom third of the frame is missing, the way a
    # foreground hides them. The bright end is found at two thirds, the
    # threshold follows it, and the limit is still where the stars fade.
    pts, mags, amps = _graded_field()
    shown = [(p, a) for p, a in zip(pts, amps) if p[1] < HEIGHT * 2 / 3]
    path = tmp_path / "treeline.jpg"
    synth.render_points(str(path), [p for p, _ in shown], WIDTH, HEIGHT,
                        amps=[a for _, a in shown])
    bright = [(x, y, m) for (x, y), m in zip(pts, mags) if m <= 3.5]
    labels = [{"name": f"S{i}", "x": x, "y": y, "mag": m, "kind": "star"}
              for i, (x, y, m) in enumerate(bright)]
    deep = [(x, y, m) for (x, y), m in zip(pts, mags)]
    _, _, meta = verify.apply(str(path), labels, [], deep=deep)
    d = meta["depth"]
    assert 0.55 <= d["baseline"] <= 0.8
    assert 5.8 <= d["limiting_mag"] <= 6.8


def test_depth_is_skipped_without_a_deep_catalog(tmp_path):
    path = tmp_path / "clean.jpg"
    synth.render_points(str(path), PREDICTED, WIDTH, HEIGHT)
    _, _, meta = verify.apply(str(path), star_labels(PREDICTED), [])
    assert "depth" not in meta


def test_controls_step_around_catalog_stars(tmp_path):
    # Every tested star has a bright catalog neighbour exactly where the
    # first control offset would fall. A control that ignored the catalog
    # would count each neighbour as a chance hit and refuse to answer.
    pts, mags, amps = _graded_field(n=150)
    win_r = max(verify.DEPTH_WINDOW_MIN_PX,
                2 * max(verify.DEPTH_SNAP_MIN_PX, WIDTH * verify.DEPTH_SNAP_FRAC))
    partners = [(x + 3 * win_r, y) for x, y in pts if x + 3 * win_r < WIDTH - 40]
    all_pts = pts + partners
    all_mags = mags + [2.0] * len(partners)
    all_amps = amps + [255.0] * len(partners)
    path = tmp_path / "partnered.jpg"
    synth.render_points(str(path), all_pts, WIDTH, HEIGHT, amps=all_amps)
    bright = [(x, y, m) for (x, y), m in zip(all_pts, all_mags) if m <= 3.5]
    labels = [{"name": f"S{i}", "x": x, "y": y, "mag": m, "kind": "star"}
              for i, (x, y, m) in enumerate(bright)]
    deep = [(x, y, m) for (x, y), m in zip(all_pts, all_mags)]
    _, _, meta = verify.apply(str(path), labels, [], deep=deep)
    d = meta["depth"]
    assert d["control_frac"] <= 0.15
    assert 5.8 <= d["limiting_mag"] <= 6.8


def test_sparse_faint_bins_make_a_floor_and_say_so(tmp_path):
    # Plenty of stars to magnitude 5, three lonely ones at 6.4 and the
    # catalog's last star at 7.3: the 6.0-6.5 bin is full but too sparse
    # to test, so the answer is a floor at 5.0, flagged as sparse rather
    # than as the catalog running out.
    pts, mags, amps = _graded_field(n=200, lo=2.0, hi=5.0)
    extra = [(100.0, 100.0), (500.0, 500.0), (900.0, 700.0), (300.0, 800.0)]
    all_pts, all_mags = pts + extra, mags + [6.4, 6.4, 6.4, 7.3]
    all_amps = amps + [255.0 * 10 ** (-0.4 * (m - 3.0)) for m in (6.4, 6.4, 6.4, 7.3)]
    path = tmp_path / "sparse-faint.jpg"
    synth.render_points(str(path), all_pts, WIDTH, HEIGHT, amps=all_amps)
    deep = [(x, y, m) for (x, y), m in zip(all_pts, all_mags)]
    _, _, meta = verify.apply(str(path), star_labels(pts[:12]), [], deep=deep)
    d = meta["depth"]
    assert d["catalog_limited"] is True and d["faint_bins_sparse"] is True
    assert d["limiting_mag"] == 5.0
