import math

import numpy as np
import pytest
from PIL import Image

from app import exif
from tests import synth


def test_gps_to_degrees_north():
    assert exif._gps_to_degrees((49, 8, 29.5), "N") == pytest.approx(49.14153, abs=1e-4)


def test_gps_to_degrees_south_west_negate():
    assert exif._gps_to_degrees((30, 0, 0), "S") == -30.0
    assert exif._gps_to_degrees((100, 30, 0), "W") == -100.5


def test_gps_to_degrees_garbage():
    assert exif._gps_to_degrees(None, "N") is None
    assert exif._gps_to_degrees(("x", 0, 0), "N") is None
    assert exif._gps_to_degrees((1,), "N") is None


def test_no_exif_falls_back_to_default_bounds(tmp_path):
    path = tmp_path / "plain.jpg"
    Image.new("RGB", (320, 240)).save(path)
    info = exif.read_exif(path)
    assert info["fov_bounds"] == exif.DEFAULT_FOV_BOUNDS
    assert info["focal_35mm"] is None
    assert info["datetime_original"] is None
    assert info["lat"] is None and info["lon"] is None
    assert info["heading"] is None and info["heading_ref"] is None
    assert (info["width"], info["height"]) == (320, 240)


def test_focal35_derives_fov_bounds(tmp_path):
    # A Pixel 4a writes FocalLengthIn35mmFilm=27; horizontal FOV for a 36mm
    # frame is 2*atan(36/54) = 67.38 deg, bracketed by 0.35x / 1.2x.
    path = tmp_path / "pixel.jpg"
    ex = synth.build_exif(f35mm=27, datetime_original="2021:07:30 23:52:43",
                          gps=(49.1415, 6.1170))
    Image.new("RGB", (64, 64)).save(path, exif=ex)

    info = exif.read_exif(path)
    fov = math.degrees(2 * math.atan(36.0 / 54.0))
    assert info["focal_35mm"] == 27.0
    assert info["fov_bounds"][0] == pytest.approx(fov * 0.35, rel=1e-6)
    assert info["fov_bounds"][1] == pytest.approx(fov * 1.2, rel=1e-6)
    assert info["datetime_original"] == "2021:07:30 23:52:43"
    assert info["lat"] == pytest.approx(49.1415, abs=1e-3)
    assert info["lon"] == pytest.approx(6.1170, abs=1e-3)


def test_portrait_frame_narrows_the_fov_estimate(tmp_path):
    """Portrait pixels put the sensor's SHORT side across the image width,
    so 36mm/f35 overstates the field by the aspect ratio. The prod ultrawide
    job of 2026-08-18: f35=12 read as 112.6 deg — over solver.MAX_EXIF_FIELD,
    which dropped the only scale tier containing the answer — when the
    portrait frame truly spans ~97 deg across."""
    path = tmp_path / "portrait.jpg"
    Image.new("RGB", (768, 1024)).save(path, exif=synth.build_exif(f35mm=12))
    info = exif.read_exif(path)
    fov = math.degrees(2 * math.atan((36.0 * 768 / 1024) / 24.0))  # ~96.9
    assert info["fov_deg"] == pytest.approx(fov, rel=1e-6)
    assert info["fov_bounds"][0] == pytest.approx(fov * 0.35, rel=1e-6)
    assert info["fov_bounds"][1] == pytest.approx(fov * 1.2, rel=1e-6)

    # The same lens held landscape keeps the full 36mm width.
    land = tmp_path / "landscape.jpg"
    Image.new("RGB", (1024, 768)).save(land, exif=synth.build_exif(f35mm=12))
    wide = math.degrees(2 * math.atan(36.0 / 24.0))
    assert exif.read_exif(land)["fov_deg"] == pytest.approx(wide, rel=1e-6)


def test_bracket_reaches_below_a_2x_sensor_crop(tmp_path):
    """The Pixel 9 case (measured 2026-08-14): EXIF says 24mm-equivalent,
    implying ~74 deg, but the saved frame is a 2x crop of the 50MP sensor
    and truly spans 38.4 deg. The bracket has to reach that far down or the
    quick pass cannot solve the photo at all."""
    path = tmp_path / "cropped.jpg"
    Image.new("RGB", (64, 64)).save(path, exif=synth.build_exif(f35mm=24))
    lo, hi = exif.read_exif(path)["fov_bounds"]
    # 38.4 is measured, not derived: it comes from the solved WCS of the
    # real photos, so it stays a literal even if the estimator changes.
    assert lo <= 38.4 <= hi, f"true field 38.4 deg outside bracket {lo:.1f}-{hi:.1f}"
    # The estimate itself must stay inside too, for uncropped shots.
    estimate = math.degrees(2 * math.atan(36.0 / (2 * 24)))
    assert lo <= estimate <= hi


