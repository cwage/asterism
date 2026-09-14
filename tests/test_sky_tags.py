"""Feed sky hints use existing result data, never narration or off-frame objects."""

import pytest

from app import dso, sky_tags, solver


def _result(cons=(), stars=(), dsos=()):
    return {"constellations": [{"abbr": con} for con in cons],
            "labels": ([{"name": name, "kind": "star"} for name in stars]
                       + [{"name": name, "kind": "dso"} for name in dsos])}


@pytest.mark.parametrize(("cons", "stars", "expected"), [
    (["Sgr", "Sco"], [], ["Milky Way core"]),
    (["Sgr"], [], []),
    (["Sco"], [], []),
    (["Cru"], [], ["Southern Cross"]),
    ([], ["Vega", "Deneb", "Altair"], ["Summer Triangle"]),
    (["Lyr", "Cyg", "Aql"], [], []),  # figures alone don't make the triangle
])
def test_regions(cons, stars, expected):
    assert sky_tags.for_result(_result(cons, stars)) == expected


@pytest.mark.parametrize("missing", ["Vega", "Deneb", "Altair"])
def test_triangle_requires_all_three_non_hidden_stars(missing, mini_catalog):
    result = _result(stars=["Vega", "Deneb", "Altair"])
    next(label for label in result["labels"] if label["name"] == missing)["status"] = "hidden"
    assert "Summer Triangle" not in sky_tags.for_result(result)
    result["labels"] = [label for label in result["labels"] if label["name"] != missing]
    result["beyond"] = [{"name": missing, "kind": "star"}]
    assert "Summer Triangle" not in sky_tags.for_result(result)


@pytest.mark.parametrize(("catalog_id", "name"), sky_tags.SHOWPIECES)
def test_showpiece_names(catalog_id, name):
    result = _result(dsos=[dso.DISPLAY_NAMES[catalog_id]])
    assert sky_tags.for_result(result) == [name]
    result["labels"][0]["status"] = "hidden"
    assert sky_tags.for_result(result) == []


def test_two_tags_in_stable_priority_order():
    result = _result(["Cru", "Sco", "Sgr"], ["Altair", "Vega", "Deneb"],
                     list(dso.DISPLAY_NAMES.values()))
    assert sky_tags.for_result(result) == ["Milky Way core", "Summer Triangle"]
    result["constellations"] = []
    assert sky_tags.for_result(result) == ["Summer Triangle", "Andromeda Galaxy"]
    result["labels"].reverse()
    assert sky_tags.for_result(result) == ["Summer Triangle", "Andromeda Galaxy"]


def test_fallback_uses_brightest_non_hidden_star_not_first_figure(mini_catalog):
    result = _result(["Lyr", "Ori"], ["Betelgeuse", "Rigel", "Vega"])
    result["labels"][-1]["status"] = "hidden"
    assert sky_tags.for_result(result) == ["Orion"]
    result["labels"][-1]["status"] = "matched"
    assert sky_tags.for_result(result) == ["Lyra"]


def test_legacy_label_without_kind_and_bayer_name(mini_catalog):
    assert sky_tags.for_result({"labels": [{"name": "α Lup"}]}) == ["Lupus"]


def test_non_stars_do_not_supply_star_names(mini_catalog):
    result = _result(stars=["Deneb", "Altair"])
    result["labels"].append({"name": "Vega", "kind": "planet"})
    assert sky_tags.for_result(result) == []


@pytest.mark.parametrize("result", [{}, {"labels": None, "constellations": None},
    {"narration": {"caption": "Summer Triangle"},
     "beyond": [{"name": "Andromeda Galaxy (M31)", "kind": "dso"}]}])
def test_empty_legacy_results_do_not_invent_tags(result):
    assert sky_tags.for_result(result) == []


def test_missing_catalog_is_not_a_feed_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(solver, "CATALOG_DIR", str(tmp_path))
    monkeypatch.setattr(solver, "_catalog_cache", None)
    assert sky_tags.for_result(_result(stars=["Rigel"])) == []
