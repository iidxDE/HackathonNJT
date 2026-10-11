from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.build.pools import build_pools
from src.contracts import CostBreakdown, CostItem
from src.safety_net.candidates import (direct_options, drive_all_the_way, hoboken_path_options,
                                       plan_station, to_utc)
from src.scoring.drive import lookup
from src.scoring.scorer import ScoringInputs, Settings, rank_options, score_all, triangular_ppf
from test_safety_net import NJT, PATH  # synthetic Tue 10-13 timetable

TUE = date(2026, 10, 13)
DEADLINE_S = 9 * 3600


def pools(extra: list[dict] | None = None) -> pd.DataFrame:
    """Weekday AM-peak runs: NEC SE→NY (alight +60..+360 s) and M&E CM→HB (+60..+300 s)."""
    rows = list(extra or [])
    for i in range(6):
        term = pd.Timestamp("2026-10-09 08:30", tz="America/New_York").tz_convert("UTC")
        for code, d in (("SE", 30 * i), ("NY", 60 + 60 * i)):
            rows.append({"service_date": date(2026, 10, 9), "train_number": f"39{i}", "trip_id": f"n{i}",
                         "route_id": "10", "toward_ny": True, "terminal_arr_utc": term, "stop_code": code,
                         "delay_s": d, "delay_vs_gtfs_s": d})
        for code, d in (("CM", 0), ("HB", 60 + 60 * min(i, 4))):
            rows.append({"service_date": date(2026, 10, 9), "train_number": f"6{i}", "trip_id": f"m{i}",
                         "route_id": "7", "toward_ny": True, "terminal_arr_utc": term, "stop_code": code,
                         "delay_s": d, "delay_vs_gtfs_s": d})
    return build_pools(pd.DataFrame(rows))


def drive_cache() -> pd.DataFrame:
    rows = []
    for dest, (lo, mid, hi) in {"SE": (1500, 1800, 2400), "CM": (60, 60, 60), "NYG": (2400, 3000, 4400)}.items():
        for hhmm in ("06:00", "07:00", "08:00", "09:00"):
            slot = pd.Timestamp(f"2026-10-13 {hhmm}", tz="America/New_York").tz_convert("UTC")
            for model, d in (("OPTIMISTIC", lo), ("BEST_GUESS", mid), ("PESSIMISTIC", hi)):
                rows.append({"dest": dest, "slot_utc": slot, "model": model, "duration_s": float(d),
                             "distance_m": 30000.0, "toll_usd": 0.0, "fetched_at": pd.Timestamp("2026-10-10")})
    return pd.DataFrame(rows)


def path_schedule() -> np.ndarray:
    return np.array([[to_utc(TUE, h * 3600 + m * 60).timestamp(), to_utc(TUE, h * 3600 + (m + 15) * 60).timestamp()]
                     for h in (8, 9) for m in range(1, 60, 10)])


def money(usd: float) -> CostBreakdown:
    item = CostItem(usd, "https://example.test")
    return CostBreakdown(item, CostItem(0.0, "x"), CostItem(0.0, "x"), CostItem(0.0, "x"))


def candidates(cache: pd.DataFrame):
    drive = lambda dest, t: lookup(cache, dest, t).best_guess_s  # noqa: E731
    deadline = to_utc(TUE, DEADLINE_S)
    se = plan_station(direct_options(NJT, TUE, "SE"), "SE", "SE", deadline, drive, "SE → NY", TUE)
    hb = plan_station(hoboken_path_options(NJT, PATH, TUE), "CM", "CM", deadline, drive, "CM → HB + PATH", TUE)
    car = drive_all_the_way(deadline, drive)
    return [replace(se, costs=money(20.0)), replace(hb, costs=money(30.0)),
            replace(car, costs=replace(money(0.0), priced=False))]


@pytest.fixture(scope="module")
def inputs() -> ScoringInputs:
    return ScoringInputs(pools(), drive_cache(), path_schedule(), TUE)


def by_id(scored):
    return {s.candidate_id: s for s in scored}


def test_triangular_ppf_matches_bounds_and_mode():
    u = np.linspace(0, 1, 10001)
    x = triangular_ppf(u, 10.0, 12.0, 20.0)
    assert x[0] == pytest.approx(10) and x[-1] == pytest.approx(20)
    assert np.mean(x) == pytest.approx((10 + 12 + 20) / 3, rel=1e-3)
    assert np.all(triangular_ppf(u, 5.0, 5.0, 5.0) == 5)


