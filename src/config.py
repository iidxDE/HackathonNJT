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

FARES_CSV = REFERENCE_DIR / "njt_fares.csv"
PATH_FARES_CSV = REFERENCE_DIR / "path_fares.csv"
ALERT_SAMPLES_CSV = REFERENCE_DIR / "alert_samples.csv"  # hand-labeled; never regenerate over labels
PARKING_CSV = REFERENCE_DIR / "parking.csv"
DRIVING_COSTS_CSV = REFERENCE_DIR / "driving_costs.csv"
TOLLS_MANUAL_CSV = REFERENCE_DIR / "tolls_manual.csv"
INFRA_CSV = REFERENCE_DIR / "infra.csv"
STATIONS_SCOPE_CSV = REFERENCE_DIR / "stations_scope.csv"
TRANSFERS_CSV = REFERENCE_DIR / "transfers.csv"
OSM_ASSETS_JSON = REFERENCE_DIR / "osm" / "infra_assets.json"  # cached Overpass response
# Demo origin (lat/lon). Kept under data/reference (git-ignored) because it is a
# personal location; one row, origin_id/lat/lon.
DEMO_ORIGIN_CSV = REFERENCE_DIR / "demo_origin.csv"
# Drive destinations: main daily lot per station + Midtown garage (NYG), from OSM.
DRIVE_DESTINATIONS_CSV = REFERENCE_DIR / "drive_destinations.csv"

# --- Infrastructure exposure (src/build/infrastructure.py) -------------------
OVERPASS_URLS: tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# Gate = line perpendicular to an OSM asset's main axis through the middle of
# its length; a leg uses the asset if its GTFS shape crosses the gate.
OSM_GATE_HALF_WIDTH_M = 250.0

ARTIFACTS_DIR = DATA_DIR / "artifacts"
DRIVE_CACHE_PARQUET = ARTIFACTS_DIR / "drive_cache.parquet"
ROUTES_RAW_DIR = DATA_DIR / "cache" / "routes_raw"  # one JSON per API call; the real cache
ALERTS_CACHE_JSON = ARTIFACTS_DIR / "alerts_cache.json"

# --- Time --------------------------------------------------------------------
LOCAL_TZ = ZoneInfo("America/New_York")
SECONDS_PER_DAY = 86_400

# --- Demo scenario -----------------------------------------------------------
# Tue 10-13: avoids Columbus Day (Mon 10-12); same Portal Cutover weekday schedule.
# Used for GTFS service resolution and the drive cache.
DEMO_SERVICE_DATE = date(2026, 10, 13)
DEMO_DEADLINE_S = 9 * 3600  # default arrive-by at NY Penn, 09:00 local

# --- Drive cache (Google Routes API, build time only) ------------------------
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTES_API_KEY_ENV = "GOOGLE_MAPS_KEY"
ROUTES_FIELD_MASK = ("routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,"
                     "routes.travelAdvisory.tollInfo")
ROUTES_TOLL_PASSES: tuple[str, ...] = ("US_NJ_EZPASSNJ",)
ROUTES_TRAFFIC_MODELS: tuple[str, ...] = ("OPTIMISTIC", "BEST_GUESS", "PESSIMISTIC")
ROUTES_TIMEOUT_S = 30
ROUTES_RETRIES = 4  # on 429/5xx, exponential backoff 1, 2, 4 s
# Departure slots, local time on DEMO_SERVICE_DATE: 05:30–08:45 every 15 min (14 slots).
DRIVE_SLOT_START_S = 5 * 3600 + 30 * 60
DRIVE_SLOT_END_S = 8 * 3600 + 45 * 60
DRIVE_SLOT_STEP_S = 15 * 60

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
# A trip is toward_ny if its terminal (last stop) is one of these stop_codes.
# Pools key on toward_ny, not GTFS direction_id (NJT direction semantics vary by line).
TOWARD_NY_TERMINALS: tuple[str, ...] = ("NY", "HB")

OBSERVATIONS_PARQUET = ARTIFACTS_DIR / "observations.parquet"
VEHICLE_RECORDS_PARQUET = ARTIFACTS_DIR / "vehicle_records.parquet"
SNAPSHOTS_PARQUET = ARTIFACTS_DIR / "snapshots.parquet"
JOIN_RATES_CSV = ARTIFACTS_DIR / "join_rates.csv"

# --- Delay pools -------------------------------------------------------------
POOLS_PARQUET = ARTIFACTS_DIR / "pools.parquet"

# A run's period comes from its scheduled arrival at its GTFS terminal, local
# time of day. (name, start_s, end_s), half-open; covers 00:00–24:00. GTFS
# times past 24:00 become wall-clock time before lookup.
PERIODS: tuple[tuple[str, int, int], ...] = (
    ("early", 0, 6 * 3600),
    ("am_peak", 6 * 3600, 10 * 3600),
    ("midday", 10 * 3600, 16 * 3600),
    ("pm_peak", 16 * 3600, 19 * 3600 + 30 * 60),
    ("evening", 19 * 3600 + 30 * 60, SECONDS_PER_DAY),
)
# Fall back to the next coarser level when the effective run count is below this.
POOL_MIN_RUNS = 5
# Weekend service is a separate robustness set (CLAUDE.md §6), not pooled.
POOL_WEEKDAYS_ONLY = True

