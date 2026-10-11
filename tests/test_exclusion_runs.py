"""Phase 3.1: exclusion runs on the demo network (regression + integration)."""

import pandas as pd
import pytest

from src import config
from src.contracts import LegMode
from src.routing.adapter import route_all
from src.routing.network import Network
from src.routing.snapshot import JOURNEYS_REF, journeys_snapshot

D, DL = config.DEMO_SERVICE_DATE, config.DEMO_DEADLINE_S
ALT_FLAG = f"{config.ALT_FLAG_PREFIX}penn_nrt"
SE_ALT = "SE → TS → HB + PATH"

needs_net = pytest.mark.skipif(
    not ((config.GTFS_NJT_RAIL_DIR / "stop_times.txt").exists() and config.TRANSFERS_CSV.exists()),
    reason="GTFS / transfers.csv not available")


@pytest.fixture(scope="module")
def net() -> Network:
    from src.build.network import build
    p = config.network_npz(D)
    if not p.exists():
        build(D)
    return Network.load(p)


@needs_net
def test_exclusion_none_identical_to_router_v1(net):
    from src.routing.exclusion import Exclusion
    ref = JOURNEYS_REF.read_text(encoding="utf-8").splitlines()
    assert journeys_snapshot(net) == ref
    assert journeys_snapshot(net, exclusion=None) == ref
    assert journeys_snapshot(net, exclusion=Exclusion()) == ref
    plain = route_all(net, D, DL, exclusions={})
    assert not plain.label_flags and SE_ALT not in plain.options


@needs_net
def test_se_alternative_ts_hb_path_0800_0900(net):
    """SE → walk TS → Main/Bergen/Pascack to HB → PATH 33rd St, arriving 08:00–09:00 on 10-13."""
    res = route_all(net, D, DL)
    assert res.label_flags.get(SE_ALT) == frozenset({ALT_FLAG})
    opts = res.options[SE_ALT]
    arrivals = [pd.Timestamp(o[-1].sched_arr).tz_convert(config.LOCAL_TZ) for o in opts]
    assert any(t.hour == 8 or (t.hour, t.minute) == (9, 0) for t in arrivals)
    for o in opts:
        assert [leg.mode for leg in o] == [LegMode.WALK, LegMode.RAIL, LegMode.WALK, LegMode.PATH, LegMode.WALK]
        assert (o[0].from_stop, o[0].to_stop, o[1].to_stop) == ("SE", "TS", "HB")
        assert (o[3].from_stop, o[3].to_stop) == ("PATH:Hoboken", "PATH:33rd Street")


@needs_net
def test_exclusion_runs_never_touch_ny_and_add_no_key_at_hb_path_stations(net):
    res = route_all(net, D, DL)
    for _, label, opt in res.report.exclusion_options:
        assert all(leg.from_stop != "NY" and leg.to_stop != "NY"
                   for leg in opt if leg.mode in (LegMode.RAIL, LegMode.PATH)), (label, opt)
    for label in res.label_flags:
        assert res.options[label][0][0].from_stop not in ("CM", "ST", "RT"), label
    # union, never a replacement: every plain-run key keeps its plain options
    plain = route_all(net, D, DL, exclusions={})
    for label, opts in plain.options.items():
        assert set(opts) <= set(res.options[label])
    # NP via Journal Square is listed as unscored, not silently dropped
    assert any(r["station"] == "NP" and "Journal Square" in r["key"] for r in res.report.unscored_alternatives)


@needs_net
def test_router_cache_key_includes_exclusions(net):
    a = route_all(net, D, DL)
    b = route_all(net, D, DL, exclusions={})
    assert a is route_all(net, D, DL) and b is route_all(net, D, DL, exclusions={})
    assert SE_ALT in a.options and SE_ALT not in b.options


@needs_net
def test_base_none_identical_to_3cae762(net):
    """route_all(base=None) and base=Exclusion() reproduce the 3.1 output exactly."""
    from src.routing.exclusion import Exclusion
    from src.routing.snapshot import OPTIONS_REF, options_snapshot
    ref = OPTIONS_REF.read_text(encoding="utf-8").splitlines()
    assert options_snapshot(net) == ref
    assert options_snapshot(net, base=Exclusion()) == ref


@needs_net
def test_base_is_composed_into_every_run(net):
    """A base ban (here: train 1612's trip) disappears from the plain run and from every exclusion run."""
    from src.routing.exclusion import Exclusion
    plain = route_all(net, D, DL)
    g = next(i for i, (t, lab) in enumerate(zip(net.trip_ids, net.trip_labels)) if str(lab) == "1612")
    based = route_all(net, D, DL, base=Exclusion(banned_trips={g}))
    assert based is not plain and based is route_all(net, D, DL, base=Exclusion(banned_trips={g}))

    def trains(res):
        return {leg.train_number for opts in res.options.values() for o in opts for leg in o if leg.train_number}
    assert "1612" in trains(plain) and "1612" not in trains(based)
    assert SE_ALT in based.options and based.label_flags.get(SE_ALT) == frozenset({ALT_FLAG})
    # NY banned in the base: no option of any run may board or alight at NY
    ny = route_all(net, D, DL, base=config.AVOID_PENN_NRT)
    for _, label, opt in [(None, k, o) for k, os in ny.options.items() for o in os]:
        assert all(leg.from_stop != "NY" and leg.to_stop != "NY"
                   for leg in opt if leg.mode in (LegMode.RAIL, LegMode.PATH)), label
