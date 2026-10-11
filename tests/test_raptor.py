"""Phase 2.2: range RAPTOR."""

from datetime import date

import pandas as pd
import pytest

from src import config, gtfs
from src.routing.network import (FeedSpec, Network, Transfers, build_network, load_timetable,
                                 load_transfers)
from src.routing.raptor import ProfileStats, route_profile

D = date(2026, 10, 13)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


def _write(d, name, text):
    (d / name).write_text(text.strip() + "\n", encoding="utf-8")


@pytest.fixture
def toy_net(tmp_path) -> Network:
    """A→B (R1), B→C (R2, two trips), a B-passing trip with no pickup at B, A→C direct."""
    _write(tmp_path, "stops.txt", "stop_id,stop_code,stop_name\nA,A,A\nB,B,B\nC,C,C")
    _write(tmp_path, "routes.txt", "route_id,route_short_name\nR1,R1\nR2,R2\nR3,R3\nR4,R4")
    _write(tmp_path, "calendar_dates.txt", "service_id,date,exception_type\nS,20261013,1")
    _write(tmp_path, "trips.txt", """
route_id,service_id,trip_id,trip_short_name
R1,S,a1,11
R2,S,b1,21
R2,S,b2,22
R3,S,np,31
R4,S,d1,41
""")
    _write(tmp_path, "stop_times.txt", """
trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type
a1,08:00:00,08:00:00,A,1,0,0
a1,08:10:00,08:10:00,B,2,0,0
b1,08:12:00,08:12:00,B,1,0,0
b1,08:22:00,08:22:00,C,2,0,0
b2,08:20:00,08:20:00,B,1,0,0
b2,08:30:00,08:30:00,C,2,0,0
np,08:00:00,08:00:00,A,1,1,0
np,08:15:00,08:15:00,B,2,1,0
np,08:25:00,08:25:00,C,3,0,0
d1,08:05:00,08:05:00,A,1,0,0
d1,08:40:00,08:40:00,C,2,0,0
""")
    tt = load_timetable(FeedSpec("X", tmp_path, "stop_code"), D)
    tr = Transfers(pd.DataFrame({"from_stop": ["X:C"], "to_stop": ["DEST:Z"],
                                 "min_transfer_s": [60]}), 300)
    return build_network([tt], tr, D)


def _toy(net, access, deadline="08:30", **kw):
    return route_profile(net, access, D, _hm(deadline), target="DEST:Z",
                         mode_by_feed={"X": "rail"}, window_before_s=3600,
                         arrive_after_s=3600, depart_after_s=0, **kw)


def test_toy_transfer_minimum_and_pareto(toy_net):
    js = _toy(toy_net, "X:A")
    got = {(j.dep_time, j.arr_time, j.rides, tuple(leg.train_no for leg in j.legs if leg.trip_id))
           for j in js}
    # 11 arrives B 08:10; 21 at 08:12 is inside the 5-min transfer → 22 (08:20).
    assert (_hm("08:00"), _hm("08:31"), 2, ("11", "22")) in got
    # Direct 41: departs later, arrives later, fewer rides → also Pareto-optimal.
    assert (_hm("08:05"), _hm("08:41"), 1, ("41",)) in got
    assert all("31" not in g[3] for g in got)  # no pickup at A or B on trip 31
    assert len(got) == 2


def test_toy_no_pickup_stop(toy_net):
    js = _toy(toy_net, "X:B")
    trains = {leg.train_no for j in js for leg in j.legs if leg.trip_id}
    assert "31" not in trains and {"21", "22"} <= trains


def test_toy_access_walk_needs_no_transfer_minimum(tmp_path, toy_net):
    # From C, the only "journey" is the footpath; with no rides nothing is emitted.
    assert _toy(toy_net, "X:C") == []


needs_net = pytest.mark.skipif(
    not ((config.GTFS_NJT_RAIL_DIR / "stop_times.txt").exists() and config.TRANSFERS_CSV.exists()),
    reason="GTFS / transfers.csv not available")


@pytest.fixture(scope="module")
def demo_net() -> Network:
    from src.build.network import build
    p = config.network_npz(config.DEMO_SERVICE_DATE)
    if not p.exists():
        build(config.DEMO_SERVICE_DATE)
    return Network.load(p)


def _profile(net, access, stats=None):
    return route_profile(net, access, config.DEMO_SERVICE_DATE, config.DEMO_DEADLINE_S, stats=stats)


def _gtfs_times(frm: str, to: str) -> dict[str, tuple[int, int]]:
    feed = gtfs.load_feed(config.GTFS_NJT_RAIL_DIR)
    p = gtfs.stop_pairs(feed, config.DEMO_SERVICE_DATE, frm, to)
    return {str(r.trip_short_name).lstrip("0"): (int(r.dep_s), int(r.arr_s)) for r in p.itertuples()}


@needs_net
@pytest.mark.parametrize("access,trains", [("NP", ["3224", "3828", "3926"]),
                                           ("SE", ["8622", "8624", "3926"])])
