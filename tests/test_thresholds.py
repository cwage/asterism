"""Measuring a threshold change instead of guessing at it (#99).

The point of these is that the sweep is honest about what it can and
cannot answer: loosening a gate is exact over a saved run, tightening it
is a lower bound, and a photo that never matched is not recoverable by
any threshold at all.
"""

from app import bench, thresholds
from tests.test_moderation import fresh_db  # noqa: F401


class _Row(dict):
    """sqlite3.Row quacks: solve_record probes with `in row.keys()`."""

    def keys(self):
        return list(super().keys())

    def __getitem__(self, k):
        return super().get(k)


def _attempt(bounds=(8.0, 35.0), success=False, logodds=None, nmatch=None,
             seconds=1.0, timed_out=False):
    a = {"fov_bounds": list(bounds), "seconds": seconds, "success": success,
         "timed_out": timed_out, "low_confidence": None, "match": None}
    if logodds is not None:
        a["match"] = {"logodds": logodds, "nmatch": nmatch, "ndistract": 0}
        a["low_confidence"] = not success
    return a


def _photo(name, success=False, stars=80, attempts=(), seconds=2.0):
    return {"name": name, "success": success, "stars_detected": stars,
            "exif_fov_deg": 60.0, "total_seconds": seconds,
            "attempts": list(attempts)}


# --- normalizing ------------------------------------------------------

def test_from_bench_takes_the_winning_attempt():
    rec = thresholds.from_bench(_photo(
        "a.jpg", success=True,
        attempts=[_attempt(logodds=12.0, nmatch=5),
                  _attempt(success=True, logodds=70.6, nmatch=19)]))
    assert rec["solved"] is True
    assert rec["logodds"] == 70.6 and rec["nmatch"] == 19
    assert rec["gate_rejected"] is False


def test_from_bench_keeps_the_closest_miss():
    """A gate-rejected match is exactly the record a sweep needs: the
    solver found something, the threshold threw it away."""
    rec = thresholds.from_bench(_photo(
        "b.jpg", attempts=[_attempt(logodds=11.0, nmatch=6),
                           _attempt(logodds=22.5, nmatch=7)]))
    assert rec["solved"] is False
    assert rec["logodds"] == 22.5
    assert rec["gate_rejected"] is True


def test_from_bench_unmatched_photo_has_no_numbers():
    rec = thresholds.from_bench(_photo("c.jpg", attempts=[_attempt(), _attempt()]))
    assert rec["logodds"] is None and rec["gate_rejected"] is False


def test_from_solve_stats_row():
    rec = thresholds.from_solve_stats(
        {"job_id": "j1", "status": "done", "stars_detected": 40,
         "logodds": 42.7, "nmatch": 12, "seconds": 9.0, "reason": None})
    assert rec["solved"] is True and rec["logodds"] == 42.7
    assert rec["name"] == "j1"


# --- the gates --------------------------------------------------------

def test_solved_at_applies_both_gates():
    rec = {"name": "x", "solved": True, "stars_detected": 40,
           "logodds": 26.0, "nmatch": 9}
    assert thresholds.solved_at(rec, 25.0, 8, 10) is True
    assert thresholds.solved_at(rec, 30.0, 8, 10) is False   # log-odds
    assert thresholds.solved_at(rec, 25.0, 12, 10) is False  # matches
    assert thresholds.solved_at(rec, 25.0, 8, 50) is False   # precheck


def test_no_threshold_recovers_an_unmatched_photo():
    rec = {"name": "x", "solved": False, "stars_detected": 400,
           "logodds": None, "nmatch": None}
    assert thresholds.solved_at(rec, 0.0, 0, 0) is False


def test_missing_star_count_does_not_fail_the_precheck():
    """count_stars returns None on an unreadable frame; that is not the
    same as having counted zero."""
    rec = {"name": "x", "solved": True, "stars_detected": None,
           "logodds": 40.0, "nmatch": 10}
    assert thresholds.solved_at(rec, 25.0, 8, 10) is True


# --- sweeping ---------------------------------------------------------

def _recs():
    return [
        {"name": "clear", "solved": True, "stars_detected": 90, "logodds": 70.6,
         "nmatch": 19, "gate_rejected": False},
        {"name": "marginal", "solved": True, "stars_detected": 60, "logodds": 26.0,
         "nmatch": 9, "gate_rejected": False},
        {"name": "rejected", "solved": False, "stars_detected": 55, "logodds": 22.0,
         "nmatch": 7, "gate_rejected": True},
        {"name": "blank", "solved": False, "stars_detected": 2, "logodds": None,
         "nmatch": None, "gate_rejected": False},
    ]


def test_sweep_reports_rate_and_names_the_flips():
    out = thresholds.sweep(_recs(), 20.0, 7, 10)
    assert out["passed"] == 3 and out["total"] == 4
    assert out["gained"] == ["rejected"] and out["lost"] == []


