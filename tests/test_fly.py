"""The fly-around page: its star data (fly.py) and what is served under
/fly. The data is every HYG star as a point in space, packed for the page;
the thing most worth pinning is that a solve's labels can be joined back to
it by name, because that join is all the page has to go on."""

import hashlib
import json
import os
import re
import struct

import pytest
from fastapi.testclient import TestClient

from app import db, fly, main, solver

# HYG rows with the columns the page's data needs (ra in hours, x/y/z in
# parsecs). The Sun; three named stars; a bright star known only by its
# Bayer letter; an unnamed secondary, which gets no name; a star with no
# colour index; one at HYG's placeholder distance; and a row with no
# position at all, which cannot be drawn and is dropped.
FLY_HYG = """id,hip,proper,ra,dec,dist,mag,absmag,ci,x,y,z,bayer,con,comp
0,,Sol,0.0,0.0,0.0,-26.7,4.85,0.656,0.000005,0.0,0.0,,,1
1,32349,Sirius,6.752481,-16.716116,2.6371,-1.44,1.454,0.009,-0.494323,2.476731,-0.758485,Alp,CMa,1
2,27989,Betelgeuse,5.919529,7.407063,152.6718,0.45,-5.469,1.5,3.189893,151.365776,19.686185,Alp,Ori,1
3,24436,Rigel,5.242298,-8.201638,264.5503,0.18,-6.933,-0.03,51.586445,256.704263,-37.740487,Bet,Ori,1
4,71860,,14.698882,-47.388200,142.4501,2.30,-3.468,-0.154,-73.337,-62.416,-104.841,Alp,Lup,1
5,65378,,13.398761,54.921822,26.3089,3.95,1.849,0.057,-14.097,-5.385,21.532,Zet,UMa,2
6,1001,Nocolour,1.0,10.0,50.0,5.5,2.005,,47.563,12.745,8.682,,,1
7,1002,Faraway,2.0,20.0,100000.0,6.0,-14.0,0.5,81379.768,46984.631,34202.014,,,1
8,1003,Nowhere,3.0,30.0,10.0,5.0,5.0,0.5,,,,,,1
"""

# Stellarium's format: abbreviation, pair count, then that many HIP pairs.
# The second figure has a pair with a star the catalog lacks.
FLY_LINES = """Ori 1 27989 24436
CMa 2 32349 99999 32349 27989
"""

JOB = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def sky(tmp_path, monkeypatch):
    """A catalog directory and a data directory of this test's own."""
    catalogs = tmp_path / "catalogs"
    catalogs.mkdir()
    (catalogs / "hyg.csv").write_text(FLY_HYG)
    (catalogs / "constellations.fab").write_text(FLY_LINES)
    monkeypatch.setattr(solver, "CATALOG_DIR", str(catalogs))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path / "data"))
    solver._catalog_cache = None
    yield catalogs
    solver._catalog_cache = None


def _unpack(packed, i):
    return struct.unpack_from("<5f", packed, i * 20)


def test_stars_are_brightest_first_and_the_sun_is_first_of_all(sky):
    packed, catalog = fly.build(str(sky))
    assert catalog["count"] == 8          # the row with no position is gone
    assert len(packed) == 8 * 20          # five float32 each
    assert catalog["names"]["Sun"] == 0
    # Sirius, Rigel, Betelgeuse, in that order after the Sun.
    assert [catalog["names"][n] for n in ("Sirius", "Rigel", "Betelgeuse")] == [1, 2, 3]


def test_each_star_is_its_position_its_absolute_magnitude_and_its_colour(sky):
    packed, catalog = fly.build(str(sky))
    x, y, z, absmag, ci = _unpack(packed, catalog["names"]["Sirius"])
    assert (x, y, z) == pytest.approx((-0.494323, 2.476731, -0.758485), abs=1e-5)
    assert absmag == pytest.approx(1.454, abs=1e-5)
    assert ci == pytest.approx(0.009, abs=1e-5)
    # No colour recorded: drawn as a white-ish star rather than dropped.
    assert _unpack(packed, catalog["names"]["Nocolour"])[4] == pytest.approx(fly.DEFAULT_CI)


