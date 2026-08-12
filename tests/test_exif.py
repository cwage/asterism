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
    # frame is 2*atan(36/54) = 67.38 deg, bracketed by 0.7x / 1.4x.
    path = tmp_path / "pixel.jpg"
    ex = synth.build_exif(f35mm=27, datetime_original="2021:07:30 23:52:43",
                          gps=(49.1415, 6.1170))
    Image.new("RGB", (64, 64)).save(path, exif=ex)

    info = exif.read_exif(path)
    fov = math.degrees(2 * math.atan(36.0 / 54.0))
    assert info["focal_35mm"] == 27.0
    assert info["fov_bounds"][0] == pytest.approx(fov * 0.7, rel=1e-6)
    assert info["fov_bounds"][1] == pytest.approx(fov * 1.4, rel=1e-6)
    assert info["datetime_original"] == "2021:07:30 23:52:43"
    assert info["lat"] == pytest.approx(49.1415, abs=1e-3)
    assert info["lon"] == pytest.approx(6.1170, abs=1e-3)


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


def test_strip_gps_raises_on_non_jpeg(tmp_path):
    # The upload path treats a strip failure on a GPS-bearing image as a
    # rejection (fail closed); a PNG must raise, not silently succeed.
    path = str(tmp_path / "gps.png")
    Image.new("RGB", (64, 64)).save(path,
                                    exif=synth.build_exif(gps=(49.1, 6.1)))
    with pytest.raises(Exception):
        exif.strip_gps(path)


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
