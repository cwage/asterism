"""Constellation lore (#123): the table covers the sky, and the two lines
shown are for the constellations whose brightest confirmed star is
brightest."""

from app import lore

CATALOG = {"Vega": "Lyr", "Altair": "Aql", "Deneb": "Cyg", "Albireo": "Cyg",
           "Sadr": "Cyg", "Rasalhague": "Oph"}
FIGURES = [{"name": "Cygnus", "abbr": "Cyg", "segments": []},
           {"name": "Lyra", "abbr": "Lyr", "segments": []},
           {"name": "Aquila", "abbr": "Aql", "segments": []},
           {"name": "Ophiuchus", "abbr": "Oph", "segments": []}]


def _star(name, mag, status="matched"):
    return {"name": name, "x": 1.0, "y": 1.0, "mag": mag, "kind": "star", "status": status}


def test_the_table_covers_every_constellation_with_a_sentence_that_names_it():
    assert len(lore.LORE) == 88
    for abbr, line in lore.LORE.items():
        assert line.endswith("."), abbr
        assert 8 <= len(line.split()) <= 30, abbr


def test_the_two_brightest_confirmed_constellations_get_a_line():
    labels = [_star("Vega", 0.03), _star("Altair", 0.76), _star("Deneb", 1.25),
              _star("Rasalhague", 2.08)]
    out = lore.annotate(FIGURES, labels, catalog=CATALOG)
    assert [e["abbr"] for e in out] == ["Lyr", "Aql"]
    assert out[0]["name"] == "Lyra" and out[0]["line"] == lore.LORE["Lyr"]


def test_hidden_stars_and_bodies_do_not_count_and_figures_are_required():
    labels = [_star("Vega", 0.03, status="hidden"), _star("Albireo", 3.05),
              _star("Rasalhague", 2.08),
              {"name": "Jupiter", "x": 1, "y": 1, "mag": -2.5, "kind": "planet"}]
    out = lore.annotate(FIGURES, labels, catalog=CATALOG)
    assert [e["abbr"] for e in out] == ["Oph", "Cyg"]   # Vega hidden: Lyra drops out
    # a constellation with a star but no figure drawn is not in frame enough
    assert lore.annotate([FIGURES[0]], labels, catalog=CATALOG) == [
        {"abbr": "Cyg", "name": "Cygnus", "line": lore.LORE["Cyg"]}]
    assert lore.annotate([], labels, catalog=CATALOG) == []
    assert lore.annotate(FIGURES, [], catalog=CATALOG) == []


def test_unverified_labels_still_count(mini_catalog):
    # no status at all: verification never ran, and the catalog's own
    # constellation column is what maps the star to its figure
    figures = [{"name": "Orion", "abbr": "Ori", "segments": []}]
    labels = [{"name": "Betelgeuse", "x": 1.0, "y": 1.0, "mag": 0.45, "kind": "star"}]
    out = lore.annotate(figures, labels)
    assert out and out[0]["abbr"] == "Ori"
