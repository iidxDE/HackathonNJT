"""Phase 2.3: router → Option adapter, checked against the hard-coded station_options()."""

from datetime import date

import pytest

from src import config, gtfs
from src.routing.adapter import AdapterReport, gtfs_utc, label_for, router_options
from src.routing.network import Network
from src.safety_net.candidates import station_options, to_utc

D, DL = config.DEMO_SERVICE_DATE, config.DEMO_DEADLINE_S

# Hard-coded options in the router window that the router does not return because
# another option departs no earlier and arrives no later (profile-dominated).
# Options arriving after deadline + ROUTER_ARRIVE_AFTER_S (10:00) are also absent; the
# test checks those by rule.
EXPECTED_DOMINATED = {
    ("ST → HB + PATH", "802"),   # 06:38→07:56, beaten by 6406 06:43→07:48
    ("ST → HB + PATH", "312"),   # 07:32→08:52, beaten by 6616 07:45→08:52
    ("ST → HB + PATH", "6320"),  # 08:15→09:32, beaten by 6620 08:20→09:24
}


def test_gtfs_utc_matches_safety_net_clock():
    for s in (0, 5 * 3600 + 17, 9 * 3600, 25 * 3600):
        assert gtfs_utc(D, s) == to_utc(D, s)


def test_label_for_signatures():
    assert label_for(("NP", (("rail", "NP", "NY"),))) == "NP → NY"
    assert label_for(("CM", (("rail", "CM", "HB"), ("path", "PATH:Hoboken", "PATH:33rd Street")))) \
        == "CM → HB + PATH"
    assert label_for(("HB", (("rail", "HB", "TS"), ("rail", "SE", "NY")))) == "HB → TS/SE → NY"


needs_data = pytest.mark.skipif(
    not ((config.GTFS_NJT_RAIL_DIR / "stop_times.txt").exists() and config.TRANSFERS_CSV.exists()),
    reason="GTFS / transfers.csv not available")


@pytest.fixture(scope="module")
def both():
    from src.build.network import build
    p = config.network_npz(D)
    if not p.exists():
        build(D)
    net = Network.load(p)
    njt, path = gtfs.load_feed(config.GTFS_NJT_RAIL_DIR), gtfs.load_feed(config.GTFS_PATH_DIR)
    rep = AdapterReport()
    return station_options(njt, path, D), router_options(net, D, DL, report=rep), rep


def _in_window(o) -> bool:
    lo = to_utc(D, DL - config.ROUTER_WINDOW_BEFORE_S)
    hi = to_utc(D, DL + config.ROUTER_DEPART_AFTER_S)
    return lo <= o[0].sched_dep <= hi


@needs_data
def test_every_hard_coded_option_is_a_router_option(both):
    hard, router, rep = both
    missing = {}
    for key, opts in hard.items():
        r = set(router[key])
        for o in filter(_in_window, opts):
            if o in r:  # dataclass equality, walk legs included
                continue
            late = o[-1].sched_arr > to_utc(D, DL + config.ROUTER_ARRIVE_AFTER_S)
            dominated = any(x[0].sched_dep >= o[0].sched_dep and x[-1].sched_arr <= o[-1].sched_arr
                            for x in router[key])
            assert late or dominated, (key, o[0].train_number)
            missing[(key, o[0].train_number)] = "late" if late else "dominated"
    assert {k for k, v in missing.items() if v == "dominated"} == EXPECTED_DOMINATED
    assert set(router) == set(hard)  # no new signatures at the demo deadline
    assert not rep.dropped.get("rail trip without trip_short_name")


@needs_data
def test_every_router_option_is_hard_coded(both):
    hard, router, _ = both
    for key, opts in router.items():
        assert set(opts) <= set(hard[key]), key


@needs_data
def test_np_3224_round_trips_exactly(both):
    hard, router, _ = both
    h = next(o for o in hard["NP → NY"] if o[0].train_number == "3224")
    r = next(o for o in router["NP → NY"] if o[0].train_number == "3224")
    assert r == h
    leg = r[0]
    assert (leg.from_stop, leg.to_stop, leg.gtfs_line, leg.direction, leg.toward_ny) == \
           ("NP", "NY", "11", h[0].direction, True)
    assert (leg.sched_dep, leg.sched_arr) == (to_utc(D, 8 * 3600 + 22 * 60), to_utc(D, 8 * 3600 + 42 * 60))


@needs_data
def test_no_router_option_arrives_later_than_hard_coded(both):
    """Walk double-count check: same first train → same or earlier arrival."""
    hard, router, _ = both
    for key, opts in router.items():
        by_train = {o[0].train_number: o for o in hard[key]}
        for o in opts:
            assert o[-1].sched_arr <= by_train[o[0].train_number][-1].sched_arr, (key, o[0].train_number)


@needs_data
def test_hoboken_walks_match_transfer_times(both):
    _, router, _ = both
    for key in ("CM → HB + PATH", "ST → HB + PATH", "RT → HB + PATH"):
        for o in router[key]:
            modes = [leg.mode.value for leg in o]
            assert modes == ["rail", "walk", "path", "walk"], (key, modes)
            assert o[1].duration_s == config.TRANSFER_MIN_S["hoboken_njt_to_path"]
            assert o[3].duration_s == config.TRANSFER_MIN_S["path_33st_to_penn_walk"]
            assert (o[1].from_stop, o[1].to_stop, o[3].to_stop) == ("HB", "PATH:Hoboken", "NY")


@needs_data
def test_router_candidates_score_like_safety_net():
    """Demo preset: router-sourced candidates give the same top option and gen_cost."""
    from dataclasses import replace

    from src.scoring.pipeline import evaluate, load_demo_data
    from src.scoring.scorer import Settings
    data = load_demo_data()
    s = Settings(n=500)
    a = {x.candidate_id: x for x in evaluate(data, replace(s, source="safety_net"))}
    b = {x.candidate_id: x for x in evaluate(data, replace(s, source="router"))}
    assert set(a) == set(b)
    for k in a:
        assert b[k].candidate.source == ("router" if k != "Drive all the way (not priced)" else "safety_net")
        assert (a[k].rank, a[k].gen_cost_usd) == (b[k].rank, b[k].gen_cost_usd), k


def test_router_options_cached(tmp_path):
    net = Network.__new__(Network)  # bare object: only the cache is exercised
    net.__dict__["_router_options"] = {(date(2026, 10, 13), 1, ("X",)): {"k": []}}
    assert router_options(net, date(2026, 10, 13), 1, ("X",)) == {"k": []}
