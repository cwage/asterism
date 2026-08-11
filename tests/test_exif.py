import math

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