def test_a_star_with_no_usable_parallax_stays_at_the_placeholder_distance(sky):
    packed, catalog = fly.build(str(sky))
    x, y, z, _, _ = _unpack(packed, catalog["names"]["Faraway"])
    assert (x * x + y * y + z * z) ** 0.5 == pytest.approx(100000, rel=1e-3)


def test_names_are_the_ones_the_solver_labels_with(sky):
    """A label reaches the page as a name and a pixel; the name is the join.
    Whatever the solver can call a star, the page's data must know."""
    _, catalog = fly.build(str(sky))
    labelled = {s["name"] for s in solver.load_catalog()}
    assert labelled, "the fixture should have stars the solver would label"
    assert labelled <= set(catalog["names"])
    assert "α Lup" in catalog["names"]            # unnamed, by Bayer letter
    assert "ζ UMa" not in catalog["names"]        # an unnamed secondary
    assert "Sol" not in catalog["names"]


def test_proper_names_are_told_apart_from_bayer_designations(sky):
    _, catalog = fly.build(str(sky))
    assert {"Sun", "Sirius", "Betelgeuse", "Rigel"} <= set(catalog["proper"])
    assert "α Lup" not in catalog["proper"]


def test_figures_are_pairs_of_star_indices(sky):
    _, catalog = fly.build(str(sky))
    names = catalog["names"]
    assert catalog["lines"]["Ori"] == [names["Betelgeuse"], names["Rigel"]]
    # The pair with a star the catalog lacks is dropped; the other stays.
    assert catalog["lines"]["CMa"] == [names["Sirius"], names["Betelgeuse"]]


def test_no_line_set_means_no_figures_not_no_data(sky):
    (sky / "constellations.fab").unlink()
    _, catalog = fly.build(str(sky))
    assert catalog["lines"] == {}
    assert catalog["count"] == 8


# ---- the cache under DATA_DIR/fly ----

def test_files_are_built_once_and_then_reused(sky, monkeypatch):
    built = []
    real = fly.build
    monkeypatch.setattr(fly, "build", lambda d: built.append(d) or real(d))
    stars_path, catalog_path = fly.files()
    assert os.path.dirname(stars_path) == os.path.join(db.DATA_DIR, "fly")
    assert fly.files() == (stars_path, catalog_path)
    assert len(built) == 1
    with open(stars_path, "rb") as f:
        packed = f.read()
    with open(catalog_path) as f:
        catalog = json.load(f)
    assert len(packed) == catalog["count"] * 20
    # The version is what the page puts in the star file's URL, so it has
    # to change exactly when the file does.
    assert catalog["version"] == hashlib.sha256(packed).hexdigest()[:12]


def test_a_changed_catalog_is_rebuilt(sky):
    _, catalog_path = fly.files()
    with open(catalog_path) as f:
        before = json.load(f)
    hyg = sky / "hyg.csv"
    hyg.write_text(FLY_HYG + "9,1004,Newstar,4.0,40.0,20.0,3.0,1.495,0.3,7.66,13.268,12.856,,,1\n")
    _, catalog_path = fly.files()
    with open(catalog_path) as f:
        after = json.load(f)
    assert after["count"] == before["count"] + 1
    assert "Newstar" in after["names"]
    assert after["version"] != before["version"]


