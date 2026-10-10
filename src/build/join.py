"""Join cleaned feed records to GTFS trips and stops, then reduce to run × stop observations.

Join key: train number (leading zeros stripped on both sides) + service date.
Within the trip, the stop is the one whose 2-char code matches the feed's
NEXT_STOP name (via the raw station schedule's name → code list), with the
feed's SCHED_DEP_TIME closest to the GTFS departure breaking ties.
After the join, GTFS is canonical for route and direction.

Service-day interpretations tried for each record (local calendar day C,
local time-of-day t):
  'same_day'      service day C,     GTFS time t
  'prev_day'      service day C − 1, GTFS time t + 24h   (standard GTFS)
  'calendar_day'  service day C,     GTFS time t + 24h   (seen in NJT weekend
                  services: after-midnight trips filed under the day they run)
"""

from __future__ import annotations

import gzip
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path

import pandas as pd

from src import config
from src.contracts import normalize_train_number
from src.gtfs import GtfsFeed, active_trips

log = logging.getLogger(__name__)

_DAY_S = config.SECONDS_PER_DAY
# (rule, service-day offset in days, seconds added to the feed's local time-of-day)
_SERVICE_DAY_RULES = (
    ("same_day", 0, 0),
    ("prev_day", 1, _DAY_S),
    ("calendar_day", 0, _DAY_S),
)


