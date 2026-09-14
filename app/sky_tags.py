"""Small, deterministic hints for browsing solved photos (#126).

Region names describe the field, not a detection of Milky Way light. Named
objects must be in the label list and not hidden; off-frame pointers never
qualify. Derive on read so retained solves benefit without being re-solved.
"""

from . import constellations, dso, solver

# Display order is deliberate: broad recognizable regions, then showpieces.
# Each region requires every constellation and every star in its row.
REGIONS: tuple[tuple[str, set[str], set[str]], ...] = (
    ("Milky Way core", {"Sgr", "Sco"}, set()),
    ("Summer Triangle", set(), {"Vega", "Deneb", "Altair"}),
    ("Southern Cross", {"Cru"}, set()),
)
SHOWPIECES = (
    ("M 31", "Andromeda Galaxy"),
    ("M 42", "Orion Nebula"),
    ("M 45", "Pleiades"),
    ("M 44", "Beehive Cluster"),
    ("M 6", "Butterfly Cluster"),
    ("M 7", "Ptolemy Cluster"),
)


def for_result(result: dict) -> list[str]:
    """At most two tags, or the constellation of the brightest visible
    catalog star when no rule applies. Empty/old results may yield none.

    The shared, cached HYG catalog supplies constellation membership for
    proper names as well as Bayer names, including in older job payloads
    which never stored that membership. No catalog fetch happens here.
    """
    labels = [label for label in result.get("labels") or []
              if label.get("status") != "hidden"]
    stars = {label.get("name") for label in labels
             if label.get("kind", "star") == "star"}
    dsos = {label.get("name") for label in labels
            if label.get("kind") == "dso"}
    figures = {figure.get("abbr")
               for figure in result.get("constellations") or []}
    tags = [name for name, required_cons, required_stars in REGIONS
            if required_cons <= figures and required_stars <= stars]
    tags.extend(name for catalog_id, name in SHOWPIECES
                if dso.DISPLAY_NAMES[catalog_id] in dsos)
    if tags:
        return tags[:2]
    if stars:
        try:
            catalog = solver.load_catalog()  # brightest first
        except FileNotFoundError:
            return []  # decoration must not require local sky-data setup
        for star in catalog:
            name = constellations.NAMES.get(star.get("con"))
            if star["name"] in stars and name:
                return [name]
    return []
