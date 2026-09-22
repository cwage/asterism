"""Solve-rate benchmark: run the solver over a directory of real photos,
and save the numbers so a threshold change can be measured (#99).

A bench run records what the solver actually produced per photo — the
log-odds and match count of every attempt, the pre-solve star count, the
tier that won, the seconds burned — not just pass or fail. That is what
makes `sweep` possible: the acceptance gate and the star precheck are
applied to numbers the run already has, so candidate thresholds can be
tried over a saved run in a second instead of hours.

Usage (in the container):

    python -u -m app.bench /photos --sample 40 --json /photos/base.json
    python -m app.bench --sweep /photos/base.json --logodds 20,25,30
    python -m app.bench --compare /photos/base.json /photos/after.json

The corpus is an astrophotography dataset, a different population from
casual phone uploads: it bounds a threshold question, it does not settle
one. `stats.solve_history` is the other half of that (#148).
"""

import argparse
import json
import os
import random
import sys
import tempfile
import time

from . import exif, solver, thresholds, verify

EXTS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff"}


def photo_list(photo_dir, sample=None, seed=0):
    """The corpus, or a deterministic sample of it. Sorted first so the
    sample depends on the seed and not on readdir order — two runs on
    two machines have to be comparing the same photos."""
    photos = sorted(f for f in os.listdir(photo_dir)
                    if os.path.splitext(f)[1].lower() in EXTS)
    if sample and sample < len(photos):
        photos = sorted(random.Random(seed).sample(photos, sample))
    return photos


def run_one(path, quick=False):
    """One photo, with the numbers the thresholds are argued from."""
    name = os.path.basename(path)
    try:
        info = exif.read_exif(path)
    except Exception as e:
        return {"name": name, "success": False, "error": str(e), "attempts": []}
    # The precheck the worker runs before the solver is invoked at all, so
    # PRECHECK_MIN_STARS can be swept alongside the acceptance gate.
    stars = verify.count_stars(path)
    with tempfile.TemporaryDirectory() as out_dir:
        result = solver.solve_tiered(path, out_dir, info, quick=quick)
    return {
        "name": name,
        "success": bool(result["success"]),
        "stars_detected": stars,
        "exif_fov_deg": info.get("fov_deg"),
        "total_seconds": result.get("total_seconds"),
        "attempts": [
            {"fov_bounds": a.get("fov_bounds"), "seconds": a.get("seconds"),
             "success": a.get("success"), "timed_out": a.get("timed_out"),
             "low_confidence": a.get("low_confidence"), "match": a.get("match")}
            for a in result.get("attempts", [])
        ],
    }


def run(photo_dir, sample=None, seed=0, quick=False, out_json=None):
    photos = photo_list(photo_dir, sample, seed)
    if not photos:
        print(f"no images found in {photo_dir}")
        return None

    records = []
    solved = 0
    print(f"{'photo':<32} {'tiers tried':>18} {'solved':>7} {'seconds':>8} "
          f"{'logodds':>8} {'nmatch':>6}")
    for name in photos:
        rec = run_one(os.path.join(photo_dir, name), quick=quick)
        records.append(rec)
        if rec.get("error"):
            print(f"{name[:32]:<32} unreadable: {rec['error']}")
            continue
        solved += bool(rec["success"])
        norm = thresholds.from_bench(rec)
        tiers = " ".join(
            f"{a['fov_bounds'][0]:.0f}-{a['fov_bounds'][1]:.0f}"
            + ("✓" if a["success"] else "✗")
            for a in rec["attempts"] if a.get("fov_bounds")
        )
        lo = f"{norm['logodds']:.1f}" if norm["logodds"] is not None else "-"
        nm = norm["nmatch"] if norm["nmatch"] is not None else "-"
        print(f"{name[:32]:<32} {tiers:>18} {'yes' if rec['success'] else 'NO':>7} "
              f"{rec['total_seconds'] or 0:>8.1f} {lo:>8} {nm:>6}")

    run_doc = {
        "photo_dir": photo_dir, "sample": sample, "seed": seed, "quick": quick,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "thresholds": thresholds.current(),
        "photos": records,
    }
    print(f"\n{solved}/{len(photos)} solved")
    report(run_doc)
    if out_json:
        with open(out_json, "w") as fh:
            json.dump(run_doc, fh, indent=1)
        print(f"\nsaved to {out_json}")
    return run_doc


