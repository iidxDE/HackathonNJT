"""Offline build: python -m src.build

Raw archive + GTFS → data/artifacts/ (snapshots, joined records, observations).
Rerunnable; reads data/raw/ only, never writes to it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from src import config
from src.build import clean, join
from src.gtfs import load_feed

log = logging.getLogger("src.build")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    snapshots, raw = clean.load_raw_vehicle_archive(config.RAW_VEHICLE_DIR)
    records = clean.normalize_records(raw)
    snapshots = clean.flag_snapshots(snapshots, records)
    records = clean.clean_records(records, snapshots)

    feed = load_feed(config.GTFS_NJT_RAIL_DIR)
    local_days = records["sched_dep_utc"].dt.tz_convert(config.LOCAL_TZ).dt.date.unique()
    service_dates = {d - timedelta(days=k) for d in local_days for k in (0, 1)}
    stop_index = join.build_stop_index(feed, service_dates)

    names = join.station_name_codes(config.RAW_STATION_SCHEDULE_DIR)
    joined = join.join_records(records, stop_index, names)
    rates = join.log_join_rates(joined)
    observations = join.build_observations(joined)

    snapshots.to_parquet(config.SNAPSHOTS_PARQUET, index=False)
    joined.to_parquet(config.VEHICLE_RECORDS_PARQUET, index=False)
    observations.to_parquet(config.OBSERVATIONS_PARQUET, index=False)
    rates.to_csv(config.JOIN_RATES_CSV)
    log.info("wrote artifacts to %s", config.ARTIFACTS_DIR)


if __name__ == "__main__":
    main()
