"""3.2b-min: alert → effects, AlertSet through the pipeline, and the app's disruption selector."""

import pytest

from src import config
from src.build.alerts_extract import Alert
from src.contracts import LegMode
from src.scoring import alerts as al

needs_data = pytest.mark.skipif(
    not (config.POOLS_PARQUET.exists() and config.DRIVE_CACHE_PARQUET.exists()
         and config.network_npz(config.DEMO_SERVICE_DATE).exists()), reason="artifacts not built")
NAMES = {"PORT JERVIS": "PO", "SLOATSBURG": "XG", "SECAUCUS UPPER LVL": "SE", "HOBOKEN": "HB"}


def _alert(**kw) -> Alert:
    base = dict(kind="suspension", lines=(), trains=(), stations=(), cut_at_station=None, delay_min=None,
                cause=None, direction=None, penn_disruption=False)
    return Alert(**{**base, **kw})


def test_penn_disruption_maps_to_avoid_penn_nrt():
    ex, n = al.alert_to_effects(al.PENN_SCENARIO, "scenario", al.PENN_SCENARIO_TEXT, {"NP"}, NAMES)
    assert ex == config.AVOID_PENN_NRT and n.affects_trip and n.label == "scenario"


def test_other_alerts_are_display_only_with_a_direction_and_scope_gate():
    trip = {"SE", "TS", "HB", "NP"}
    ex, n = al.alert_to_effects(_alert(stations=("Port Jervis", "Hoboken"), direction="both"), "replayed", "t",
                                trip, NAMES)
    assert ex is None and not n.affects_trip  # HB is only a reference point
    assert n.effect == "Not affecting your trip: Port Jervis is not on your routes."
    ex, n = al.alert_to_effects(_alert(stations=("Secaucus",), direction="toward_ny"), "replayed", "t", trip, NAMES)
    assert ex is None and n.affects_trip and "Secaucus Upper Lvl is on your routes" in n.effect
    _, n = al.alert_to_effects(_alert(stations=("Secaucus",), direction="away_from_ny"), "replayed", "t", trip, NAMES)
    assert not n.affects_trip and n.effect == "Not affecting your trip: away from NY only."


def test_alert_set_is_hashable_and_none_is_empty():
    assert al.NO_ALERTS.exclusion is None and not al.NO_ALERTS.notices and not al.NO_ALERTS.delay_shifts
    s = al.AlertSet("penn_scenario", config.AVOID_PENN_NRT, (), ())
    assert hash(s) == hash(al.AlertSet("penn_scenario", config.AVOID_PENN_NRT, (), ()))


@pytest.fixture(scope="module")
def data():
    from src.scoring.pipeline import load_demo_data
    return load_demo_data()


def _by_id(scored):
    return {s.candidate_id: s for s in scored}


@needs_data
def test_no_alert_is_the_3cae762_router_output(data):
    from src.routing.adapter import route_all
    from src.scoring.scorer import Settings
    assert data.alert_set("none") is al.NO_ALERTS
    opts, flags = data.options_and_flags(Settings())
    res = route_all(data.network, data.inputs.service_date, config.DEMO_DEADLINE_S)
    assert opts is res.options and flags == res.label_flags  # same cache entry as base=None
    from src.routing.snapshot import OPTIONS_REF, options_snapshot
    assert options_snapshot(data.network) == OPTIONS_REF.read_text(encoding="utf-8").splitlines()


@needs_data
def test_penn_scenario_bans_ny_and_greys_direct_rows(data):
    from src.scoring.pipeline import evaluate
    from src.scoring.scorer import Settings
    a = data.alert_set("penn_scenario")
    s = Settings(alert_set=a, n=500)
    opts, _ = data.options_and_flags(s)
    for label, os_ in opts.items():
        for o in os_:
            assert all(leg.from_stop != "NY" and leg.to_stop != "NY" for leg in o
                       if leg.mode in (LegMode.RAIL, LegMode.PATH)), label
    rows = _by_id(evaluate(data, s))
    for k in ("NP → NY", "SE → NY"):
        assert {"infeasible", "alert_unavailable"} <= rows[k].flags and rows[k].rank is None
        assert "Penn suspended (scenario)" in rows[k].candidate.infeasible_reason
    assert rows["CM → HB + PATH"].rank == 1


