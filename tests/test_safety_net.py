from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from src import config
from src.contracts import Candidate, Leg, LegMode
from src.gtfs import GtfsFeed, gtfs_time_to_seconds, stop_pairs
from src.safety_net.candidates import (build_candidates, direct_options, hoboken_path_options,
                                       plan_station, solve_leave_home, to_utc)

TUE = date(2026, 10, 13)


def feed(trips: list[tuple[str, str, str, list[tuple[str, str]]]], key_col: str = "stop_code") -> GtfsFeed:
    """trips: (trip_id, route_id, train number, [(stop key, HH:MM:SS), ...]) all on TUE."""
    rows = [{"trip_id": t, "stop_id": f"s_{k}", "stop_sequence": str(i + 1),
             "arrival_time": hms, "departure_time": hms}
            for t, _, _, stops in trips for i, (k, hms) in enumerate(stops)]
    st = pd.DataFrame(rows)
    st["arrival_s"] = gtfs_time_to_seconds(st["arrival_time"])
    st["departure_s"] = gtfs_time_to_seconds(st["departure_time"])
    keys = sorted({k for *_, stops in trips for k, _ in stops})
    return GtfsFeed(
        stops=pd.DataFrame({"stop_id": [f"s_{k}" for k in keys], key_col: keys}),
        routes=pd.DataFrame({"route_id": sorted({r for _, r, _, _ in trips})}),
        trips=pd.DataFrame({"trip_id": [t for t, *_ in trips], "route_id": [r for _, r, _, _ in trips],
                            "service_id": "A", "trip_short_name": [n for _, _, n, _ in trips],
                            "direction_id": "0"}),
        stop_times=st, calendar=None,
        calendar_dates=pd.DataFrame({"service_id": ["A"], "date": ["20261013"], "exception_type": ["1"]}),
    )


NJT = feed([
    ("a", "10", "3920", [("NP", "07:30:00"), ("SE", "07:38:00"), ("NY", "07:50:00")]),
    ("b", "10", "3922", [("NP", "08:00:00"), ("SE", "08:08:00"), ("NY", "08:20:00")]),
    ("c", "10", "3924", [("NP", "08:30:00"), ("SE", "08:38:00"), ("NY", "08:50:00")]),
    ("d", "10", "3926", [("NP", "09:00:00"), ("SE", "09:08:00"), ("NY", "09:20:00")]),
    ("out", "10", "3913", [("NY", "08:00:00"), ("SE", "08:12:00"), ("NP", "08:20:00")]),
    ("me1", "7", "0872", [("CM", "07:50:00"), ("HB", "08:31:00")]),
    ("me2", "7", "6620", [("CM", "08:13:00"), ("HB", "09:00:00")]),
])
PATH = feed([
    ("p1", "BLU", "", [("Hoboken", "08:34:00"), ("33rd Street", "08:49:00")]),
    ("p2", "BLU", "", [("Hoboken", "08:41:00"), ("33rd Street", "08:56:00")]),
    ("p3", "BLU", "", [("Hoboken", "09:06:00"), ("33rd Street", "09:21:00")]),
], key_col="stop_name")


def flat_drive(minutes: float):
    return lambda dest, t: minutes * 60


def test_stop_pairs_respects_stop_order():
    pairs = stop_pairs(NJT, TUE, "SE", "NY")
    assert list(pairs["trip_short_name"]) == ["3920", "3922", "3924", "3926"]  # not outbound 3913


def test_planned_is_latest_on_time_and_catch_next_runs_past_deadline():
    deadline = to_utc(TUE, 9 * 3600)
    c = plan_station(direct_options(NJT, TUE, "SE"), "SE", "SE", deadline, flat_drive(30), "SE", TUE)
    assert not c.infeasible
    assert c.planned_train.train_number == "3924"
    assert [o[0].train_number for o in c.catch_next] == ["3924", "3926"]  # 3926 arrives 09:20
    # leave = 08:38 − 8 min buffer − 5 min margin − 30 min drive = 07:55 local
    assert c.leave_home == to_utc(TUE, 7 * 3600 + 55 * 60)
    assert [leg.mode for leg in c.legs] == [LegMode.DRIVE, LegMode.WALK, LegMode.RAIL]
    assert c.legs[0].to_stop == "LOT:SE" and c.legs[2].toward_ny


def test_planned_is_latest_departure_among_on_time():
    # A slow train that leaves later but still arrives by the deadline beats an earlier express.
    t = lambda hhmm: to_utc(TUE, int(hhmm[:2]) * 3600 + int(hhmm[3:]) * 60)  # noqa: E731
    def opt(train, dep, arr):
        return (Leg(LegMode.RAIL, "SE", "NY", t(dep), t(arr), train_number=train, gtfs_line="10",
                    direction=0, toward_ny=True),)
    express, local = opt("1", "08:30", "08:42"), opt("2", "08:35", "08:58")
    c = plan_station([express, local], "SE", "SE", t("09:00"), flat_drive(30), "SE", TUE)
    assert c.planned_train.train_number == "2"