def test_sweep_names_what_a_tightening_would_cost():
    out = thresholds.sweep(_recs(), 30.0, 8, 10)
    assert out["lost"] == ["marginal"]
    assert out["passed"] == 1


def test_loosening_is_exact_and_tightening_is_a_lower_bound():
    base = {"min_logodds": 25.0, "min_matches": 8, "min_stars": 10}
    assert thresholds.sweep(_recs(), 20.0, 7, 10, baseline=base)["exact"] is True
    assert thresholds.sweep(_recs(), 25.0, 8, 10, baseline=base)["exact"] is True
    assert thresholds.sweep(_recs(), 26.0, 8, 10, baseline=base)["exact"] is False
    assert thresholds.sweep(_recs(), 25.0, 8, 11, baseline=base)["exact"] is False


def test_margins_describe_the_solved_distribution():
    m = thresholds.margins(_recs())
    assert m["n"] == 2
    assert m["logodds"]["min"] == 26.0 and m["logodds"]["max"] == 70.6
    assert m["nmatch"]["min"] == 9


def test_margins_on_nothing_solved():
    m = thresholds.margins([{"name": "a", "solved": False, "logodds": None,
                             "nmatch": None}])
    assert m["n"] == 0 and m["logodds"]["median"] is None


# --- comparing two runs -----------------------------------------------

def test_compare_reports_flips_and_cost():
    before = [{"name": "a", "solved": True, "seconds": 5.0},
              {"name": "b", "solved": False, "seconds": 70.0}]
    after = [{"name": "a", "solved": True, "seconds": 4.0},
             {"name": "b", "solved": True, "seconds": 12.0}]
    out = thresholds.compare(before, after)
    assert out["shared"] == 2
    assert out["gained"] == ["b"] and out["lost"] == []
    assert out["before_seconds"] == 75.0 and out["after_seconds"] == 16.0


def test_compare_flags_runs_over_different_photos():
    out = thresholds.compare([{"name": "a", "solved": True, "seconds": 1.0}],
                             [{"name": "z", "solved": True, "seconds": 1.0}])
    assert out["shared"] == 0
    assert out["only_before"] == ["a"] and out["only_after"] == ["z"]


# --- sampling ---------------------------------------------------------

def test_sample_is_deterministic_and_independent_of_listing_order(tmp_path):
    for i in range(20):
        (tmp_path / f"img{i:02d}.jpg").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    first = bench.photo_list(str(tmp_path), sample=5, seed=7)
    assert first == bench.photo_list(str(tmp_path), sample=5, seed=7)
    assert first != bench.photo_list(str(tmp_path), sample=5, seed=8)
    assert len(first) == 5
    assert all(n.endswith(".jpg") for n in first)


def test_sample_larger_than_the_corpus_returns_everything(tmp_path):
    (tmp_path / "one.jpg").write_bytes(b"")
    assert bench.photo_list(str(tmp_path), sample=50) == ["one.jpg"]


# --- the production half ----------------------------------------------

def test_wasted_seconds_counts_only_the_tiers_that_missed(fresh_db):
    """`seconds` is the total, so the cost of a wrong scale bracket was
    invisible — the one CPU_LIMIT question a sweep cannot answer."""
    from app import db, stats

    conn = db.get_conn()
    job = {"id": "j1", "created_at": "2026-09-22 00:00:00", "mode": "deep"}
    result = {"success": True, "total_seconds": 48.0, "attempts": [
        {"fov_bounds": [30.0, 90.0], "seconds": 40.0, "success": False},
        {"fov_bounds": [8.0, 35.0], "seconds": 8.0, "success": True,
         "match": {"logodds": 55.0, "nmatch": 14, "ndistract": 1}},
    ]}
    rec = stats.record_solve(conn, _Row(job), "done", result)
    assert rec["wasted_seconds"] == 40.0
    row = dict(conn.execute("SELECT * FROM solve_stats WHERE job_id='j1'").fetchone())
    assert row["wasted_seconds"] == 40.0
    assert thresholds.from_solve_stats(row)["solved"] is True


def test_a_clean_solve_wastes_nothing(fresh_db):
    from app import db, stats

    conn = db.get_conn()
    result = {"success": True, "total_seconds": 4.0, "attempts": [
        {"fov_bounds": [30.0, 90.0], "seconds": 4.0, "success": True,
         "match": {"logodds": 70.6, "nmatch": 19, "ndistract": 0}}]}
    rec = stats.record_solve(conn, _Row({"id": "j2", "created_at": "x", "mode": "quick"}),
                             "done", result)
    assert rec["wasted_seconds"] is None


def test_the_success_status_is_done_not_solved():
    """worker.process returns "done" for a solve. Reading it as "solved"
    scores every real upload as a failure and the report lies quietly."""
    assert thresholds.from_solve_stats({"status": "done"})["solved"] is True
    assert thresholds.from_solve_stats({"status": "failed"})["solved"] is False
    assert thresholds.from_solve_stats({"status": "solved"})["solved"] is False


