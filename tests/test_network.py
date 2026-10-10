"""Phase 2.1: network-agnostic loader and the NJT + PATH build."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src import config
from src.routing.network import (FeedSpec, Network, Transfers, assert_fifo, build_network,
                                 load_timetable, load_transfers)

D = date(2026, 10, 13)  # Tuesday


def _write(d, name, text):
    (d / name).write_text(text.strip() + "\n", encoding="utf-8")


@pytest.fixture
def toy_feed(tmp_path):
    """calendar_dates only; platforms with a parent; overtaking; > 24:00; pickup rules."""
    _write(tmp_path, "stops.txt", """
stop_id,stop_code,stop_name,location_type,parent_station
A,AA,Alpha,1,
A1,,Alpha p1,0,A
B,BB,Bravo,,
C,CC,Charlie,,
""")
    _write(tmp_path, "routes.txt", "route_id,route_short_name\nR,RED")
    _write(tmp_path, "trips.txt", """
route_id,service_id,trip_id,trip_short_name
R,WK,t1,0100
R,WK,t2,0200
R,WK,t3,
R,WE,t4,0400
""")
    _write(tmp_path, "calendar_dates.txt", "service_id,date,exception_type\nWK,20261013,1\nWE,20261017,1")
    # t2 departs after t1 but arrives at C first (overtakes) → split.
    # t3 runs past midnight. t1 has no drop-off at B.
    _write(tmp_path, "stop_times.txt", """
trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type
t1,08:00:00,08:00:00,A1,1,0,0
t1,08:10:00,08:11:00,B,2,0,1
t1,08:40:00,08:40:00,C,3,0,0
t2,08:05:00,08:05:00,A1,1,0,0
t2,08:12:00,08:12:00,B,2,0,0
t2,08:30:00,08:30:00,C,3,0,0
t3,24:10:00,24:10:00,A1,1,0,0
t3,24:20:00,24:20:00,B,2,,
t3,24:50:00,24:50:00,C,3,0,0
t4,09:00:00,09:00:00,A1,1,0,0
t4,09:30:00,09:30:00,C,3,0,0
""")
    return FeedSpec("X", tmp_path, stop_key="stop_code")


def test_loader_rules(toy_feed):
    tt = load_timetable(toy_feed, D)
    assert set(tt.trips["trip"]) == {"X:t1", "X:t2", "X:t3"}  # WE not active
    assert set(tt.stop_times["stop"]) == {"X:AA", "X:BB", "X:CC"}  # A1 → parent A
    assert tt.stop_times["arr_s"].max() == 24 * 3600 + 50 * 60
    labels = dict(zip(tt.trips["trip"], tt.trips["label"]))
    assert labels["X:t1"] == "100" and labels["X:t3"] == "RED@24:10 AA"
    b = tt.stop_times[(tt.stop_times["trip"] == "X:t1") & (tt.stop_times["stop"] == "X:BB")]
    assert not b["alight_ok"].item() and b["board_ok"].item()


def test_fifo_split_and_index(toy_feed):
    tt = load_timetable(toy_feed, D)
    net = build_network([tt], Transfers(
        pd.DataFrame({"from_stop": ["X:CC"], "to_stop": ["DEST:Z"],
                                        "min_transfer_s": [60]}), 120), D)
    assert net.stats["fifo_splits"] == {"X": 1}
    assert net.n_patterns == 2 and assert_fifo(net) == []
    s = net.stop_index()
    assert "DEST:Z" in s
    i = s["X:BB"]
    assert set(net.sp_pattern[net.sp_ptr[i]:net.sp_ptr[i + 1]]) == {0, 1}
    assert set(net.sp_pos[net.sp_ptr[i]:net.sp_ptr[i + 1]]) == {1}


def test_save_load_roundtrip(toy_feed, tmp_path):
    tt = load_timetable(toy_feed, D)
    net = build_network([tt], Transfers(pd.DataFrame(columns=["from_stop", "to_stop",
                                                              "min_transfer_s"]), 300), D)
    p = tmp_path / "n.npz"
    net.save(p)
    back = Network.load(p)
    assert back.service_date == "20261013" and back.same_stop_transfer_s == 300
    assert np.array_equal(back.arr, net.arr) and list(back.trip_labels) == list(net.trip_labels)
    assert back.trip_direction.tolist() == [-1] * len(net.trip_ids)  # toy feed has no direction_id
    g, p = back.trip_index()["X:t1"]
    assert back.terminal_of_pattern(p) == "X:CC"


def test_unknown_transfer_stop_fails(toy_feed):
    tt = load_timetable(toy_feed, D)
    with pytest.raises(ValueError, match="not in the timetable"):
        build_network([tt], Transfers(pd.DataFrame({"from_stop": ["X:ZZ"], "to_stop": ["X:AA"],
                                                    "min_transfer_s": [60]}), 300), D)


needs_gtfs = pytest.mark.skipif(
    not ((config.GTFS_NJT_RAIL_DIR / "stop_times.txt").exists() and config.TRANSFERS_CSV.exists()),
    reason="GTFS / transfers.csv not available")


@needs_gtfs
def test_demo_network_build():
    from src.build.network import build
    net, md = build(config.DEMO_SERVICE_DATE)
    s = net.stop_index()
    for stop in ("NJT:CM", "NJT:SE", "NJT:TS", "NJT:NP", "NJT:HB", "NJT:NY",
                 "PATH:Hoboken", "PATH:33rd Street", "PATH:Newark", config.ROUTER_DEST_STOP):
        assert stop in s
    labels = set(net.trip_labels)
    assert {"3224", "3926", "872"} <= labels  # safety-net plans on 10-13
    assert any(lab.startswith("BLU@") for lab in labels)
    assert "All build asserts passed" in md
    t = load_transfers(config.TRANSFERS_CSV)
    assert t.same_stop_s == config.TRANSFER_MIN_S["hoboken_njt_to_path"]


def test_unwrap_midnight():
    from src.routing.network import unwrap_midnight
    h = 3600
    st = pd.DataFrame({"trip": ["p"] * 4 + ["q"] * 2,
                       "arr_s": [23 * h + 3300, 23 * h + 3582, 72, 630, 0, 600],
                       "dep_s": [23 * h + 3300, 0, 90, 630, 0, 600]})  # wraps between arr and dep
    out, fixed = unwrap_midnight(st)
    day = 86_400
    assert out["dep_s"].tolist()[:4] == [23 * h + 3300, day, day + 90, day + 630]
    assert out["arr_s"].tolist()[4:] == [0, 600] and fixed == 1  # q untouched
    with pytest.raises(ValueError, match="backwards"):
        unwrap_midnight(pd.DataFrame({"trip": ["r", "r"], "arr_s": [600, 300], "dep_s": [600, 300]}))
