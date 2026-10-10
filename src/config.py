"""Single source of tunable values: paths, sampling, pooling, transfers, assumptions.

No magic numbers in logic modules; import them from here.
Times of day are seconds since service-day midnight, New York local (GTFS convention).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Paths -------------------------------------------------------------------
# Data is git-ignored, so worktrees have none. Point CROSSING_DATA_DIR at the
# main checkout's data/ when running from a worktree.
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("CROSSING_DATA_DIR", REPO_ROOT / "data"))

RAW_DIR = DATA_DIR / "raw"
RAW_VEHICLE_DIR = RAW_DIR / "rail_vehicle_data"
RAW_STATION_MSG_DIR = RAW_DIR / "rail_station_msg"
RAW_STATION_SCHEDULE_DIR = RAW_DIR / "rail_station_schedule"

REFERENCE_DIR = DATA_DIR / "reference"
GTFS_DIR = REFERENCE_DIR / "gtfs"
GTFS_NJT_RAIL_DIR = GTFS_DIR / "njtransit_gtfs_rail"
GTFS_PATH_DIR = GTFS_DIR / "PATH GTFS"

FARES_CSV = REFERENCE_DIR / "fares.csv"
PARKING_CSV = REFERENCE_DIR / "parking.csv"
DRIVING_COSTS_CSV = REFERENCE_DIR / "driving_costs.csv"
TOLLS_MANUAL_CSV = REFERENCE_DIR / "tolls_manual.csv"
INFRA_CSV = REFERENCE_DIR / "infra.csv"
STATIONS_SCOPE_CSV = REFERENCE_DIR / "stations_scope.csv"
TRANSFERS_CSV = REFERENCE_DIR / "transfers.csv"

ARTIFACTS_DIR = DATA_DIR / "artifacts"
DRIVE_CACHE_JSON = ARTIFACTS_DIR / "drive_cache.json"
ALERTS_CACHE_JSON = ARTIFACTS_DIR / "alerts_cache.json"

# --- Time --------------------------------------------------------------------
LOCAL_TZ = ZoneInfo("America/New_York")
SECONDS_PER_DAY = 86_400

# --- Demo scenario -----------------------------------------------------------
DEMO_SERVICE_DATE = date(2026, 10, 12)  # Mon, first day of the new timetable
DEMO_WINDOW_START_S = 6 * 3600 + 30 * 60  # 06:30 local
DEMO_WINDOW_END_S = 8 * 3600 + 30 * 60  # 08:30 local

# --- Stations (NJ Transit STATION_2CHAR codes) -------------------------------
ACCESS_STATIONS: tuple[str, ...] = ("CM", "ST", "RT", "SE", "NP", "HB")
DESTINATION_STATION = "NY"

# --- Raw archive cleaning ----------------------------------------------------
SNAPSHOT_MIN_COUNT_FRACTION = 0.5  # drop if train count < this × rolling median
SNAPSHOT_ROLLING_WINDOW = 21  # snapshots (~10 min at 30 s polling)
SNAPSHOT_MAX_STALENESS_S = 120  # drop if newest record is older than this

# --- Feed ↔ GTFS join --------------------------------------------------------
# Stops are matched by station name (feed name → 2-char code); time breaks ties.
# Feed SCHED_DEP_TIME vs GTFS departure at the matched stop must be within
# STOP_MATCH_MAX_DIFF_S. Typical offsets are ≤ 45 s (GTFS rounds to the minute),
# but some NEC/NJCL/M&E arrivals at NY carry a feed schedule exactly 13 min
# later than GTFS. Diffs above STOP_SCHEDULE_AGREE_S are logged as disagreements.
STOP_MATCH_MAX_DIFF_S = 30 * 60
STOP_SCHEDULE_AGREE_S = 120
# Port Jervis runs to Hoboken, not Penn; out of scope (GTFS route_id 6).
EXCLUDED_ROUTE_IDS: tuple[str, ...] = ("6",)

OBSERVATIONS_PARQUET = ARTIFACTS_DIR / "observations.parquet"
VEHICLE_RECORDS_PARQUET = ARTIFACTS_DIR / "vehicle_records.parquet"
SNAPSHOTS_PARQUET = ARTIFACTS_DIR / "snapshots.parquet"
JOIN_RATES_CSV = ARTIFACTS_DIR / "join_rates.csv"

# --- Delay pools -------------------------------------------------------------
# (name, start_s, end_s), half-open, local time of day. Covers 00:00–24:00;
# GTFS times past 24:00 are taken modulo SECONDS_PER_DAY before lookup.
PERIODS: tuple[tuple[str, int, int], ...] = (
    ("overnight", 0, 5 * 3600),
    ("early", 5 * 3600, 6 * 3600),
    ("am_peak", 6 * 3600, 9 * 3600 + 30 * 60),
    ("shoulder_am", 9 * 3600 + 30 * 60, 11 * 3600),
    ("midday", 11 * 3600, 15 * 3600 + 30 * 60),
    ("pm_peak", 15 * 3600 + 30 * 60, 19 * 3600),
    ("evening", 19 * 3600, SECONDS_PER_DAY),
)
POOL_MIN_RUNS = 8  # below this, fall back to the next coarser pool level

# --- Monte Carlo -------------------------------------------------------------
N_SAMPLES = 2_000
DEFAULT_SEED = 20261012

# Assumption, not observed: sampled drive time = cached duration × lognormal
# factor with median 1 and this sigma (≈ ±15% typical spread).
DRIVE_TIME_LOGNORMAL_SIGMA = 0.15

# PATH is not measured: assumed on schedule; a missed connection costs one headway.
PATH_ASSUMED_HEADWAY_S = 10 * 60

# --- Transfer minimums (seconds; assumptions until checked on site) ----------
TRANSFER_MIN_S: dict[str, int] = {
    "park_to_platform": 5 * 60,  # drive arrival → rail/PATH boarding, any station
    "hoboken_njt_to_path": 5 * 60,
    "path_33st_to_penn_walk": 10 * 60,
    "secaucus_same_system": 6 * 60,
    "newark_penn_njt_path": 5 * 60,
}

# --- Lateness ----------------------------------------------------------------
# Late-arrival buckets in minutes, half-open [lo, hi). The user's
# cost_of_lateness assigns a dollar cost to each bucket.
LATENESS_BUCKETS_MIN: tuple[tuple[float, float], ...] = (
    (0, 5),
    (5, 15),
    (15, 30),
    (30, float("inf")),
)
ON_TIME_GRACE_S = 0  # arrival ≤ deadline + grace counts as on time

# --- User defaults -----------------------------------------------------------
DEFAULT_MPG = 28.0