# Delay measure per stop (check A, 10-10; confirmed: NJT getStationSchedule has
# DWELL_TIME = 780 at NY on exactly the offset runs, so the feed's NY time is
# arrival + terminal dwell and SEC_LATE is the arrival delay). At FEED_DELAY_STOPS,
# 26 of 206 NY-terminating runs carry a feed schedule exactly 780 s later than
# GTFS, so delay_vs_gtfs_s jumps ~+766 s (median) from the previous stop while
# SEC_LATE moves ~+2 s. "feed": use delay_s (SEC_LATE) at those stops and
# delay_vs_gtfs_s elsewhere; "gtfs": delay_vs_gtfs_s everywhere.
# HB showed no offset (0 of 150 runs), so it stays on delay_vs_gtfs_s.
TERMINAL_DELAY_SOURCE = "feed"
FEED_DELAY_STOPS: tuple[str, ...] = ("NY",)

# --- Monte Carlo -------------------------------------------------------------
N_SAMPLES = 2_000
DEFAULT_SEED = 42

# --- Scorer user settings (defaults = demo preset) ---------------------------
DEFAULT_VOT_USD_PER_H = 30.0  # value of time: door-to-Penn time and lateness
DEFAULT_HARD_PENALTY_USD = 75.0  # flat cost of arriving late at all (0 = soft mode)
LATE_CAP_MIN = 60  # no catchable train: arrival = deadline + this (censored)

# Drive time per sample: triangular(OPTIMISTIC, BEST_GUESS, PESSIMISTIC) from
# the Routes API traffic models (sorted first). No invented spread.

# PATH is not measured: assumed on schedule; a missed connection costs one headway.
PATH_ASSUMED_HEADWAY_S = 10 * 60

# --- Transfer minimums (seconds; assumptions until checked on site) ----------
TRANSFER_MIN_S: dict[str, int] = {
    "hoboken_njt_to_path": 5 * 60,
    "path_33st_to_penn_walk": 4 * 60,
    "secaucus_same_system": 5 * 60,  # SE <-> TS (Phase 2 decision, transfers.csv)
    "newark_penn_njt_path": 5 * 60,
}

# --- Router network (Phase 2.1) -----------------------------------------------
# Cached RAPTOR arrays per service date; transfers.csv is the single transfer source.
NETWORK_NPZ_TEMPLATE = "network_{ymd}.npz"  # under ARTIFACTS_DIR
NETWORK_REPORT_MD = REPO_ROOT / "eval" / "network_report.md"
ROUTER_DEST_STOP = "DEST:NYP"  # virtual NY Penn target (NJT:NY + 0, PATH:33rd Street + walk)
ROUTER_VIRTUAL_PREFIXES: tuple[str, ...] = ("DEST:",)
# Portal cutover check: on DEMO_SERVICE_DATE no trip of these NJT routes may
# serve NY Penn (M&E 7, Gladstone 8, Montclair-Boonton 2 / 3 end at Hoboken).
CUTOVER_HOBOKEN_ROUTE_IDS: tuple[str, ...] = ("7", "8", "2", "3")

# --- Router (Phase 2.2, range RAPTOR per access station) ---------------------
ROUTER_MAX_ROUNDS = 3  # K: rides searched
ROUTER_MAX_RIDES_OUT = 2  # journeys with more rides are logged, not passed on
ROUTER_WINDOW_BEFORE_S = 150 * 60  # departures in [deadline − this, deadline]
ROUTER_ARRIVE_AFTER_S = 30 * 60  # keep arrivals ≤ deadline + this (catch-next)
ROUTER_MODE_BY_FEED: dict[str, str] = {"NJT": "rail", "PATH": "path"}


def network_npz(service_date: date) -> Path:
    return ARTIFACTS_DIR / NETWORK_NPZ_TEMPLATE.format(ymd=service_date.strftime("%Y%m%d"))


# --- Safety-net candidates (1.3; estimates, flagged in the UI) ---------------
SAFETY_MARGIN_S = 5 * 60  # slack between parking and the station buffer
# Station buffer: park → platform. SE includes the garage and the upper level.
STATION_BUFFER_S: dict[str, int] = {"CM": 4 * 60, "ST": 4 * 60, "RT": 4 * 60,
                                    "NP": 6 * 60, "SE": 8 * 60}
GARAGE_TO_PENN_WALK_S = 5 * 60  # drive-all-the-way: Midtown garage → NY Penn
CATCH_NEXT_K = 4  # trains kept after the planned one (incl. after the deadline)
TRAIN_CHOICE_T = 3  # plan the last T on-time trains per station; the scorer picks the cheapest
LEAVE_HOME_MAX_ROUNDS = 3  # fixed-point iterations for leave_home
LEAVE_HOME_TOL_S = 30
PATH_HOBOKEN_STOP = "Hoboken"  # PATH GTFS stop_name; Leg id "PATH:Hoboken"
PATH_33RD_STOP = "33rd Street"
GARAGE_DEST = "NYG"  # drive_destinations.csv id for the Midtown garage

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
