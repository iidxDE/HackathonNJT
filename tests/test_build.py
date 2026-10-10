from datetime import date

import pandas as pd
import pytest

from src.build import clean, join
from src.gtfs import GtfsFeed, active_service_ids, gtfs_time_to_seconds

NAMES = {"Chatham": frozenset({"CM"}), "Summit": frozenset({"ST"}),
         "New York": frozenset({"NY"}), "Secaucus": frozenset({"SE", "TS"})}


def tiny_feed() -> GtfsFeed:
    """Train 0400 (zero-padded) on Fri 10-09; train 0900 after midnight on the same service day."""
    stop_times = pd.DataFrame({
        "trip_id": ["t1", "t1", "t1", "t2", "t2"],
        "stop_id": ["s_st", "s_cm", "s_ny", "s_cm", "s_ny"],
        "stop_sequence": [1, 2, 3, 1, 2],
        "arrival_time": ["07:00:00", "07:05:00", "07:50:00", "24:30:00", "25:15:00"],
        "departure_time": ["07:00:00", "07:05:00", "07:50:00", "24:30:00", "25:15:00"],
    })
    stop_times["arrival_s"] = gtfs_time_to_seconds(stop_times["arrival_time"])
    stop_times["departure_s"] = gtfs_time_to_seconds(stop_times["departure_time"])
    return GtfsFeed(
        stops=pd.DataFrame({"stop_id": ["s_st", "s_cm", "s_ny"], "stop_code": ["ST", "CM", "NY"]}),
        routes=pd.DataFrame({"route_id": ["7"]}),
        trips=pd.DataFrame({"trip_id": ["t1", "t2"], "route_id": ["7", "7"], "service_id": ["A", "A"],
                            "trip_short_name": ["0400", "0900"], "direction_id": ["1", "1"]}),
        stop_times=stop_times,
        calendar=None,
        calendar_dates=pd.DataFrame({"service_id": ["A"], "date": ["20261009"],
                                     "exception_type": ["1"]}),
    )


def feed_records(rows: list[tuple[str, str, str, int, str]]) -> pd.DataFrame:
    """rows: (train ID as sent by the API, NEXT_STOP, SCHED_DEP_TIME, SEC_LATE, LAST_MODIFIED)."""
    raw = pd.DataFrame(rows, columns=["ID", "NEXT_STOP", "SCHED_DEP_TIME", "SEC_LATE", "LAST_MODIFIED"])
    raw = raw.astype(str).assign(TRAIN_LINE="Morris & Essex Line", DIRECTION="Eastbound",
                                 LATITUDE="40.7", LONGITUDE="-74.4",
                                 poll_utc=pd.Timestamp("2026-10-09T12:00", tz="UTC"))
    return clean.normalize_records(raw)


def joined(rows):
    idx = join.build_stop_index(tiny_feed(), [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)])
    return join.join_records(feed_records(rows), idx, NAMES)


def test_parse_ny_local_to_utc():
    out = clean.parse_ny_local(pd.Series(["09-Oct-2026 07:40:24 AM", "09-Oct-2026 11:59:00 PM"]))
    assert out[0] == pd.Timestamp("2026-10-09T11:40:24", tz="UTC")  # EDT = UTC−4
    assert out[1] == pd.Timestamp("2026-10-10T03:59:00", tz="UTC")


def test_active_service_from_calendar_dates_only():
    assert active_service_ids(tiny_feed(), date(2026, 10, 9)) == {"A"}
    assert active_service_ids(tiny_feed(), date(2026, 10, 10)) == set()


def test_zero_padded_gtfs_matches_unpadded_feed():
    j = joined([("400", "Chatham", "09-Oct-2026 07:05:30 AM", 60, "09-Oct-2026 07:04:00 AM")])
    assert j.loc[0, "join_status"] == "matched"
    assert j.loc[0, "trip_id"] == "t1" and j.loc[0, "stop_code"] == "CM"
    assert j.loc[0, "train_number"] == "400"


def test_after_midnight_joins_previous_service_day():
    j = joined([("900", "New York", "10-Oct-2026 01:15:00 AM", 0, "10-Oct-2026 01:10:00 AM")])
    assert j.loc[0, "join_status"] == "matched"
    assert j.loc[0, "service_day"] == pd.Timestamp("2026-10-09")
    assert j.loc[0, "service_day_rule"] == "prev_day"


def test_station_name_beats_nearest_time():
    # Feed schedule at NY is 13 min later than GTFS (seen in the real archive).
    j = joined([("400", "New York", "09-Oct-2026 08:03:00 AM", 120, "09-Oct-2026 07:55:00 AM")])
    assert j.loc[0, "join_status"] == "matched" and j.loc[0, "stop_code"] == "NY"
    assert j.loc[0, "time_diff_s"] == 13 * 60


def test_wrong_station_name_is_not_matched():
    j = joined([("400", "Summit", "09-Oct-2026 07:50:00 AM", 0, "09-Oct-2026 07:45:00 AM")])
    assert j.loc[0, "join_status"] == "no_stop"


def test_unknown_train_is_no_trip():
    j = joined([("1234", "Chatham", "09-Oct-2026 07:05:00 AM", 0, "09-Oct-2026 07:00:00 AM")])
    assert j.loc[0, "join_status"] == "no_trip"


def test_observation_uses_last_record_and_gtfs_delay():
    j = joined([
        ("400", "New York", "09-Oct-2026 08:03:00 AM", 60, "09-Oct-2026 07:40:00 AM"),
        ("400", "New York", "09-Oct-2026 08:03:00 AM", 180, "09-Oct-2026 07:58:00 AM"),
        ("400", "Chatham", "09-Oct-2026 07:05:00 AM", 30, "09-Oct-2026 07:04:00 AM"),
    ])
    obs = join.build_observations(j).set_index("stop_code")
    assert obs.loc["NY", "delay_s"] == 180
    assert obs.loc["NY", "n_records"] == 2
    assert obs.loc["NY", "delay_vs_gtfs_s"] == 13 * 60 + 180
    assert obs.loc["NY", "sched_dep_utc"] == pd.Timestamp("2026-10-09T11:50", tz="UTC")
    assert obs.loc["CM", "delay_vs_gtfs_s"] == 30


@pytest.mark.parametrize("staleness_s,expect_stale", [(60, False), (300, True)])
def test_stale_snapshot_flag(staleness_s, expect_stale):
    polls = pd.date_range("2026-10-09T20:00", periods=5, freq="30s", tz="UTC")
    snaps = pd.DataFrame({"poll_utc": polls, "n_records": 50, "read_ok": True})
    newest = polls - pd.Timedelta(seconds=10)
    newest = newest.where(polls != polls[2], polls[2] - pd.Timedelta(seconds=staleness_s))
    recs = pd.DataFrame({"poll_utc": polls, "last_modified_utc": newest})
    flagged = clean.flag_snapshots(snaps, recs)
    assert bool(flagged.loc[2, "stale"]) is expect_stale
    assert flagged.loc[[0, 1, 3, 4], "keep"].all()


def test_low_count_snapshot_dropped():
    polls = pd.date_range("2026-10-09T20:00", periods=7, freq="30s", tz="UTC")
    snaps = pd.DataFrame({"poll_utc": polls, "n_records": [60, 61, 59, 2, 60, 62, 61], "read_ok": True})
    recs = pd.DataFrame({"poll_utc": polls, "last_modified_utc": polls})
    flagged = clean.flag_snapshots(snaps, recs)
    assert flagged["keep"].tolist() == [True, True, True, False, True, True, True]