def test_a_rewrite_of_the_same_length_in_the_same_second_is_still_a_change(sky):
    hyg = sky / "hyg.csv"
    before = os.stat(hyg)
    _, catalog_path = fly.files()
    # Same number of bytes, one name different, a millisecond later.
    hyg.write_text(FLY_HYG.replace("Nocolour", "Recolour"))
    os.utime(hyg, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
    after = os.stat(hyg)
    assert after.st_size == before.st_size
    assert int(after.st_mtime) == int(before.st_mtime)
    _, catalog_path = fly.files()
    with open(catalog_path) as f:
        names = json.load(f)["names"]
    assert "Recolour" in names and "Nocolour" not in names


def test_a_half_written_cache_is_not_trusted(sky):
    stars_path, _ = fly.files()
    # As after a crash between writing the stars and recording what they
    # were built from.
    os.unlink(os.path.join(os.path.dirname(stars_path), "built.json"))
    with open(stars_path, "wb") as f:
        f.write(b"junk")
    stars_path, _ = fly.files()
    assert os.path.getsize(stars_path) == 8 * 20


def test_no_catalog_means_no_files(sky):
    (sky / "hyg.csv").unlink()
    assert fly.files() is None


# ---- what is served under /fly ----

@pytest.fixture()
def client(sky):
    return TestClient(main.app)


def test_the_page_is_served_and_loads_its_script_by_absolute_path(client):
    r = client.get("/fly")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # /fly has no trailing slash, so a relative script path would resolve
    # against the site root and miss.
    assert 'src="/fly/main.js"' in r.text
    assert '"/fly/vendor/three/three.module.js"' in r.text
    assert "og:image" not in r.text


def test_a_share_link_unfurls_with_the_solves_card(client):
    r = client.get(f"/fly?job={JOB}")
    assert r.status_code == 200
    assert f'/jobs/{JOB}/card"' in r.text
    assert "og:title" in r.text


def test_a_job_that_is_not_an_id_gets_the_plain_page(client):
    r = client.get('/fly?job="><script>alert(1)</script>')
    assert r.status_code == 200
    assert "og:image" not in r.text
    assert "alert(1)" not in r.text


@pytest.mark.parametrize("path", [
    "/fly/main.js", "/fly/fit.mjs",
    "/fly/vendor/three/three.module.js", "/fly/vendor/three/three.core.js",
])
def test_the_pages_scripts_are_served_as_javascript(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_the_script_imports_resolve_to_files_that_are_there(client):
    """The page is not run by these tests. This at least catches a renamed
    or missing module: every relative import in its scripts is fetched."""
    for script in ("/fly/main.js", "/fly/vendor/three/three.module.js"):
        text = client.get(script).text
        modules = set(re.findall(r"""from\s+['"](\./[^'"]+)['"]""", text))
        assert modules, script
        for module in modules:
            base = script.rsplit("/", 1)[0]
            assert client.get(f"{base}/{module[2:]}").status_code == 200, module


def test_the_catalog_is_fetched_afresh_and_names_the_star_files_version(client):
    r = client.get("/fly/catalog.json")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    catalog = r.json()
    assert catalog["count"] == 8 and catalog["version"]

    r = client.get(f"/fly/stars.bin?v={catalog['version']}")
    assert r.status_code == 200
    assert "immutable" in r.headers["cache-control"]
    assert len(r.content) == catalog["count"] * 20
    assert hashlib.sha256(r.content).hexdigest()[:12] == catalog["version"]


def test_the_data_says_so_when_the_catalog_has_not_been_fetched(client, sky):
    (sky / "hyg.csv").unlink()
    assert client.get("/fly/catalog.json").status_code == 503
    assert client.get("/fly/stars.bin").status_code == 503


def test_head_is_answered_like_get(client):
    assert client.head("/fly").status_code == 200
    assert client.head("/fly/catalog.json").status_code == 200
    assert client.head("/fly/main.js").status_code == 200


@pytest.mark.parametrize("path", [
    "/fly/../index.html", "/fly/%2e%2e/index.html", "/fly/..%2f..%2fapp/main.py",
    "/fly/vendor/../../index.html",
])
def test_nothing_outside_the_pages_own_directory_is_served(client, path):
    r = client.get(path)
    assert "import asyncio" not in r.text             # app/main.py
    assert "<title>asterism</title>" not in r.text    # the homepage
