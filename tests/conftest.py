import pytest

from app import dso, solver

# Realistic-enough rows in HYG column format (ra in hours). Includes rows
# load_catalog must filter out: the Sun, an unnamed star with no Bayer
# designation, a faint one, and an unnamed secondary component — plus
# unnamed stars it must keep under their Bayer designation.
MINI_HYG = """id,proper,ra,dec,mag,bayer,con,comp
1,Sol,0.0,0.0,-26.7,,,
2,Sirius,6.752481,-16.716116,-1.44,Alp,CMa,1
3,Betelgeuse,5.919529,7.407063,0.45,Alp,Ori,1
4,Rigel,5.242298,-8.201638,0.18,Bet,Ori,1
5,Bellatrix,5.418851,6.349703,1.64,Gam,Ori,1
6,Alnilam,5.603559,-1.201919,1.69,Eps,Ori,1
7,Mintaka,5.533445,-0.299095,2.25,Del,Ori,1
8,Saiph,5.795941,-9.669605,2.07,Kap,Ori,1
9,Vega,18.615649,38.783692,0.03,Alp,Lyr,1
10,,5.679313,-1.942572,1.74,,,1
11,Faintstar,5.5,1.0,5.2,,,1
12,,14.698882,-47.388200,2.30,Alp,Lup,1
13,,8.158887,-47.336588,1.75,Gam-2,Vel,1
14,,13.398761,54.921822,3.95,Zet,UMa,2
"""


@pytest.fixture(autouse=True)
def calibrated_confidence_floors(monkeypatch):
    """Pin the #71 confidence floors to their calibrated defaults.

    MIN_LOGODDS/MIN_MATCHES are read from the environment at import time so
    they can be tuned per deployment. Without this, a developer or CI runner
    with SOLVE_MIN_LOGODDS set would see solver tests fail for reasons that
    have nothing to do with the solver."""
    monkeypatch.setattr(solver, "MIN_LOGODDS", solver.DEFAULT_MIN_LOGODDS)
    monkeypatch.setattr(solver, "MIN_MATCHES", solver.DEFAULT_MIN_MATCHES)


@pytest.fixture()
def mini_catalog(tmp_path, monkeypatch):
    """Self-contained star catalog so fast tests don't need catalogs/hyg.csv."""
    (tmp_path / "hyg.csv").write_text(MINI_HYG)
    monkeypatch.setattr(solver, "CATALOG_DIR", str(tmp_path))
    solver._catalog_cache = None
    yield
    solver._catalog_cache = None


# Real dso.csv rows (ra in hours, r1 = major axis in arcmin) plus rows
# load_catalog must filter: too faint, a dark nebula, and an anonymous
# non-Messier cluster.
MINI_DSO = """ra,dec,type,const,mag,name,id1,cat1,r1
3.79,24.117,OC,Tau,1.6,Pleiades,45,M,110
0.712306,41.2683,Gxy,And,3.6,,31,M,190.5
5.58811,-5.39083,OC+Neb,Ori,4,,42,M,90
8.67283,19.6719,OC,Cnc,3.1,Praesepe,44,M,95
12.4433,-63.0864,DN,Cru,0,Coalsack,99,C,420
7.28,-37.97,OC,Pup,2.1,,135,Col,50
5.5,32.55,OC,Aur,7.4,,37,M,15
"""


@pytest.fixture()
def mini_dso_catalog(tmp_path, monkeypatch):
    """Self-contained DSO catalog so fast tests don't need catalogs/dso.csv."""
    (tmp_path / "dso.csv").write_text(MINI_DSO)
    monkeypatch.setattr(dso, "CATALOG_DIR", str(tmp_path))
    dso._catalog_cache = None
    yield
    dso._catalog_cache = None
