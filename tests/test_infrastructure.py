import numpy as np
import pandas as pd
import pytest

from src.build import infrastructure as infra
from src.gtfs import GtfsFeed


def test_crosses_detects_segment_intersection():
    gate = ((0.0, 1.0), (2.0, 1.0))  # vertical line lon=1 from lat 0 to 2
    lat = np.array([1.0, 1.0, 1.0, 3.0])
    lon = np.array([0.0, 0.5, 1.5, 1.5])
    assert infra.crosses(lat, lon, gate).tolist() == [False, True, False]


def osm_way(way_id, tags, coords):
    return {"type": "way", "id": way_id, "tags": tags,
            "geometry": [{"lat": la, "lon": lo} for la, lo in coords]}


def fake_osm():
    """Each asset is a short E–W line; gates should be N–S through its middle."""
    def line(lat, lon0, lon1):
        return [(lat, lon0), (lat, (lon0 + lon1) / 2), (lat, lon1)]
    return {"elements": [
        osm_way(1, {"bridge:name": "Portal Bridge", "railway": "rail"}, line(40.7535, -74.097, -74.093)),
        osm_way(2, {"name": "Portal North Bridge", "railway": "rail"}, line(40.7540, -74.099, -74.091)),
        osm_way(3, {"tunnel:name": "North River Tunnels"}, line(40.7600, -74.040, -74.000)),
        osm_way(4, {"tunnel:name": "Uptown Hudson Tubes"}, line(40.7314, -74.032, -74.007)),
        osm_way(5, {"tunnel:name": "Downtown Hudson Tubes"}, line(40.7145, -74.034, -74.014)),
    ], "osm3s": {"timestamp_osm_base": "2026-10-10T00:00:00Z"}}


def test_gates_from_osm_cross_the_middle_of_each_asset():
    gates = {g.asset_name: g for g in infra.gates_from_osm(fake_osm())}
    assert set(gates) == set(infra.OSM_ASSET_COLUMNS)
    (lat_a, lon_a), (lat_b, lon_b) = gates["North River Tunnels"].lat_lon
    assert lon_a == pytest.approx(-74.020, abs=1e-3) and lon_b == pytest.approx(-74.020, abs=1e-3)
    assert abs(lat_a - lat_b) > 0.003  # ~500 m long, perpendicular to an E–W asset
    assert gates["Portal North Bridge"].column == "portal_bridge"
    assert gates["Uptown Hudson Tubes"].agency == "path"


def test_missing_osm_asset_raises():
    osm = fake_osm()
    osm["elements"] = osm["elements"][1:]
    with pytest.raises(ValueError):
        infra.gates_from_osm(osm)


def test_trip_legs_flags_only_legs_spanning_the_gate():
    # Three stops west → east along lat 40.76; the gate sits between B and C.
    stops = pd.DataFrame({"stop_id": ["a", "b", "c"], "stop_code": ["A", "B", "NY"],
                          "stop_name": ["A", "B", "NEW YORK"],
                          "stop_lat": ["40.76"] * 3, "stop_lon": ["-74.10", "-74.05", "-74.00"]})
    shape = pd.DataFrame({"shape_id": "s1", "shape_pt_sequence": [1, 2, 3, 4],
                          "shape_pt_lat": [40.76] * 4, "shape_pt_lon": [-74.10, -74.05, -74.03, -74.00]})
    stop_times = pd.DataFrame({"trip_id": "t1", "stop_id": ["a", "b", "c"], "stop_sequence": [1, 2, 3]})
    feed = GtfsFeed(
        stops=stops, routes=pd.DataFrame({"route_id": ["7"], "route_long_name": ["Morris & Essex Line"]}),
        trips=pd.DataFrame({"trip_id": ["t1"], "route_id": ["7"], "shape_id": ["s1"]}),
        stop_times=stop_times, calendar=None, calendar_dates=None, shapes=shape)
    gate = infra.Gate("North River Tunnels", "north_river_tunnels", "njt",
                      ((40.75, -74.02), (40.77, -74.02)), "osm")
    legs = infra.trip_legs(feed, [gate], stops.set_index("stop_id")["stop_code"])
    got = {(r.from_station, r.to_station): r.north_river_tunnels for r in legs.itertuples()}
    assert got == {("A", "B"): False, ("A", "NY"): True, ("B", "NY"): True}
    assert set(legs["service_pattern"]) == {"Morris & Essex Line → NEW YORK"}