def test_exposure_time_read_for_the_satellite_window(tmp_path):
    # Astro-mode shots record long per-frame exposures; that window is what
    # satellite crossings (#11) are computed over.
    path = tmp_path / "astro.jpg"
    Image.new("RGB", (64, 64)).save(
        path, exif=synth.build_exif(exposure_seconds=16))
    assert exif.read_exif(path)["exposure_seconds"] == 16.0

    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (64, 64)).save(plain)
    assert exif.read_exif(plain)["exposure_seconds"] is None


def test_exposure_time_survives_a_raw_rational_pair(tmp_path):
    """A RATIONAL can come back from Pillow as a bare (num, den) tuple
    rather than an IFDRational. float() on a tuple raises, and the exposure
    was silently dropped — which shrinks the satellite window to its
    one-second default without anything saying so."""
    path = tmp_path / "rational.jpg"
    ex = Image.Exif()
    ex.get_ifd(synth.EXIF_IFD)[synth.TAG_EXPOSURE_TIME] = (16, 1)
    Image.new("RGB", (64, 64)).save(path, exif=ex)
    assert exif.read_exif(path)["exposure_seconds"] == 16.0

    # A zero denominator is malformed, not a crash.
    bad = tmp_path / "bad.jpg"
    ex2 = Image.Exif()
    ex2.get_ifd(synth.EXIF_IFD)[synth.TAG_EXPOSURE_TIME] = (16, 0)
    Image.new("RGB", (64, 64)).save(bad, exif=ex2)
    assert exif.read_exif(bad)["exposure_seconds"] is None


def test_southern_western_gps_signs(tmp_path):
    path = tmp_path / "south.jpg"
    ex = synth.build_exif(gps=(-33.87, -151.21))
    Image.new("RGB", (64, 64)).save(path, exif=ex)
    info = exif.read_exif(path)
    assert info["lat"] == pytest.approx(-33.87, abs=1e-3)
    assert info["lon"] == pytest.approx(-151.21, abs=1e-3)


def test_heading_read_from_gps_ifd(tmp_path):
    # Pixel phones write GPSImgDirection (ref 'M') even in shots where they
    # drop lat/lon — the #7 fallback depends on catching it.
    path = tmp_path / "heading.jpg"
    ex = synth.build_exif(heading=(171.5, "M"))
    Image.new("RGB", (64, 64)).save(path, exif=ex)
    info = exif.read_exif(path)
    assert info["heading"] == pytest.approx(171.5)
    assert info["heading_ref"] == "M"
    assert info["lat"] is None and info["lon"] is None


def test_strip_gps_removes_location_keeps_rest(tmp_path):
    path = str(tmp_path / "gps.jpg")
    ex = synth.build_exif(f35mm=27, datetime_original="2021:07:30 23:52:43",
                          gps=(49.1415, 6.1170))
    Image.new("RGB", (64, 64), (30, 40, 50)).save(path, exif=ex, quality=92)
    with Image.open(path) as img:
        before = np.asarray(img).copy()

    assert exif.strip_gps(path) is True

    info = exif.read_exif(path)
    assert info["lat"] is None and info["lon"] is None
    # the fields the pipeline needs survive
    assert info["focal_35mm"] == 27.0
    assert info["datetime_original"] == "2021:07:30 23:52:43"
    # lossless: pixel data untouched, not a re-encode
    with Image.open(path) as img:
        after = np.asarray(img).copy()
    assert np.array_equal(before, after)