# --- what the review caught ------------------------------------------

def test_a_sweep_considers_every_matched_attempt():
    """The gate has two axes, so ranking attempts on log-odds alone drops
    the one that would have cleared a candidate on match count. Neither
    of these clears 25/8; only the lower-log-odds one clears 20/8."""
    rec = thresholds.from_bench(_photo("x.jpg", attempts=[
        _attempt(logodds=24.0, nmatch=100), _attempt(logodds=100.0, nmatch=1)]))
    assert thresholds.solved_at(rec, 20.0, 8, 10) is True
    assert thresholds.solved_at(rec, 25.0, 8, 10) is False
    # and the representative kept for display is still the best miss
    assert rec["logodds"] == 100.0


def test_solved_at_still_reads_a_record_without_the_attempt_list():
    """Hand-built records and older saved runs have no `matches`."""
    rec = {"name": "x", "solved": True, "stars_detected": 40,
           "logodds": 26.0, "nmatch": 9}
    assert thresholds.solved_at(rec, 25.0, 8, 10) is True
    assert thresholds.solved_at(rec, 30.0, 8, 10) is False


def test_gate_rejection_is_not_a_reason_string():
    """_describe_failure writes no_match, timeout, partial_timeout or
    no_stars — never low_confidence. Keying on that string made the
    matched-but-rejected line permanently empty."""
    from app import worker

    for reason in ("no_match", "timeout", "partial_timeout"):
        rec = thresholds.from_solve_stats(
            {"status": "failed", "reason": reason, "logodds": 22.1,
             "nmatch": 7, "stars_detected": 57})
        assert rec["gate_rejected"] is True, reason
    # a failure with no numbers at all was never judged by the gate
    assert thresholds.from_solve_stats(
        {"status": "failed", "reason": "no_stars", "logodds": None,
         "nmatch": None})["gate_rejected"] is False
    # a solve is not a rejection
    assert thresholds.from_solve_stats(
        {"status": "done", "logodds": 70.6, "nmatch": 19})["gate_rejected"] is False
    # pin the vocabulary this depends on, so a new reason string is noticed
    tried = [{"timed_out": False, "fov_bounds": [30.0, 90.0]}]
    ran_out = [{"timed_out": True, "fov_bounds": [8.0, 35.0]}]
    assert worker._describe_failure(tried)[0] == "no_match"
    assert worker._describe_failure(ran_out)[0] == "timeout"
    assert worker._describe_failure(tried + ran_out)[0] == "partial_timeout"


def test_no_baseline_means_no_exactness_claim():
    """Production rows can span a threshold change, or predate the gate
    columns. Claiming exactness against this build's constants would be
    the tool lying about its own footing."""
    assert thresholds.sweep(_recs(), 20.0, 7, 10)["exact"] is None
    assert thresholds.sweep(_recs(), 20.0, 7, 10, baseline={
        "min_logodds": 25.0, "min_matches": 8, "min_stars": 10})["exact"] is True


def test_a_row_records_the_gates_it_ran_under(fresh_db):
    from app import db, solver, stats

    conn = db.get_conn()
    result = {"success": True, "total_seconds": 4.0, "attempts": [
        {"fov_bounds": [30.0, 90.0], "seconds": 4.0, "success": True,
         "match": {"logodds": 70.6, "nmatch": 19, "ndistract": 0}}]}
    rec = stats.record_solve(conn, _Row({"id": "j3", "created_at": "x",
                                         "mode": "quick"}), "done", result)
    assert rec["gate_logodds"] == solver.MIN_LOGODDS
    assert rec["gate_matches"] == solver.MIN_MATCHES
    row = dict(conn.execute("SELECT * FROM solve_stats WHERE job_id='j3'").fetchone())
    assert row["gate_logodds"] == solver.MIN_LOGODDS
    assert row["gate_stars"] == stats.worker_min_stars()


def test_an_early_tiers_match_is_not_lost_to_a_later_empty_one(fresh_db):
    """result["match"] is the last attempt's. A gate-rejected match on
    tier one followed by a tier that matched nothing used to store None,
    losing exactly the row a threshold argument needs."""
    from app import db, stats

    conn = db.get_conn()
    result = {"success": False, "total_seconds": 60.0, "match": None,
              "attempts": [
                  {"fov_bounds": [30.0, 90.0], "seconds": 20.0, "success": False,
                   "match": {"logodds": 22.1, "nmatch": 7, "ndistract": 3}},
                  {"fov_bounds": [8.0, 35.0], "seconds": 40.0, "success": False},
              ], "failure": {"reason": "no_match"}}
    rec = stats.record_solve(conn, _Row({"id": "j4", "created_at": "x",
                                         "mode": "deep"}), "failed", result)
    assert rec["logodds"] == 22.1 and rec["nmatch"] == 7
    assert thresholds.from_solve_stats(
        dict(rec, status="failed"))["gate_rejected"] is True
