"""What a threshold change would do, measured instead of guessed (#99).

Every acceptance number in this codebase was set from a handful of photos,
and `MIN_LOGODDS`/`MIN_MATCHES` were already wrong once in production (#86).
The reason the argument stayed anecdotal is that the evidence kept being
thrown away: retention deletes the rows, so "what does the distribution
actually look like" could only be asked of whatever sat on disk that day.

Two sources now survive that: `solve_stats` for real uploads (#148) and a
saved bench run over the corpus. This module is the one analysis path over
both, so a threshold argument reads the same whichever it is quoting.

The leverage is that three of the thresholds are applied *after* the solver
has done its work — the gate in `solver.solve` and the star precheck in
`worker.process` both judge numbers the run already recorded. So sweeping
them over a saved run costs nothing and needs no re-solve. The tier bounds
and the CPU limits are not like that: they change what the solver does, so
they need a fresh run and `compare`.
"""

from . import solver


def from_bench(rec):
    """Normalize one bench photo record."""
    attempts = rec.get("attempts") or []
    matched = [a for a in attempts if a.get("match")]
    # The winning attempt when there was one, else the closest miss: the
    # gate rejects on either number, so rank by log-odds and let the
    # sweep decide what the match count would have done.
    best = next((a for a in matched if a.get("success")), None)
    if best is None and matched:
        best = max(matched, key=lambda a: a["match"].get("logodds") or 0.0)
    match = (best or {}).get("match") or {}
    return {
        "name": rec.get("name"),
        "solved": bool(rec.get("success")),
        "stars_detected": rec.get("stars_detected"),
        "logodds": match.get("logodds"),
        "nmatch": match.get("nmatch"),
        "seconds": rec.get("total_seconds"),
        "gate_rejected": bool(matched) and not any(a.get("success") for a in attempts),
    }


def from_solve_stats(row):
    """Normalize one `solve_stats` row (`stats.solve_history`)."""
    return {
        "name": row.get("job_id"),
        # The worker's success status is "done" (worker.process); reading
        # this as "solved" silently scores every real solve as a failure.
        "solved": row.get("status") == "done",
        "stars_detected": row.get("stars_detected"),
        "logodds": row.get("logodds"),
        "nmatch": row.get("nmatch"),
        "seconds": row.get("seconds"),
        "gate_rejected": row.get("reason") == "low_confidence",
    }


def solved_at(rec, min_logodds, min_matches, min_stars):
    """Would this record count as solved under these thresholds?

    A photo the solver never matched at all is out regardless: no
    threshold recovers a frame that produced no match. Everything else
    is the two gates applied to the numbers already recorded.
    """
    stars = rec.get("stars_detected")
    if stars is not None and stars < min_stars:
        return False
    logodds, nmatch = rec.get("logodds"), rec.get("nmatch")
    if logodds is None or nmatch is None:
        return False
    return logodds >= min_logodds and nmatch >= min_matches


def sweep(records, min_logodds, min_matches, min_stars, baseline=None):
    """Pass rate and per-photo flips under candidate thresholds.

    `baseline` is the run's own thresholds; when a candidate is *stricter*
    than the run on any axis the answer is a lower bound, because tiers
    that the run stopped short of would have kept going under the stricter
    gate and might have landed. Loosening is exact.
    """
    flips = {"gained": [], "lost": []}
    passed = 0
    for rec in records:
        now = solved_at(rec, min_logodds, min_matches, min_stars)
        passed += bool(now)
        if now and not rec["solved"]:
            flips["gained"].append(rec["name"])
        elif not now and rec["solved"]:
            flips["lost"].append(rec["name"])
    out = {
        "thresholds": {"min_logodds": min_logodds, "min_matches": min_matches,
                       "min_stars": min_stars},
        "total": len(records), "passed": passed,
        "rate": round(passed / len(records), 3) if records else 0.0,
        "gained": flips["gained"], "lost": flips["lost"],
        "exact": True,
    }
    if baseline:
        out["exact"] = (min_logodds <= baseline.get("min_logodds", min_logodds)
                        and min_matches <= baseline.get("min_matches", min_matches)
                        and min_stars <= baseline.get("min_stars", min_stars))
    return out


def current():
    """The thresholds this build would apply, for a run's header."""
    from .worker import PRECHECK_MIN_STARS
    return {"min_logodds": solver.MIN_LOGODDS, "min_matches": solver.MIN_MATCHES,
            "min_stars": PRECHECK_MIN_STARS, "cpulimit": solver.CPU_LIMIT,
            "capped_pass_cpulimit": solver.CAPPED_PASS_CPULIMIT,
            "source_depth": solver.SOURCE_DEPTH,
            "fallback_tiers": solver.FALLBACK_TIERS}


def margins(records):
    """How much room the solved photos had, so a proposed move can be
    read against the distribution rather than against one frame."""
    vals = sorted(r["logodds"] for r in records if r["solved"] and r.get("logodds"))
    matches = sorted(r["nmatch"] for r in records if r["solved"] and r.get("nmatch"))

    def pct(xs, p):
        if not xs:
            return None
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return {
        "n": len(vals),
        "logodds": {"min": vals[0] if vals else None, "p10": pct(vals, 0.10),
                    "median": pct(vals, 0.50), "max": vals[-1] if vals else None},
        "nmatch": {"min": matches[0] if matches else None, "p10": pct(matches, 0.10),
                   "median": pct(matches, 0.50), "max": matches[-1] if matches else None},
    }


def compare(before, after):
    """Two runs over the same photos: what moved. For the thresholds a
    sweep cannot answer — tier bounds, CPU limits, source depth — where
    the only honest measurement is running it again."""
    b = {r["name"]: r for r in before}
    a = {r["name"]: r for r in after}
    shared = [n for n in b if n in a]
    gained = sorted(n for n in shared if a[n]["solved"] and not b[n]["solved"])
    lost = sorted(n for n in shared if b[n]["solved"] and not a[n]["solved"])
    return {
        "shared": len(shared),
        "only_before": sorted(set(b) - set(a)),
        "only_after": sorted(set(a) - set(b)),
        "before_passed": sum(1 for n in shared if b[n]["solved"]),
        "after_passed": sum(1 for n in shared if a[n]["solved"]),
        "gained": gained, "lost": lost,
        "before_seconds": round(sum(b[n].get("seconds") or 0 for n in shared), 1),
        "after_seconds": round(sum(a[n].get("seconds") or 0 for n in shared), 1),
    }