def test_same_seed_identical_output(inputs):
    s = Settings(deadline_s=DEADLINE_S, n=500)
    a, b = score_all(candidates(inputs.drive_cache), inputs, s), score_all(candidates(inputs.drive_cache), inputs, s)
    assert [(x.p_on_time, x.gen_cost_usd, x.train_taken_share) for x in a] == \
           [(x.p_on_time, x.gen_cost_usd, x.train_taken_share) for x in b]


def test_zero_delay_and_spread_is_on_time_and_planned(inputs):
    s = Settings(deadline_s=DEADLINE_S, n=300, delay_scale=0, drive_spread=False)
    scored = by_id(score_all(candidates(inputs.drive_cache), inputs, s))
    for cid in ("SE → NY", "CM → HB + PATH", "Drive all the way (not priced)"):
        assert scored[cid].p_on_time == 1.0 and scored[cid].censored_share == 0
    assert scored["SE → NY"].train_taken_share == {"3924": 1.0}
    assert scored["CM → HB + PATH"].train_taken_share == {"872": 1.0}
    assert all(x.rank == x.sched_rank for x in scored.values())


def test_shifting_delays_hurts_rail_not_drive(inputs):
    base = Settings(deadline_s=DEADLINE_S, n=1000)
    a = by_id(score_all(candidates(inputs.drive_cache), inputs, base))
    b = by_id(score_all(candidates(inputs.drive_cache), inputs, replace(base, delay_shift_s=1200)))
    assert b["SE → NY"].p_on_time < a["SE → NY"].p_on_time
    assert b["CM → HB + PATH"].p_on_time < a["CM → HB + PATH"].p_on_time
    drive = "Drive all the way (not priced)"
    assert b[drive].p_on_time == a[drive].p_on_time


def test_hoboken_delay_cascades_to_next_path(inputs):
    # HB 08:31 + delay + 5 min transfer must be <= 08:41 PATH (→ 33rd 08:56 + 4 = 09:00);
    # more than 300 s late → 08:51 PATH → 09:10. HB delays 60..300 s shifted +120 s
    # = 180, 240, 300, 360, 420, 420: three of six runs miss the 08:41.
    s = Settings(deadline_s=DEADLINE_S, n=2000, drive_spread=False, delay_shift_s=120)
    hb = by_id(score_all(candidates(inputs.drive_cache), inputs, s))["CM → HB + PATH"]
    assert hb.p_on_time == pytest.approx(3 / 6, abs=0.04)
    assert hb.arrival_p90 == to_utc(TUE, 9 * 3600 + 10 * 60)


def test_no_catchable_train_is_censored(inputs):
    s = Settings(deadline_s=DEADLINE_S, n=200, delay_scale=0, drive_spread=False)
    se = candidates(inputs.drive_cache)[0]
    late = replace(se, leave_home=to_utc(TUE, 9 * 3600 + 30 * 60))  # leaves after the last train
    out = score_all([late], inputs, s)[0]
    assert out.censored_share == 1.0 and out.train_taken_share == {"none": 1.0}
    assert out.expected_late_min == pytest.approx(60)


def test_ranking_rules(inputs):
    s = Settings(deadline_s=DEADLINE_S, n=300)
    cands = candidates(inputs.drive_cache)
    infeasible = replace(cands[0], infeasible=True, infeasible_reason="test", label="late SE")
    scored = by_id(rank_options(score_all(cands + [infeasible], inputs, s)))
    assert scored["Drive all the way (not priced)"].rank is None  # reference row
    assert scored["late SE"].rank == max(s.rank for s in scored.values() if s.rank)  # infeasible last
    assert {scored["SE → NY"].rank, scored["CM → HB + PATH"].rank} == {1, 2}


def _rail(frm, to, dep_s, arr_s, train, route="10", terminal="NY"):
    from src.contracts import Leg, LegMode
    return Leg(LegMode.RAIL, frm, to, to_utc(TUE, dep_s), to_utc(TUE, arr_s), train_number=train,
               gtfs_line=route, direction=0, toward_ny=True, terminal=terminal)


H8 = 8 * 3600
T902_ARR, T903_ARR = H8 + 29 * 60, H8 + 43 * 60