@needs_data
def test_a017_replay_changes_nothing(data):
    from src.scoring.pipeline import evaluate
    from src.scoring.scorer import Settings
    a = data.alert_set("A017")
    assert a.exclusion is None and a.notices and not any(n.affects_trip for n in a.notices)
    for n in a.notices:  # one alert in the cached A017 extraction
        assert n.label == "replayed" and not n.affects_trip
        assert n.effect == "Not affecting your trip: Port Jervis, Sloatsburg are not on your routes."
        assert not any(code in n.effect for code in ("PO", "XG", "HB", "["))  # names, not codes or lists
    x, y = _by_id(evaluate(data, Settings(n=500))), _by_id(evaluate(data, Settings(n=500, alert_set=a)))
    assert set(x) == set(y)
    for k in x:
        t = lambda s: s.candidate.planned_train.train_number if s.candidate.planned_train else None  # noqa: E731
        assert (x[k].rank, t(x[k]), x[k].gen_cost_usd) == (y[k].rank, t(y[k]), y[k].gen_cost_usd), k


@needs_data
def test_app_disruption_toggle_resimulates_and_shows_deltas():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(config.REPO_ROOT / "app" / "app.py"), default_timeout=180)
    at.run()

    def table():
        d = at.dataframe[0].value
        return getattr(d, "data", d).reset_index(drop=True)

    none0 = table()
    se_alt = none0[none0["Option"] == "SE → TS → HB + PATH"].iloc[0]
    assert se_alt["$/day"].startswith("≥ $") and "missing: fare" in se_alt["$/day"]
    assert se_alt["Risk-adjusted $"] == "—" and se_alt["Rank"] == "—" and "cost incomplete" in se_alt["Flags"]
    box = next(b for b in at.selectbox if b.label == "Disruption")
    box.set_value("penn_scenario").run()
    penn1 = table()
    assert any("Penn suspended" in e.value or "NY Penn Station suspended" in e.value for e in at.error)
    np_row = penn1[penn1["Option"] == "NP → NY"].iloc[0]
    assert np_row["Δ rank"] == "unavailable" and "Penn suspended (scenario)" in np_row["Flags"]
    box = next(b for b in at.selectbox if b.label == "Disruption")
    box.set_value("none").run()
    assert table().equals(none0)
    box = next(b for b in at.selectbox if b.label == "Disruption")
    box.set_value("penn_scenario").run()
    assert table().equals(penn1)


def test_penn_alert_for_outbound_trains_only_is_display_only():
    ex, n = al.alert_to_effects(_alert(penn_disruption=True, direction="away_from_ny"), "replayed", "t", {"NP"},
                                NAMES)
    assert ex is None and not n.affects_trip and "Penn alert for outbound trains only" in n.effect
    ex, _ = al.alert_to_effects(_alert(penn_disruption=True, direction=None), "replayed", "t", {"NP"}, NAMES)
    assert ex == config.AVOID_PENN_NRT  # direction unknown: applied


@needs_data
def test_a017_with_hoboken_named_is_still_not_affecting(data):
    """Synthetic A017 that also names Hoboken: HB is a reference point (SCOPE_IGNORE), same result."""
    from src.build.alert_labels import read_rows
    from src.build.alerts_extract import load_cache, replay
    from src.scoring.alerts import trip_stations
    from src.scoring.scorer import Settings
    row = next(r for r in read_rows()[1] if r["alert_id"] == "A017")
    names = dict(zip(data.njt.stops["stop_name"].str.upper().str.replace(".", "", regex=False),
                     data.njt.stops["stop_code"].astype(str)))
    stations = trip_stations(data.options_and_flags(Settings())[0])
    assert "HB" in stations  # Hoboken is on the routes; the gate must still ignore it
    for a in replay(row["msg_text"], load_cache()):
        a2 = type(a)(**{**a.__dict__, "stations": (*a.stations, "Hoboken")})
        ex, n = al.alert_to_effects(a2, "replayed", "t", stations, names)
        assert ex is None and not n.affects_trip and "Hoboken" not in n.effect


@needs_data
def test_penn_scenario_is_avoid_penn_nrt_and_leaves_the_no_alert_snapshot(data):
    from src.routing.snapshot import OPTIONS_REF, options_snapshot
    assert data.alert_set("penn_scenario").exclusion == config.AVOID_PENN_NRT
    assert options_snapshot(data.network) == OPTIONS_REF.read_text(encoding="utf-8").splitlines()