def normalized(run_doc):
    return [thresholds.from_bench(r) for r in run_doc["photos"]
            if not r.get("error")]


def report(run_doc):
    """The paragraph a PR that moves a threshold should paste."""
    report_records(normalized(run_doc), run_doc["thresholds"])


def report_records(recs, cur):
    """Shared by the corpus and by production rows, so a threshold
    argument reads the same whichever it is quoting (#99)."""
    m = thresholds.margins(recs)
    print(f"\ngates: logodds>={cur['min_logodds']} "
          f"matches>={cur['min_matches']} stars>={cur['min_stars']} "
          f"cpulimit={cur['cpulimit']}")
    if m["n"]:
        lo, nm = m["logodds"], m["nmatch"]
        print(f"solved log-odds: min {lo['min']:.1f}, p10 {lo['p10']:.1f}, "
              f"median {lo['median']:.1f}, max {lo['max']:.1f}")
        print(f"solved matches:  min {nm['min']}, p10 {nm['p10']}, "
              f"median {nm['median']}, max {nm['max']}")
    rejected = [r["name"] for r in recs if r["gate_rejected"]]
    if rejected:
        print(f"matched but rejected by the gate: {len(rejected)} "
              f"({', '.join(rejected[:4])}{'…' if len(rejected) > 4 else ''})")


def _floats(s):
    return [float(x) for x in s.split(",") if x.strip()]


def sweep_cmd(path, logodds=None, matches=None, stars=None):
    with open(path) as fh:
        run_doc = json.load(fh)
    recs = normalized(run_doc)
    base = run_doc["thresholds"]
    print(f"{len(recs)} photos from {run_doc['photo_dir']} "
          f"(run {run_doc['ran_at']})")
    _sweep_table(recs, base, logodds, matches, stars)


def _sweep_table(recs, base, logodds=None, matches=None, stars=None,
                 fallback=None):
    defaults = base or fallback or {}
    los = logodds or [defaults["min_logodds"]]
    nms = matches or [defaults["min_matches"]]
    sts = stars or [defaults["min_stars"]]
    print(f"\n{'logodds':>8} {'matches':>8} {'stars':>6} {'passed':>8} {'rate':>7}  flips")
    for lo in los:
        for nm in nms:
            for st in sts:
                out = thresholds.sweep(recs, lo, nm, st, baseline=base)
                flips = []
                if out["gained"]:
                    flips.append(f"+{len(out['gained'])}")
                if out["lost"]:
                    flips.append(f"-{len(out['lost'])}: "
                                 + ", ".join(out["lost"][:3]))
                if out["exact"] is None:
                    mark = "  (exactness unknown: these rows' gates are not recorded)"
                elif out["exact"]:
                    mark = ""
                else:
                    mark = "  (lower bound: stricter than the run)"
                print(f"{lo:>8} {nm:>8} {st:>6} {out['passed']:>8} "
                      f"{out['rate']:>7.1%}  {' '.join(flips)}{mark}")
    if base is None:
        print("\nThese records do not say what gates they ran under, so no "
              "exactness claim is made for them either way.")
    else:
        print("\nLoosening is exact. Tightening is a lower bound: a tier the "
              "run stopped at would have kept going under a stricter gate, and "
              "might have landed.")
    print("Tier bounds and CPU limits need --compare, not a sweep.")