def station_name_codes(schedule_dir: Path) -> dict[str, frozenset[str]]:
    """Feed station name → set of 2-char codes (e.g. 'Secaucus' → {SE, TS})."""
    names: dict[str, set[str]] = defaultdict(set)
    for path in sorted(schedule_dir.glob("*/*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for station in json.load(fh):
                names[station["STATIONNAME"].strip()].add(station["STATION_2CHAR"].strip())
    return {k: frozenset(v) for k, v in names.items()}


def build_stop_index(feed: GtfsFeed, service_dates: Iterable[date]) -> pd.DataFrame:
    """One row per (service day, train number, stop visit) for active trips."""
    stops = feed.stops[["stop_id", "stop_code"]]
    per_day = []
    for d in sorted(set(service_dates)):
        trips = active_trips(feed, d)[["trip_id", "route_id", "direction_id", "trip_short_name"]]
        if trips.empty:
            continue
        trips = trips.assign(service_day=pd.Timestamp(d),
                             train_number=trips["trip_short_name"].map(normalize_train_number))
        per_day.append(trips.drop(columns="trip_short_name"))
    if not per_day:
        raise ValueError("No active GTFS trips on any requested service date")
    trips = pd.concat(per_day, ignore_index=True)
    dupes = trips[trips.duplicated(["service_day", "train_number"], keep=False)]
    if len(dupes):
        log.info("stop index: %d trips share a (service day, train number); numbers: %s",
                 len(dupes), sorted(dupes["train_number"].unique())[:10])
    st = feed.stop_times[["trip_id", "stop_id", "stop_sequence", "departure_s"]]
    return trips.merge(st, on="trip_id").merge(stops, on="stop_id", how="left")


def join_records(records: pd.DataFrame, stop_index: pd.DataFrame,
                 name_codes: Mapping[str, frozenset[str]],
                 max_diff_s: float = config.STOP_MATCH_MAX_DIFF_S) -> pd.DataFrame:
    """Attach GTFS trip and stop to each record.

    Adds service_day, day_shift_s (add to service_day + departure_s to get
    local wall time), service_day_rule, trip/stop columns, time_diff_s and
    join_status: 'matched', 'no_stop' (trip found, no stop with that name
    within max_diff_s) or 'no_trip'. Records whose station name is unknown
    fall back to time-only matching within STOP_SCHEDULE_AGREE_S.
    """
    recs = records.reset_index(drop=True).copy()
    recs["rec_idx"] = recs.index
    local = recs["sched_dep_utc"].dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None)
    cal_day = local.dt.normalize()
    tod_s = (local - cal_day).dt.total_seconds()

    candidates = []
    for rule, day_offset, add_s in _SERVICE_DAY_RULES:
        probe = pd.DataFrame({
            "rec_idx": recs["rec_idx"],
            "train_number": recs["train_number"],
            "service_day": cal_day - pd.Timedelta(days=day_offset),
            "feed_gtfs_s": tod_s + add_s,
            "service_day_rule": rule,
            "day_shift_s": day_offset * _DAY_S - add_s,
            "next_stop_name": recs["next_stop_name"],
        })
        candidates.append(probe.merge(stop_index, on=["service_day", "train_number"]))
    cand = pd.concat(candidates, ignore_index=True)
    cand["time_diff_s"] = (cand["departure_s"] - cand["feed_gtfs_s"]).abs()

    expected = cand["next_stop_name"].map(name_codes)
    name_known = expected.notna()
    name_ok = pd.Series(
        [code in codes for code, codes in zip(cand.loc[name_known, "stop_code"],
                                              expected[name_known])],
        index=cand.index[name_known], dtype=bool,
    ).reindex(cand.index, fill_value=False)
    limit = name_known.map({True: max_diff_s, False: config.STOP_SCHEDULE_AGREE_S})
    cand["ok"] = (name_ok | ~name_known) & (cand["time_diff_s"] <= limit)

    # Prefer valid matches, then smallest time difference.
    best = (cand.sort_values(["ok", "time_diff_s"], ascending=[False, True])
                .drop_duplicates("rec_idx").set_index("rec_idx"))
    cols = ["service_day", "day_shift_s", "service_day_rule", "trip_id", "route_id",
            "direction_id", "stop_id", "stop_code", "stop_sequence", "departure_s",
            "time_diff_s", "ok"]
    out = recs.join(best[cols], on="rec_idx").drop(columns="rec_idx")
    trip_found = out["trip_id"].notna()
    matched = trip_found & out["ok"].fillna(False).astype(bool)
    out["join_status"] = "no_trip"
    out.loc[trip_found, "join_status"] = "no_stop"
    out.loc[matched, "join_status"] = "matched"
    stop_cols = ["stop_id", "stop_code", "stop_sequence", "departure_s"]
    out.loc[~matched, stop_cols] = pd.NA
    return out.drop(columns="ok")


def operating_day(poll_utc: pd.Series) -> pd.Series:
    """Approximate service day for unjoined records (day boundary 04:00 local)."""
    local = poll_utc.dt.tz_convert(config.LOCAL_TZ).dt.tz_localize(None)
    return (local - pd.Timedelta(hours=4)).dt.normalize()


def log_join_rates(joined: pd.DataFrame) -> pd.DataFrame:
    """Per feed line: record-level and run-level join rates. Returns the table."""
    df = joined.assign(
        run_day=joined["service_day"].fillna(operating_day(joined["poll_utc"])),
        ok=joined["join_status"] == "matched",
    )
    runs = df.groupby(["feed_line", "train_number", "run_day"])["ok"].any().reset_index()
    table = pd.DataFrame({
        "records": df.groupby("feed_line").size(),
        "record_join_rate": df.groupby("feed_line")["ok"].mean(),
        "runs": runs.groupby("feed_line").size(),
        "run_join_rate": runs.groupby("feed_line")["ok"].mean(),
    }).sort_values("runs", ascending=False)
    log.info("join: status %s; rules used %s; overall record rate %.4f, run rate %.4f",
             df["join_status"].value_counts().to_dict(),
             df.loc[df["ok"], "service_day_rule"].value_counts().to_dict(),
             df["ok"].mean(), runs["ok"].mean())
    log.info("join rate by feed line:\n%s", table.to_string(float_format="{:.4f}".format))
    unknown = sorted(df.loc[df["join_status"] != "matched", "train_number"].unique())
    log.info("unjoined train numbers (%d): %s", len(unknown), unknown[:20])
    return table


def build_observations(joined: pd.DataFrame,
                       excluded_route_ids: Iterable[str] = config.EXCLUDED_ROUTE_IDS) -> pd.DataFrame:
    """Run × stop table from the last record seen while the stop was NEXT_STOP.

    delay_s is SEC_LATE (vs the feed's own schedule). delay_vs_gtfs_s is the
    estimated departure (feed schedule + SEC_LATE) minus the GTFS time, i.e.
    lateness against the published timetable.
    """
    m = joined[(joined["join_status"] == "matched")
               & ~joined["route_id"].isin(list(excluded_route_ids))]
    keys = ["service_day", "train_number", "stop_sequence"]
    m = m.sort_values("last_modified_utc")
    last = m.groupby(keys, as_index=False).last()
    counts = m.groupby(keys).size().rename("n_records")
    o = last.merge(counts, on=keys)
    gtfs_local = o["service_day"] + pd.to_timedelta(
        o["departure_s"].astype(float) + o["day_shift_s"].astype(float), unit="s")
    gtfs_utc = gtfs_local.dt.tz_localize(config.LOCAL_TZ).dt.tz_convert("UTC")
    est_utc = o["sched_dep_utc"] + pd.to_timedelta(o["delay_s"], unit="s")
    obs = pd.DataFrame({
        "service_date": o["service_day"].dt.date,
        "train_number": o["train_number"],
        "trip_id": o["trip_id"],
        "route_id": o["route_id"],
        "direction_id": o["direction_id"].astype(int),
        "stop_id": o["stop_id"],
        "stop_code": o["stop_code"],
        "stop_sequence": o["stop_sequence"].astype(int),
        "sched_dep_s": o["departure_s"].astype(int),
        "sched_dep_utc": gtfs_utc,
        "feed_sched_dep_utc": o["sched_dep_utc"],
        "delay_s": o["delay_s"].astype(int),
        "est_dep_utc": est_utc,
        "delay_vs_gtfs_s": (est_utc - gtfs_utc).dt.total_seconds().astype(int),
        "observed_utc": o["last_modified_utc"],
        "n_records": o["n_records"].astype(int),
        "service_day_rule": o["service_day_rule"],
    }).sort_values(["service_date", "train_number", "stop_sequence"]).reset_index(drop=True)

    n_runs = obs.groupby(["service_date", "train_number"]).ngroups
    log.info("observations: %d run×stop rows, %d runs (excluded routes %s)",
             len(obs), n_runs, list(excluded_route_ids))
    runs = obs.drop_duplicates(["service_date", "train_number"])
    log.info("runs by route_id × direction_id:\n%s",
             runs.pivot_table(index="route_id", columns="direction_id", values="train_number",
                              aggfunc="count", fill_value=0).to_string())
    disagree = (obs["sched_dep_utc"] - obs["feed_sched_dep_utc"]).dt.total_seconds().abs() \
        > config.STOP_SCHEDULE_AGREE_S
    log.info("schedule disagreement (feed vs GTFS > %ds): %d of %d rows; by route × stop:\n%s",
             config.STOP_SCHEDULE_AGREE_S, disagree.sum(), len(obs),
             obs[disagree].groupby(["route_id", "stop_code"]).size()
                .sort_values(ascending=False).head(10).to_string())
    return obs