def test_earlier_deadline_moves_planned_train():
    c = plan_station(direct_options(NJT, TUE, "SE"), "SE", "SE", to_utc(TUE, 8 * 3600),
                     flat_drive(30), "SE", TUE)
    assert c.planned_train.train_number == "3920"
    assert [o[0].train_number for o in c.catch_next] == ["3920", "3922", "3924", "3926"]


def test_no_on_time_train_is_infeasible_with_earliest():
    c = plan_station(direct_options(NJT, TUE, "SE"), "SE", "SE", to_utc(TUE, 7 * 3600),
                     flat_drive(30), "SE", TUE)
    assert c.infeasible and c.planned_train.train_number == "3920"
    assert "07:50" in c.infeasible_reason


def test_station_without_trains_is_infeasible_without_legs():
    c = plan_station(direct_options(NJT, TUE, "CM"), "CM", "CM", to_utc(TUE, 9 * 3600),
                     flat_drive(1), "CM", TUE)
    assert c.infeasible and c.legs == () and c.leave_home is None


def test_hoboken_path_uses_first_path_after_transfer():
    opts = hoboken_path_options(NJT, PATH, TUE)
    first = opts[0]
    assert [leg.mode for leg in first] == [LegMode.RAIL, LegMode.WALK, LegMode.PATH, LegMode.WALK]
    # HB 08:31 + 5 min transfer → 08:34 PATH is too early, 08:41 is taken; +4 min egress.
    assert first[2].sched_dep == to_utc(TUE, 8 * 3600 + 41 * 60)
    assert first[-1].to_stop == "NY" and first[-1].sched_arr == to_utc(TUE, 9 * 3600)
    assert first[2].from_stop == "PATH:Hoboken" and first[2].to_stop == "PATH:33rd Street"
    c = plan_station(opts, "CM", "CM", to_utc(TUE, 9 * 3600), flat_drive(1), "CM → HB + PATH", TUE)
    assert c.planned_train.train_number == "872" and len(c.catch_next) == 2


def test_leave_home_fixed_point_with_time_dependent_drive():
    arrive = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    def drive(dest, t):  # 20 min, +1 min per 10 min later departure
        return 1200 + (t - (arrive - timedelta(hours=1))).total_seconds() / 10

    leave, d = solve_leave_home(drive, "SE", arrive)
    assert abs((leave + timedelta(seconds=d) - arrive).total_seconds()) < config.LEAVE_HOME_TOL_S


def test_infeasible_candidate_needs_reason():
    with pytest.raises(ValueError):
        Candidate("CM", (), None, None, "safety_net", infeasible=True)


def test_catch_next_must_match_planned_suffix():
    t = lambda m: to_utc(TUE, 8 * 3600 + m * 60)  # noqa: E731
    rail = Leg(LegMode.RAIL, "SE", "NY", t(0), t(12), train_number="1", gtfs_line="10",
               direction=0, toward_ny=True)
    other = Leg(LegMode.RAIL, "SE", "NY", t(20), t(32), train_number="2", gtfs_line="10",
                direction=0, toward_ny=True)
    with pytest.raises(ValueError):
        Candidate("SE", (rail,), None, None, "safety_net", catch_next=((other,),))


@pytest.mark.skipif(not (config.GTFS_NJT_RAIL_DIR / "stop_times.txt").exists()
                    or not config.GTFS_PATH_DIR.exists(), reason="GTFS not available")
@pytest.mark.parametrize("deadline_s,se_train,hb_train", [(9 * 3600, "3926", "872"),
                                                          (8 * 3600, "8620", "802")])
def test_golden_demo_candidates(deadline_s, se_train, hb_train):
    """Real Tue 10-13 GTFS (hand-checked against stop_times.txt)."""
    from src.gtfs import load_feed

    cands = build_candidates(load_feed(config.GTFS_NJT_RAIL_DIR), load_feed(config.GTFS_PATH_DIR),
                             flat_drive(20), TUE, deadline_s)
    assert len({c.label for c in cands}) == 9  # station rows; each may carry up to 3 train variants
    assert max(sum(c.label == lab for c in cands) for lab in {c.label for c in cands}) <= config.TRAIN_CHOICE_T
    by_label = {c.label: c for c in cands if c.variant == 0}  # latest on-time train = timetable plan
    for s in ("CM", "ST", "RT"):  # Portal cutover: no one-seat ride to NY, but HB + PATH
        assert by_label[f"{s} → NY"].infeasible
        assert by_label[f"{s} → NY"].infeasible_reason == "Midtown Direct diverted to Hoboken (Portal cutover)"
        assert not by_label[f"{s} → HB + PATH"].infeasible
    assert by_label["SE → NY"].planned_train.train_number == se_train
    assert by_label["CM → HB + PATH"].planned_train.train_number == hb_train
    assert by_label["Drive all the way (not priced)"].sched_arr == to_utc(TUE, deadline_s)