def history_cmd(days=None, logodds=None, matches=None, stars=None):
    """The same report and sweep over real uploads instead of the corpus.

    The corpus bounds a threshold question; production settles it, once
    there are enough rows. Today there usually are not — that is the
    honest scope note in #99, not a reason to leave the path unbuilt.
    """
    from . import db, stats

    conn = db.get_conn()
    rows = stats.solve_history(conn, days)
    recs = [thresholds.from_solve_stats(r) for r in rows]
    if not recs:
        print("no rows in solve_stats yet")
        return
    window = f"the last {days} days" if days else "all retained rows"
    print(f"{len(recs)} finished jobs over {window}")

    # The gates each row ran under, not this process's constants: a window
    # can span a threshold change, and rows written before the gate columns
    # existed carry none at all. Where they disagree there is no baseline to
    # judge a candidate against, and the sweep says so instead of guessing.
    gates = {(r.get("gate_logodds"), r.get("gate_matches"), r.get("gate_stars"))
             for r in rows}
    base = None
    if len(gates) == 1:
        lo, nm, st = gates.pop()
        if None not in (lo, nm, st):
            base = {"min_logodds": lo, "min_matches": nm, "min_stars": st}
    cur = thresholds.current()
    report_records(recs, dict(base, cpulimit=cur["cpulimit"]) if base else cur)
    if base is None:
        print("(those are this build's gates: the rows do not all record their "
              "own, so some of them ran under others)")
    if logodds or matches or stars:
        _sweep_table(recs, base, logodds, matches, stars,
                     fallback=thresholds.current())


def compare_cmd(before_path, after_path):
    with open(before_path) as fh:
        before = json.load(fh)
    with open(after_path) as fh:
        after = json.load(fh)
    out = thresholds.compare(normalized(before), normalized(after))
    print(f"{out['shared']} photos in both runs")
    if out["only_before"] or out["only_after"]:
        print(f"  (only in before: {len(out['only_before'])}, "
              f"only in after: {len(out['only_after'])} — different samples?)")
    print(f"before: {out['before_passed']}/{out['shared']} in {out['before_seconds']}s")
    print(f"after:  {out['after_passed']}/{out['shared']} in {out['after_seconds']}s")
    if out["gained"]:
        print(f"gained ({len(out['gained'])}): {', '.join(out['gained'])}")
    if out["lost"]:
        print(f"lost ({len(out['lost'])}): {', '.join(out['lost'])}")
    if not out["gained"] and not out["lost"]:
        print("no photo changed verdict")
    for label, doc in (("before", before), ("after", after)):
        print(f"{label} thresholds: {json.dumps(doc['thresholds'])}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="app.bench", description=__doc__)
    p.add_argument("photo_dir", nargs="?", default="/photos")
    p.add_argument("--sample", type=int, help="run a deterministic subset")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--quick", action="store_true",
                   help="the uploader-is-watching budget, not the deep one")
    p.add_argument("--json", dest="out_json", help="save the run's numbers")
    p.add_argument("--sweep", metavar="RUN.json",
                   help="try thresholds against a saved run")
    p.add_argument("--compare", nargs=2, metavar=("BEFORE.json", "AFTER.json"))
    p.add_argument("--history", nargs="?", const=0, type=int, metavar="DAYS",
                   help="report over solve_stats instead of the corpus")
    p.add_argument("--logodds", type=_floats, help="sweep values, comma-separated")
    p.add_argument("--matches", type=_floats)
    p.add_argument("--stars", type=_floats)
    args = p.parse_args(argv)

    if args.compare:
        return compare_cmd(*args.compare)
    if args.sweep:
        return sweep_cmd(args.sweep, args.logodds, args.matches, args.stars)
    if args.history is not None:
        return history_cmd(args.history or None, args.logodds, args.matches,
                           args.stars)
    run(args.photo_dir, args.sample, args.seed, args.quick, args.out_json)


if __name__ == "__main__":
    main(sys.argv[1:])
