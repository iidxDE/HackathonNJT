"""Load the raw rail vehicle archive, filter bad snapshots, parse times, dedupe.

Raw files are read only. Each file is one poll: data/raw/rail_vehicle_data/
<UTC date>/<HHMMSS>Z.json.gz holding a JSON list of train records whose
timestamps are New York local time with no zone marker.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

import pandas as pd

from src import config
from src.contracts import normalize_train_number

log = logging.getLogger(__name__)

_FEED_TIME_FORMAT = "%d-%b-%Y %I:%M:%S %p"  # e.g. 09-Oct-2026 07:40:24 AM


def parse_ny_local(values: pd.Series) -> pd.Series:
    """Feed timestamps (NY local, no zone) → tz-aware UTC. Unparseable/DST-invalid → NaT."""
    naive = pd.to_datetime(values, format=_FEED_TIME_FORMAT, errors="coerce")
    local = naive.dt.tz_localize(config.LOCAL_TZ, ambiguous="NaT", nonexistent="NaT")
    return local.dt.tz_convert("UTC")


def load_raw_vehicle_archive(raw_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read every poll file.

    Returns (snapshots, records): snapshots has one row per poll file
    (poll_utc, n_records, read_ok), including empty polls; records has
    one row per train record with its poll_utc.
    """
    snaps, frames = [], []
    for path in sorted(raw_dir.glob("*/*.json.gz")):
        poll_utc = pd.Timestamp(f"{path.parent.name}T{path.name[:6]}", tz="UTC")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
            ok = isinstance(payload, list)
        except (OSError, ValueError):
            payload, ok = [], False
        recs = payload if ok else []
        snaps.append({"poll_utc": poll_utc, "n_records": len(recs), "read_ok": ok})
        if recs:
            df = pd.DataFrame.from_records(recs).astype(str)
            df["poll_utc"] = poll_utc
            frames.append(df)
    snapshots = pd.DataFrame(snaps)
    records = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    log.info("raw: %d poll files (%d unreadable, %d empty), %d records",
             len(snapshots), (~snapshots["read_ok"]).sum(),
             (snapshots["n_records"] == 0).sum(), len(records))
    return snapshots, records


def normalize_records(raw: pd.DataFrame) -> pd.DataFrame:
    """Typed, renamed records. Feed line/direction kept only for diagnostics."""
    out = pd.DataFrame({
        "poll_utc": raw["poll_utc"],
        "train_number": raw["ID"].map(normalize_train_number),
        "feed_line": raw["TRAIN_LINE"],
        "feed_direction": raw["DIRECTION"],
        "next_stop_name": raw["NEXT_STOP"].str.strip(),
        "last_modified_utc": parse_ny_local(raw["LAST_MODIFIED"]),
        "sched_dep_utc": parse_ny_local(raw["SCHED_DEP_TIME"]),
        "delay_s": pd.to_numeric(raw["SEC_LATE"], errors="coerce"),
        "lat": pd.to_numeric(raw["LATITUDE"], errors="coerce"),
        "lon": pd.to_numeric(raw["LONGITUDE"], errors="coerce"),
    })
    bad = out[["last_modified_utc", "sched_dep_utc", "delay_s"]].isna().any(axis=1)
    if bad.any():
        log.info("normalize: dropped %d records with unparseable time or delay", bad.sum())
    return out[~bad].reset_index(drop=True)


def flag_snapshots(snapshots: pd.DataFrame, records: pd.DataFrame) -> pd.DataFrame:
    """Add keep/drop flags per poll.

    low_count: train count < SNAPSHOT_MIN_COUNT_FRACTION × centered rolling median.
    stale: newest LAST_MODIFIED is more than SNAPSHOT_MAX_STALENESS_S before the poll.
    """
    snaps = snapshots.sort_values("poll_utc").reset_index(drop=True)
    newest = records.groupby("poll_utc")["last_modified_utc"].max()
    snaps["newest_utc"] = snaps["poll_utc"].map(newest)
    median = (snaps["n_records"]
              .rolling(config.SNAPSHOT_ROLLING_WINDOW, center=True, min_periods=1)
              .median())
    snaps["low_count"] = snaps["n_records"] < config.SNAPSHOT_MIN_COUNT_FRACTION * median
    staleness_s = (snaps["poll_utc"] - snaps["newest_utc"]).dt.total_seconds()
    snaps["stale"] = staleness_s > config.SNAPSHOT_MAX_STALENESS_S
    snaps["keep"] = snaps["read_ok"] & ~snaps["low_count"] & ~snaps["stale"]
    log.info("snapshots: %d total, dropped %d low-count, %d stale (%d both); %d kept",
             len(snaps), snaps["low_count"].sum(), snaps["stale"].sum(),
             (snaps["low_count"] & snaps["stale"]).sum(), snaps["keep"].sum())
    return snaps


def clean_records(records: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    """Keep records from good polls; drop repeats of the same train update across polls."""
    kept_polls = snapshots.loc[snapshots["keep"], "poll_utc"]
    out = records[records["poll_utc"].isin(kept_polls)]
    n_before = len(out)
    out = (out.sort_values(["train_number", "last_modified_utc", "poll_utc"])
              .drop_duplicates(["train_number", "last_modified_utc", "next_stop_name",
                                "sched_dep_utc", "delay_s"], keep="first")
              .reset_index(drop=True))
    log.info("records: %d from kept polls, %d after dedupe", n_before, len(out))
    return out
