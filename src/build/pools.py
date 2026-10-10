"""Delay pools: observations.parquet → pools.parquet (long format).

python -m src.build.pools   rebuilds pools from existing observations and
                            writes the evidence table (eval/pool_evidence.md).

One row per (pool level, run, stop) with delay_used_s, the delay measure
chosen per stop by config.TERMINAL_DELAY_SOURCE. Each run appears once per
level: L1 keyed (line, toward_ny, period), L2 (toward_ny, period), L3
(toward_ny); coarser levels store None for the dropped keys. Only observed
stops are stored; missing stops are never filled in.

Run id = (service_date, train_number, trip_id). A run's period comes from its
scheduled arrival at its GTFS terminal, New York local time.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.contracts import DelayField, FallbackLevel
from src.scoring.pools import RUN_ID, delay_field_for, level_counts, period_of, resolve

log = logging.getLogger(__name__)

EVIDENCE_MD = config.REPO_ROOT / "eval" / "pool_evidence.md"

# Demo rail legs on Tue 10-13 (config.DEMO_SERVICE_DATE; Portal cutover: M&E/Gladstone end at HB),
# plus pre-cutover CM/ST → NY for comparison. (board, alight, line, label)
DEMO_LEGS: tuple[tuple[str, str, str, str], ...] = (
    ("CM", "HB", "7", "M&E"),
    ("ST", "HB", "7", "M&E"),
    ("RT", "HB", "7", "M&E"),
    ("ST", "HB", "8", "Gladstone"),
    ("SE", "NY", "10", "NEC"),
    ("SE", "NY", "11", "NJCL"),
    ("NP", "NY", "10", "NEC"),
    ("NP", "NY", "11", "NJCL"),
    ("CM", "NY", "7", "M&E (pre-cutover only)"),
    ("ST", "NY", "7", "M&E (pre-cutover only)"),
)
DEMO_PERIOD = "am_peak"


def add_delay_used(obs: pd.DataFrame, source: str = config.TERMINAL_DELAY_SOURCE,
                   feed_stops: tuple[str, ...] = config.FEED_DELAY_STOPS) -> pd.DataFrame:
    """Add delay_field (per stop) and delay_used_s (seconds) to observations."""
    fields = obs["stop_code"].map(lambda c: delay_field_for(c, source, feed_stops).value)
    used = np.where(fields == DelayField.FEED.value, obs["delay_s"], obs["delay_vs_gtfs_s"])
    return obs.assign(delay_field=fields, delay_used_s=used.astype(float))


def build_pools(obs: pd.DataFrame, weekdays_only: bool = config.POOL_WEEKDAYS_ONLY,
                source: str = config.TERMINAL_DELAY_SOURCE,
                feed_stops: tuple[str, ...] = config.FEED_DELAY_STOPS) -> pd.DataFrame:
    """Long-format pool table for all three fallback levels."""
    o = obs
    if weekdays_only:
        weekday = pd.to_datetime(o["service_date"]).dt.weekday < 5
        log.info("pools: weekday filter keeps %d of %d runs",
                 o[weekday].groupby(RUN_ID).ngroups, o.groupby(RUN_ID).ngroups)
        o = o[weekday]
    o = add_delay_used(o, source, feed_stops)
    o = o.assign(line=o["route_id"].astype(str), toward_ny=o["toward_ny"].astype(bool),
                 period=o["terminal_arr_utc"].map(period_of))
    base = o[RUN_ID + ["line", "toward_ny", "period", "stop_code", "delay_used_s", "delay_field"]]

    levels = [
        base.assign(level=FallbackLevel.LINE_TOWARD_NY_PERIOD.value),
        base.assign(level=FallbackLevel.TOWARD_NY_PERIOD.value, line=None),
        base.assign(level=FallbackLevel.TOWARD_NY.value, line=None, period=None),
    ]
    pools = pd.concat(levels, ignore_index=True)
    pools["line"] = pools["line"].astype("string")
    pools["period"] = pools["period"].astype("string")
    pools = pools[["level", "line", "toward_ny", "period", *RUN_ID,
                   "stop_code", "delay_used_s", "delay_field"]]

    l1_runs = (base.drop_duplicates(RUN_ID)
                   .pivot_table(index=["line", "toward_ny"], columns="period",
                                values="train_number", aggfunc="count", fill_value=0))
    log.info("pools: runs per L1 cell (line × toward_ny × period):\n%s", l1_runs.to_string())
    by_field = base.groupby("delay_field")["stop_code"]
    log.info("pools: rows by delay field %s; feed-relative stops %s",
             by_field.size().to_dict(),
             sorted(base.loc[base["delay_field"] == DelayField.FEED.value, "stop_code"].unique()))
    return pools


def evidence_table(pools: pd.DataFrame, min_runs: int = config.POOL_MIN_RUNS) -> pd.DataFrame:
    """Effective count, level and alight-delay summary for each demo leg (AM peak, toward NY)."""
    rows = []
    for board, alight, line, label in DEMO_LEGS:
        pool = resolve(pools, board, alight, line, True, DEMO_PERIOD, min_runs)
        counts = level_counts(pools, board, alight, line, True, DEMO_PERIOD)
        l1 = counts[FallbackLevel.LINE_TOWARD_NY_PERIOD.value]
        alight_min = pool.delays_s[:, 1] / 60 if pool.run_count else np.array([np.nan])
        rows.append({
            "leg": f"{board}→{alight}", "line": f"{line} {label}",
            "l1_alight_runs": l1, "l1_board_observed": counts["l1_board_observed"],
            "board_zero_share": round(pool.board_zero_share, 2),
            "level": pool.level.value,
            "run_count": pool.run_count,
            "alight_field": pool.fields[alight].value, "board_field": pool.fields[board].value,
            "alight_p50_min": round(float(np.median(alight_min)), 1),
            "alight_p90_min": round(float(np.quantile(alight_min, 0.9)), 1),
            "reason": "" if pool.level is FallbackLevel.LINE_TOWARD_NY_PERIOD
            else f"L1 has {l1} runs observed at {alight} (< {min_runs})",
        })
    return pd.DataFrame(rows)


def stop_coverage_diagnostic(obs: pd.DataFrame, stop_codes: pd.DataFrame, raw_records: pd.DataFrame,
                             cleaned: pd.DataFrame, stop_names: set[str], stop: str = "RT",
                             ref: tuple[str, str] = ("ST", "HB")) -> dict:
    """Why runs lack an approach record at `stop`.

    Population: runs observed at both ref stops whose GTFS trip schedules `stop`.
    Each run without an observation at `stop` is classified as
      no_next_stop_record  no raw record with NEXT_STOP = stop for that train
      dropped_by_cleaning  raw records exist, none survive the snapshot filters (staleness / low count)
      stop_code_mismatch   cleaned records exist but did not join to `stop`
    stop_codes: GTFS (trip_id, stop_code) rows. Records are matched by train number and
    poll time between the run's observations at the two ref stops (so other days' runs of
    the same train number do not count).
    """
    scheduled = set(stop_codes.loc[stop_codes["stop_code"] == stop, "trip_id"])
    at_ref = obs[obs["stop_code"].isin(ref)]
    seen = at_ref.groupby(RUN_ID).agg(codes=("stop_code", set), t0=("observed_utc", "min"),
                                      t1=("observed_utc", "max")).reset_index()
    seen = seen.merge(obs.groupby(RUN_ID)["stop_code"].agg(set).rename("all_codes").reset_index(), on=RUN_ID)
    pop = seen[seen["codes"].map(lambda s: set(ref) <= s) & seen["trip_id"].isin(scheduled)]
    missing = pop[~pop["all_codes"].map(lambda s: stop in s)]
    classes = {"no_next_stop_record": 0, "dropped_by_cleaning": 0, "stop_code_mismatch": 0}
    raw_at = raw_records[raw_records["next_stop_name"].isin(stop_names)]
    clean_at = cleaned[cleaned["next_stop_name"].isin(stop_names)]

    def any_in(recs: pd.DataFrame, r) -> bool:
        x = recs[recs["train_number"] == r.train_number]
        return bool(((x["poll_utc"] >= r.t0) & (x["poll_utc"] <= r.t1)).any())

    for r in missing.itertuples():
        if not any_in(raw_at, r):
            classes["no_next_stop_record"] += 1
        elif not any_in(clean_at, r):
            classes["dropped_by_cleaning"] += 1
        else:
            classes["stop_code_mismatch"] += 1
    return {"population": len(pop), "observed": len(pop) - len(missing), "missing": len(missing), **classes}


def cell_composition(pools: pd.DataFrame, stop_codes: pd.DataFrame, line: str = "7", alight: str = "HB",
                     stop: str = "RT", period: str = DEMO_PERIOD) -> pd.DataFrame:
    """Runs in the L1 cell reaching `alight`: does the trip schedule / do we observe `stop`?"""
    scheduled = set(stop_codes.loc[stop_codes["stop_code"] == stop, "trip_id"])
    cell = pools[(pools["level"] == FallbackLevel.LINE_TOWARD_NY_PERIOD.value) & (pools["line"] == line)
                 & pools["toward_ny"] & (pools["period"] == period)]
    runs = cell.groupby(RUN_ID)["stop_code"].agg(set).reset_index()
    runs = runs[runs["stop_code"].map(lambda s: alight in s)]
    return pd.DataFrame({"service_date": runs["service_date"].astype(str), "train": runs["train_number"],
                         f"schedules_{stop}": runs["trip_id"].isin(scheduled),
                         f"observed_{stop}": runs["stop_code"].map(lambda s: stop in s)})


def _md_table(table: pd.DataFrame) -> str:
    header = "| " + " | ".join(table.columns) + " |"
    sep = "|" + "---|" * len(table.columns)
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in table.itertuples(index=False)]
    return "\n".join([header, sep, *body])


def write_evidence(table: pd.DataFrame, path: Path = EVIDENCE_MD, diagnostic: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Delay pool evidence (demo legs, toward NY, AM peak 06:00–10:00)\n\n"
        "Generated by `python -m src.build.pools`. Weekday runs only; "
        f"min runs per level = {config.POOL_MIN_RUNS}. Pools are anchored on the alight stop: "
        "population = runs of the line × toward_ny × period observed there; boarding delay = that "
        "run's observed delay, 0 if unobserved (board_zero_share). Fallback L1 → L2 → L3. Alight delay in minutes over the "
        "effective runs at the chosen level (`delay_s` at NY, `delay_vs_gtfs_s` elsewhere; "
        "check A, provisional). Cancelled runs are invisible (optimistic bias).\n\n"
        + _md_table(table) + "\n" + diagnostic, encoding="utf-8")


def rt_diagnostic_markdown(pools: pd.DataFrame, obs: pd.DataFrame) -> str:
    """Load raw + cleaned records and GTFS, run the RT coverage diagnostic, format it."""
    from src.build import clean, join
    from src.gtfs import load_feed

    feed = load_feed(config.GTFS_NJT_RAIL_DIR)
    stop_codes = feed.stop_times.merge(feed.stops[["stop_id", "stop_code"]], on="stop_id")[["trip_id", "stop_code"]]
    _, raw = clean.load_raw_vehicle_archive(config.RAW_VEHICLE_DIR)
    names = {n for n, codes in join.station_name_codes(config.RAW_STATION_SCHEDULE_DIR).items() if "RT" in codes}
    d = stop_coverage_diagnostic(obs, stop_codes, clean.normalize_records(raw),
                                 pd.read_parquet(config.VEHICLE_RECORDS_PARQUET), names)
    comp = cell_composition(pools, stop_codes)
    log.info("RT diagnostic: %s", d)
    return (
        "\n## RT (Short Hills) coverage diagnostic\n\n"
        f"All archived runs observed at ST and HB whose trip schedules RT: **{d['population']}**; "
        f"with an RT approach record: **{d['observed']}**; without: **{d['missing']}** "
        f"(no NEXT_STOP=RT record {d['no_next_stop_record']}, dropped by staleness / low-count filters "
        f"{d['dropped_by_cleaning']}, stop-code mismatch {d['stop_code_mismatch']}).\n\n"
        "Weekday AM-peak M&E (line 7) runs reaching HB, the L1 cell for RT→HB:\n\n"
        + _md_table(comp)
        + f"\n\n**Finding:** only {int(comp['schedules_RT'].sum())} of {len(comp)} runs in the cell are "
        "scheduled to stop at Short Hills (the others run express past it), and every scheduled one was "
        "observed there. The thin RT coverage is the timetable, not lost data; alight-anchored pools "
        "give RT→HB the full M&E population, zero-filling the boarding delay where RT was not observed.\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-diagnose", action="store_true", help="skip the RT diagnostic (reads raw archive)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    obs = pd.read_parquet(config.OBSERVATIONS_PARQUET)
    pools = build_pools(obs)
    pools.to_parquet(config.POOLS_PARQUET, index=False)
    log.info("wrote %d pool rows to %s", len(pools), config.POOLS_PARQUET)
    table = evidence_table(pools)
    log.info("pool evidence:\n%s", table.to_string(index=False))
    write_evidence(table, diagnostic="" if args.no_diagnose else rt_diagnostic_markdown(pools, obs))


if __name__ == "__main__":
    main()