def test_strip_gps_falls_back_when_piexif_cannot_re_encode(tmp_path):
    """piexif is fragile on real EXIF: a float-valued ExposureTime makes it
    raise UnboundLocalError re-encoding the block, which used to reject the
    upload outright. A library that cannot parse a file is not a reason to
    refuse it, so Pillow finishes the job — lossy, and only on this path."""
    path = str(tmp_path / "float-exposure.jpg")
    ex = Image.Exif()
    ifd = ex.get_ifd(synth.EXIF_IFD)
    ifd[synth.TAG_EXPOSURE_TIME] = 10.0          # a bare float, not a rational
    ifd[synth.TAG_FOCAL_35MM] = 39
    gps = ex.get_ifd(synth.GPS_IFD)
    gps[1], gps[2] = "N", synth._deg_to_dms(36.16)
    gps[3], gps[4] = "W", synth._deg_to_dms(86.78)
    Image.new("RGB", (64, 64)).save(path, exif=ex, quality=92)

    assert exif.read_exif(path)["lat"] == pytest.approx(36.16, abs=0.01)
    with pytest.raises(Exception):
        exif._strip_gps_piexif(path)             # the preferred path is out

    assert exif.strip_gps(path) is True
    assert exif.has_location(path) is False
    assert exif.read_exif(path)["focal_35mm"] == 39.0


def test_a_png_with_gps_still_ends_up_without_it(tmp_path):
    """piexif handles no format but JPEG and TIFF. What matters is the file
    afterwards, not which library got there."""
    path = str(tmp_path / "gps.png")
    Image.new("RGB", (64, 64)).save(path,
                                    exif=synth.build_exif(gps=(49.1, 6.1)))
    exif.strip_gps(path)
    assert exif.has_location(path) is False


def test_has_location_never_raises_on_junk(tmp_path):
    """It answers "is this safe to serve", so an unreadable file is not a
    leak and must not become a 500 on the upload path."""
    path = str(tmp_path / "not-an-image.jpg")
    open(path, "wb").write(b"certainly not a jpeg")
    assert exif.has_location(path) is False
    assert exif.has_location(str(tmp_path / "missing.jpg")) is False


def test_strip_gps_no_gps_is_a_noop(tmp_path):
    path = str(tmp_path / "nogps.jpg")
    Image.new("RGB", (64, 64)).save(path, exif=synth.build_exif(f35mm=27))
    assert exif.strip_gps(path) is False
    assert exif.read_exif(path)["focal_35mm"] == 27.0


def test_public_exif_rounds_coordinates():
    from app.main import _public_exif
    out = _public_exif({"lat": 49.14153, "lon": -6.11701, "focal_35mm": 27.0,
                        "datetime_original": "2021:07:30 23:52:43"})
    assert out["lat"] == 49.1
    assert out["lon"] == -6.1
    # everything else passes through, and the original dict isn't mutated
    assert out["focal_35mm"] == 27.0
    assert _public_exif({"lat": None, "lon": None})["lat"] is None
    assert _public_exif(None) is None


# --- 35mm equivalent derived from sensor width (#70) ---

# The Canon 5DS upload that motivated this: 8688px across a 36mm sensor at
# 2413.333 px/cm, 24mm lens. No FocalLengthIn35mmFilm anywhere in the file.
CANON_5DS = {"focal_length": 24.0, "focal_plane_x_res": 2413.333344,
             "focal_plane_unit": 3}


def _write(tmp_path, name, size, **exif_kwargs):
    path = tmp_path / name
    Image.new("RGB", size).save(path, exif=synth.build_exif(**exif_kwargs))
    return path


def test_full_frame_focal_length_derives_its_own_equivalent(tmp_path):
    path = _write(tmp_path, "canon.jpg", (8688, 5672), **CANON_5DS)
    info = exif.read_exif(path)
    # 8688 / 2413.333 px per cm = 3.6cm = full frame, so 24mm stays 24mm.
    assert info["focal_35mm"] == pytest.approx(24.0, abs=0.01)
    assert info["focal_35mm_source"] == "sensor_width"
    # ~73.7 deg horizontal, bracketed the same way as a phone's tag.
    assert info["fov_bounds"] == pytest.approx((25.8, 88.5), abs=0.1)


def test_crop_sensor_scales_the_equivalent_up(tmp_path):
    # APS-C: 22.3mm wide, so a 24mm lens frames like ~38.7mm full frame.
    path = _write(tmp_path, "apsc.jpg", (6000, 4000), focal_length=24.0,
                  focal_plane_x_res=6000 / 2.23, focal_plane_unit=3)
    info = exif.read_exif(path)
    assert info["focal_35mm"] == pytest.approx(38.7, abs=0.2)