def rail_rail_candidate(route1: str = "10"):
    """NP → SE on 901 (mid-trip: its terminal is NY), then SE → NY on the planned 902
    (dep 08:16, arr 08:29) or the next second-leg train 903 (dep 08:30, arr 08:43)."""
    from src.contracts import Candidate, Leg, LegMode
    r1 = _rail("NP", "SE", H8, H8 + 600, "901", route=route1)
    a = _rail("SE", "NY", H8 + 960, T902_ARR, "902")
    b = _rail("SE", "NY", H8 + 1800, T903_ARR, "903")
    leave = to_utc(TUE, 7 * 3600)
    drive = Leg(LegMode.DRIVE, "ORIGIN", "LOT:SE", leave, to_utc(TUE, 7 * 3600 + 1800))
    walk = Leg(LegMode.WALK, "LOT:SE", "NP", to_utc(TUE, 7 * 3600 + 1800), r1.sched_dep)
    return Candidate("NP", (drive, walk, r1, a), money(10.0), None, "router",
                     catch_next=((r1, a), (r1, b)), leave_home=leave, label="NP → SE → NY")


def test_rail_to_rail_planned_second_leg(inputs):
    """No delays: SE 08:10 + 5 min same-stop minimum = 08:15 ≤ 902 at 08:16 → 902 every draw."""
    s = Settings(deadline_s=DEADLINE_S, n=500, delay_scale=0.0)
    out = score_all([rail_rail_candidate()], inputs, s)[0]
    assert out.train_taken_share == {"901+902": 1.0}
    assert pd.Timestamp(out.arrival_p50) == pd.Timestamp(to_utc(TUE, T902_ARR))
    assert {u.train_number for u in out.pools} == {"901", "902"}
    assert "intermediate_alight" in out.flags  # 901 alights at SE, its terminal is NY
    assert [u.intermediate_alight for u in out.pools if u.train_number == "902"] == [False]


def test_rail_to_rail_late_first_leg_takes_next_second_leg():
    """Leg 1 pool runs 10 min late at SE: ready = 08:10 + 600 + 300 = 08:25, later than
    902 even with its boarding delay (≤ 08:18:30) → the rider takes 903 on every draw."""
    term = pd.Timestamp("2026-10-09 08:30", tz="America/New_York").tz_convert("UTC")
    late = [{"service_date": date(2026, 10, 9), "train_number": f"32{i}", "trip_id": f"j{i}",
             "route_id": "11", "toward_ny": True, "terminal_arr_utc": term, "stop_code": code,
             "delay_s": d, "delay_vs_gtfs_s": d}
            for i in range(6) for code, d in (("NP", 0), ("SE", 600))]
    inp = ScoringInputs(pools(late), drive_cache(), path_schedule(), TUE)
    out = score_all([rail_rail_candidate(route1="11")], inp, Settings(deadline_s=DEADLINE_S, n=500))[0]
    assert out.train_taken_share == {"901+903": 1.0}
    # 903 arrival + its NY alight delay (+60..+360 s in the NEC pool).
    p50 = pd.Timestamp(out.arrival_p50)
    assert pd.Timestamp(to_utc(TUE, T903_ARR + 60)) <= p50 <= pd.Timestamp(to_utc(TUE, T903_ARR + 360))
    assert {u.train_number for u in out.pools} == {"901", "903"}  # PoolUsage of the train taken
    assert out.censored_share == 0.0


def test_rail_to_rail_transfer_minimum_matters(inputs):
    """Same draws, no same-stop minimum: 902 is catchable more often → earlier median."""
    s = Settings(deadline_s=DEADLINE_S, n=500)
    c = rail_rail_candidate()
    strict = score_all([c], replace(inputs, same_stop_transfer_s=900), s)[0]
    loose = score_all([c], replace(inputs, same_stop_transfer_s=0), s)[0]
    assert strict.train_taken_share == {"901+903": 1.0}
    assert loose.train_taken_share.get("901+902", 0) > 0.5
    assert pd.Timestamp(loose.arrival_p50) < pd.Timestamp(strict.arrival_p50)


def test_late_minutes_capped_on_every_draw(inputs):
    """A train arriving after deadline + cap scores exactly the cap, like a censored draw."""
    s = Settings(deadline_s=H8, n=200, delay_scale=0.0, late_cap_min=10)  # deadline 08:00
    out = score_all([rail_rail_candidate()], inputs, s)[0]  # arrives 08:29 (29 min late)
    assert out.expected_late_min == pytest.approx(10.0)
    assert out.p_on_time == 0.0


def test_can_board_rule():
    from src.scoring.scorer import can_board
    assert can_board(100, 100) and not can_board(101, 100) and can_board(101, 100, 1)
