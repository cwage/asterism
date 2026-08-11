import pytest

from app import solver

# Realistic-enough rows in HYG column format (ra in hours). Includes rows
# load_catalog must filter out: the Sun, an unnamed star, and a faint one.
MINI_HYG = """id,proper,ra,dec,mag
1,Sol,0.0,0.0,-26.7
2,Sirius,6.752481,-16.716116,-1.44
3,Betelgeuse,5.919529,7.407063,0.45
4,Rigel,5.242298,-8.201638,0.18
5,Bellatrix,5.418851,6.349703,1.64
6,Alnilam,5.603559,-1.201919,1.69
7,Mintaka,5.533445,-0.299095,2.25
8,Saiph,5.795941,-9.669605,2.07
9,Vega,18.615649,38.783692,0.03
10,,5.679313,-1.942572,1.74
11,Faintstar,5.5,1.0,5.2
"""


@pytest.fixture()
def mini_catalog(tmp_path, monkeypatch):
    """Self-contained star catalog so fast tests don't need catalogs/hyg.csv."""
    (tmp_path / "hyg.csv").write_text(MINI_HYG)
    monkeypatch.setattr(solver, "CATALOG_DIR", str(tmp_path))
    solver._catalog_cache = None
    yield
    solver._catalog_cache = None