def test_direct_trains_with_gtfs_times(demo_net, access, trains):
    js = _profile(demo_net, f"NJT:{access}")
    direct = {j.legs[0].train_no: j for j in js if j.rides == 1 and j.legs[0].mode == "rail"}
    ref = _gtfs_times(access, "NY")
    for tn in trains:
        j = direct[tn]
        assert (j.dep_time, j.arr_time) == ref[tn]  # NJT:NY → DEST:NYP is 0 min
        assert j.legs[0].alight_stop == "NJT:NY"


@needs_net
@pytest.mark.parametrize("access", ["CM", "ST", "RT"])
def test_midtown_direct_via_hoboken_path(demo_net, access):
    js = _profile(demo_net, f"NJT:{access}")
    assert js
    for j in js:
        rides = [leg for leg in j.legs if leg.mode != "walk"]
        assert [leg.mode for leg in rides] == ["rail", "path"], j
        assert rides[0].alight_stop == "NJT:HB" and rides[0].route_id in ("NJT:7", "NJT:8")
        assert all(leg.alight_stop != "NJT:NY" for leg in j.legs)
        walk = next(leg for leg in j.legs if leg.board_stop == "NJT:HB")
        assert rides[1].dep >= walk.arr == rides[0].arr + config.TRANSFER_MIN_S["hoboken_njt_to_path"]
    assert "872" in {j.legs[0].train_no for j in js}


@needs_net
def test_pickup_and_drop_off_respected(demo_net):
    """Never board where pickup is not allowed (flagged PATH stops from 2.1)."""
    net = demo_net
    sidx = net.stop_index()
    trip_row = {t: i for i, t in enumerate(net.trip_ids)}
    # Pattern + position for each (trip, stop) to look up the masks.
    where = {}
    for p in range(net.n_patterns):
        stops = net.pattern_stops[net.ps_ptr[p]:net.ps_ptr[p + 1]]
        for g in range(net.pt_ptr[p], net.pt_ptr[p + 1]):
            for pos, s in enumerate(stops):
                where[(str(net.trip_ids[g]), int(s))] = int(net.tt_ptr[g]) + pos
    restricted = {int(net.pattern_stops[net.ps_ptr[p] + pos])
                  for p in range(net.n_patterns)
                  for g in range(net.pt_ptr[p], net.pt_ptr[p + 1])
                  for pos in range(net.ps_ptr[p + 1] - net.ps_ptr[p])
                  if not net.board_ok[net.tt_ptr[g] + pos]}
    assert {net.stop_ids[s] for s in restricted} == {"PATH:23rd Street", "PATH:9th Street"}
    n_checked = 0
    for s in restricted:
        for hour in range(0, 24):
            js = route_profile(net, str(net.stop_ids[s]), config.DEMO_SERVICE_DATE,
                               hour * 3600 + 3600, window_before_s=3600, arrive_after_s=1800)
            for j in js:
                for leg in j.legs:
                    if leg.trip_id is None:
                        continue
                    assert net.board_ok[where[(leg.trip_id, sidx[leg.board_stop])]], leg
                    assert net.alight_ok[where[(leg.trip_id, sidx[leg.alight_stop])]], leg
                    n_checked += 1
    assert n_checked > 0 and trip_row


@needs_net
@pytest.mark.parametrize("access", config.ACCESS_STATIONS)
def test_profile_pareto_window_and_runtime(demo_net, access):
    stats = ProfileStats()
    js = _profile(demo_net, f"NJT:{access}", stats)
    assert stats.runtime_s < 0.5 and stats.iterations > 0 and stats.labels_touched > 0
    dl = config.DEMO_DEADLINE_S
    for j in js:
        assert j.rides <= config.ROUTER_MAX_RIDES_OUT
        assert dl - config.ROUTER_WINDOW_BEFORE_S <= j.dep_time <= dl + config.ROUTER_DEPART_AFTER_S
        assert j.arr_time <= dl + config.ROUTER_ARRIVE_AFTER_S
        assert j.legs[-1].alight_stop == config.ROUTER_DEST_STOP
        for a, b in zip(j.legs, j.legs[1:]):
            assert a.alight_stop == b.board_stop and a.arr <= b.dep
        for o in js:  # no journey dominated by another
            if o is not j:
                assert not (o.dep_time >= j.dep_time and o.arr_time <= j.arr_time
                            and o.rides <= j.rides), (j, o)
    t = load_transfers(config.TRANSFERS_CSV)
    assert demo_net.same_stop_transfer_s == t.same_stop_s


# --- Exclusion (3.1): each field blocks exactly what it names --------------------

def _trains(js):
    return {tuple(leg.train_no for leg in j.legs if leg.trip_id) for j in js}


def _trip_index(net, raw_trip):
    return next(i for i, t in enumerate(net.trip_ids) if str(t).split(":", 1)[-1] == raw_trip)


