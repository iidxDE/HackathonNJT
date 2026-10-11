"""Network-agnostic GTFS loading and service-day helpers.

All IDs are read as strings. Schedule times are seconds since service-day
midnight and may exceed 86,400.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path

import numpy as np
import pandas as pd

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class GtfsFeed:
    stops: pd.DataFrame
    routes: pd.DataFrame
    trips: pd.DataFrame
    stop_times: pd.DataFrame  # with arrival_s / departure_s added
    calendar: pd.DataFrame | None  # calendar.txt is optional in GTFS
    calendar_dates: pd.DataFrame | None
    shapes: pd.DataFrame | None = None  # shapes.txt is optional in GTFS


def _read(feed_dir: Path, name: str, required: bool = True) -> pd.DataFrame | None:
    path = feed_dir / name
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def gtfs_time_to_seconds(times: pd.Series) -> pd.Series:
    """'HH:MM:SS' (HH may be ≥ 24) → seconds since service-day midnight; '' → NaN."""
    parts = times.str.strip().str.split(":", expand=True)
    if parts.shape[1] != 3:
        return pd.Series(np.nan, index=times.index)
    h, m, s = (pd.to_numeric(parts[i], errors="coerce") for i in range(3))
    return h * 3600 + m * 60 + s


def gtfs_to_utc(service_date: date, seconds: float, tz: tzinfo) -> datetime:
    """GTFS time → aware UTC datetime: local noon on service_date − 12 h + seconds.

    The GTFS definition of a service-day time; equals local midnight + seconds
    except on DST-change days. The single conversion used at request time.
    """
    noon = pd.Timestamp(service_date).replace(hour=12).tz_localize(tz)
    return (noon - pd.Timedelta(hours=12) + pd.Timedelta(seconds=float(seconds))
            ).tz_convert("UTC").to_pydatetime()


def load_feed(feed_dir: Path) -> GtfsFeed:
    stop_times = _read(feed_dir, "stop_times.txt")
    stop_times["arrival_s"] = gtfs_time_to_seconds(stop_times["arrival_time"])
    stop_times["departure_s"] = gtfs_time_to_seconds(stop_times["departure_time"])
    stop_times["stop_sequence"] = stop_times["stop_sequence"].astype(int)
    return GtfsFeed(
        stops=_read(feed_dir, "stops.txt"),
        routes=_read(feed_dir, "routes.txt"),
        trips=_read(feed_dir, "trips.txt"),
        stop_times=stop_times,
        calendar=_read(feed_dir, "calendar.txt", required=False),
        calendar_dates=_read(feed_dir, "calendar_dates.txt", required=False),
        shapes=_read(feed_dir, "shapes.txt", required=False),
    )


def active_service_ids(feed: GtfsFeed, service_date: date) -> set[str]:
    """Service IDs running on a date: calendar.txt, then calendar_dates add/remove."""
    ymd = service_date.strftime("%Y%m%d")
    active: set[str] = set()
    cal = feed.calendar
    if cal is not None and len(cal):
        day_col = _WEEKDAYS[service_date.weekday()]
        mask = (cal["start_date"] <= ymd) & (cal["end_date"] >= ymd) & (cal[day_col] == "1")
        active |= set(cal.loc[mask, "service_id"])
    cd = feed.calendar_dates
    if cd is not None and len(cd):
        today = cd[cd["date"] == ymd]
        active |= set(today.loc[today["exception_type"] == "1", "service_id"])
        active -= set(today.loc[today["exception_type"] == "2", "service_id"])
    return active


def active_trips(feed: GtfsFeed, service_date: date) -> pd.DataFrame:
    """Trips running on a service date."""
    return feed.trips[feed.trips["service_id"].isin(active_service_ids(feed, service_date))]


def stop_pairs(feed: GtfsFeed, service_date: date, from_key: str, to_key: str,
               key_col: str = "stop_code") -> pd.DataFrame:
    """Trips on service_date that stop at from_key and later at to_key.

    Stops are matched on stops[key_col] (e.g. stop_code or stop_name). One row
    per trip: trip_id, route_id, direction_id, trip_short_name, dep_s (at
    from_key), arr_s (at to_key), terminal (key of the trip's last stop).
    Times are GTFS seconds since service-day midnight (may exceed 86,400).
    """
    trips = active_trips(feed, service_date)
    st = feed.stop_times[feed.stop_times["trip_id"].isin(trips["trip_id"])]
    st = st.merge(feed.stops[["stop_id", key_col]], on="stop_id")
    st = st.assign(seq=st["stop_sequence"].astype(int)).rename(columns={key_col: "key"})
    a = st.loc[st["key"] == from_key, ["trip_id", "seq", "departure_s"]]
    b = st.loc[st["key"] == to_key, ["trip_id", "seq", "arrival_s"]]
    pairs = a.merge(b, on="trip_id", suffixes=("_a", "_b"))
    pairs = pairs[pairs["seq_a"] < pairs["seq_b"]]
    terminal = st.sort_values("seq").drop_duplicates("trip_id", keep="last").set_index("trip_id")["key"]
    cols = [c for c in ("trip_id", "route_id", "direction_id", "trip_short_name") if c in trips]
    out = pairs.merge(trips[cols], on="trip_id")
    out = out.rename(columns={"departure_s": "dep_s", "arrival_s": "arr_s"})
    out["terminal"] = out["trip_id"].map(terminal)
    return (out.drop(columns=["seq_a", "seq_b"])
               .sort_values(["arr_s", "dep_s"]).reset_index(drop=True))