def test_inch_resolution_unit_is_handled(tmp_path):
    # Same full-frame geometry expressed in the other common unit.
    path = _write(tmp_path, "inch.jpg", (8688, 5672), focal_length=24.0,
                  focal_plane_x_res=8688 / (36.0 / 25.4), focal_plane_unit=2)
    info = exif.read_exif(path)
    assert info["focal_35mm"] == pytest.approx(24.0, abs=0.01)


def test_the_35mm_tag_still_wins_when_present(tmp_path):
    # A file carrying both must not be re-derived: the manufacturer's own
    # equivalent accounts for crops the focal plane tags know nothing about.
    path = _write(tmp_path, "both.jpg", (4000, 3000), f35mm=24,
                  focal_length=4.5, focal_plane_x_res=4000 / 0.94,
                  focal_plane_unit=3)
    info = exif.read_exif(path)
    assert info["focal_35mm"] == 24.0
    assert info["focal_35mm_source"] == "exif_35mm"


def test_focal_length_without_focal_plane_tags_is_not_guessed(tmp_path):
    # A bare focal length says nothing without a sensor size to scale it by.
    path = _write(tmp_path, "bare.jpg", (4000, 3000), focal_length=24.0)
    info = exif.read_exif(path)
    assert info["focal_35mm"] is None
    assert info["focal_35mm_source"] is None
    assert info["fov_bounds"] == exif.DEFAULT_FOV_BOUNDS


def test_implausible_sensor_width_is_rejected(tmp_path):
    # The resize trap: focal plane tags describe the original capture, so a
    # downscaled export computes a sensor a fraction of its real width. Better
    # to fall back to the generic tiers than to hint a field that is wrong by
    # the resize factor.
    path = _write(tmp_path, "resized.jpg", (800, 600), focal_length=24.0,
                  focal_plane_x_res=2413.333344, focal_plane_unit=3)
    info = exif.read_exif(path)
    assert info["focal_35mm"] is None
    assert info["fov_bounds"] == exif.DEFAULT_FOV_BOUNDS


def test_garbage_focal_plane_values_do_not_raise(tmp_path):
    for bad in (0, -5, "banana"):
        path = _write(tmp_path, f"bad{str(bad)[:3]}.jpg", (6000, 4000),
                      focal_length=24.0, focal_plane_x_res=bad,
                      focal_plane_unit=3)
        assert exif.read_exif(path)["focal_35mm"] is None


def test_unknown_resolution_unit_is_ignored(tmp_path):
    path = _write(tmp_path, "unit1.jpg", (6000, 4000), focal_length=24.0,
                  focal_plane_x_res=2413.333344, focal_plane_unit=1)
    assert exif.read_exif(path)["focal_35mm"] is None


def test_resize_detected_by_stored_dimensions_is_refused(tmp_path):
    # Resized to 6000px wide, but PixelXDimension still says 8688 and the
    # density still describes the original capture. The derived sensor would
    # be 24.9mm — inside the plausible range, so only the disagreement
    # between stored and actual width catches it. Left unguarded, this hints
    # ~35mm equivalent for a 24mm lens, a bracket that excludes the truth.
    path = _write(tmp_path, "resized-tagged.jpg", (6000, 3917),
                  pixel_x_dimension=8688, **CANON_5DS)
    info = exif.read_exif(path)
    assert info["focal_35mm"] is None
    assert info["fov_bounds"] == exif.DEFAULT_FOV_BOUNDS


def test_untouched_file_with_matching_dimensions_still_derives(tmp_path):
    path = _write(tmp_path, "tagged.jpg", (8688, 5672),
                  pixel_x_dimension=8688, **CANON_5DS)
    assert exif.read_exif(path)["focal_35mm"] == pytest.approx(24.0, abs=0.01)


def test_a_crop_narrows_the_hint_rather_than_breaking_it(tmp_path):
    # Cropping leaves pixel density untouched, so the arithmetic reports the
    # sensor extent actually kept: half the width of a full frame reads as
    # 18mm, i.e. 24mm behaving like 48mm — which is what the crop really did.
    path = _write(tmp_path, "cropped.jpg", (4344, 2836),
                  pixel_x_dimension=4344, **CANON_5DS)
    info = exif.read_exif(path)
    assert info["focal_35mm"] == pytest.approx(48.0, abs=0.1)
    assert info["focal_35mm_source"] == "sensor_width"