def test_exclusion_none_and_empty_equal_plain(toy_net):
    from src.routing.exclusion import Exclusion
    for access in ("X:A", "X:B"):
        plain = _toy(toy_net, access)
        assert _toy(toy_net, access, exclusion=None) == plain
        assert _toy(toy_net, access, exclusion=Exclusion()) == plain
    assert not Exclusion() and Exclusion(no_board={"X:A"})
    assert hash(Exclusion(no_board=["X:A"])) == hash(Exclusion(no_board={"X:A"}))


def test_exclusion_no_alight(toy_net):
    from src.routing.exclusion import Exclusion
    assert _trains(_toy(toy_net, "X:A")) == {("11", "22"), ("41",)}
    # cannot leave 11 at B → only the direct 41; alighting at C is untouched
    assert _trains(_toy(toy_net, "X:A", exclusion=Exclusion(no_alight={"X:B"}))) == {("41",)}
    assert _toy(toy_net, "X:A", exclusion=Exclusion(no_alight={"X:C"})) == []


def test_exclusion_no_board(toy_net):
    from src.routing.exclusion import Exclusion
    # 11 may still alight at B, but nothing may board there → only the direct 41
    assert _trains(_toy(toy_net, "X:A", exclusion=Exclusion(no_board={"X:B"}))) == {("41",)}
    assert _toy(toy_net, "X:A", exclusion=Exclusion(no_board={"X:A"})) == []
    # boarding at A is untouched by a ban at C
    assert _trains(_toy(toy_net, "X:A", exclusion=Exclusion(no_board={"X:C"}))) == {("11", "22"), ("41",)}


def test_exclusion_banned_trips(toy_net):
    from src.routing.exclusion import Exclusion
    assert _trains(_toy(toy_net, "X:B")) == {("21",), ("22",)}
    b1 = _trip_index(toy_net, "b1")
    assert _trains(_toy(toy_net, "X:B", exclusion=Exclusion(banned_trips={b1}))) == {("22",)}
    b2 = _trip_index(toy_net, "b2")
    assert _trains(_toy(toy_net, "X:A", exclusion=Exclusion(banned_trips={b2}))) == {("41",)}


def test_exclusion_banned_routes(toy_net):
    from src.routing.exclusion import Exclusion
    assert _trains(_toy(toy_net, "X:A", exclusion=Exclusion(banned_routes={"X:R4"}))) == {("11", "22")}
    assert _trains(_toy(toy_net, "X:A", exclusion=Exclusion(banned_routes={"X:R2"}))) == {("41",)}


def test_exclusion_banned_footpaths(toy_net):
    from src.routing.exclusion import Exclusion
    assert _toy(toy_net, "X:A", exclusion=Exclusion(banned_footpaths={("X:C", "DEST:Z")})) == []
    # a footpath not on any journey changes nothing
    assert _toy(toy_net, "X:A", exclusion=Exclusion(banned_footpaths={("X:A", "X:C")})) == _toy(toy_net, "X:A")


def test_exclusion_never_mutates_network(toy_net):
    import numpy as np

    from src.routing.exclusion import Exclusion
    _toy(toy_net, "X:A")
    names = ("dep", "arr", "board_ok", "alight_ok", "tr_from", "tr_to")
    before = {k: np.array(getattr(toy_net, k)) for k in names}
    prep = toy_net.__dict__["_raptor_prep"]
    snap = (repr(prep.p_board), repr(prep.p_alight), repr(prep.foot), repr(prep.stop_routes))
    _toy(toy_net, "X:A", exclusion=Exclusion(no_board={"X:B"}, no_alight={"X:B"}, banned_routes={"X:R4"},
                                             banned_trips={0}, banned_footpaths={("X:C", "DEST:Z")}))
    assert all(np.array_equal(before[k], getattr(toy_net, k)) for k in names)
    assert toy_net.__dict__["_raptor_prep"] is prep
    assert snap == (repr(prep.p_board), repr(prep.p_alight), repr(prep.foot), repr(prep.stop_routes))
    assert _trains(_toy(toy_net, "X:A")) == {("11", "22"), ("41",)}


def test_exclusion_unknown_ids_ignored(toy_net, caplog):
    from src.routing.exclusion import Exclusion
    js = _toy(toy_net, "X:A", exclusion=Exclusion(no_board={"X:NOPE"}, banned_routes={"X:R99"}))
    assert _trains(js) == {("11", "22"), ("41",)}
    assert "not in the network" in caplog.text


def test_exclusion_union():
    from src.routing.exclusion import Exclusion
    a = Exclusion(no_board={"X:A"}, banned_trips={1})
    b = Exclusion(no_alight={"X:B"}, banned_trips={2}, banned_routes={"X:R1"}, banned_footpaths={("X:C", "D")})
    u = a.union(b)
    assert u == b.union(a) and a.union(None) is a and a.union(Exclusion()) == a
    assert (u.no_board, u.no_alight, u.banned_trips, u.banned_routes, u.banned_footpaths) == (
        {"X:A"}, {"X:B"}, {1, 2}, {"X:R1"}, {("X:C", "D")})
