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
from src.scoring.pools import RUN_ID, delay_field_for, period_of, resolve

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
        l1 = resolve(pools, board, alight, line, True, DEMO_PERIOD, min_runs=0)
        alight_min = pool.delays_s[:, 1] / 60 if pool.run_count else np.array([np.nan])
        rows.append({
            "leg": f"{board}→{alight}", "line": f"{line} {label}",
            "l1_effective_runs": l1.run_count, "level": pool.level.value,
            "run_count": pool.run_count,
            "alight_field": pool.fields[alight].value, "board_field": pool.fields[board].value,
            "alight_p50_min": round(float(np.median(alight_min)), 1),
            "alight_p90_min": round(float(np.quantile(alight_min, 0.9)), 1),
            "reason": "" if pool.level is FallbackLevel.LINE_TOWARD_NY_PERIOD
            else f"L1 has {l1.run_count} runs observed at both {board} and {alight} (< {min_runs})",
        })
    return pd.DataFrame(rows)


def write_evidence(table: pd.DataFrame, path: Path = EVIDENCE_MD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "| " + " | ".join(table.columns) + " |"
    sep = "|" + "---|" * len(table.columns)
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in table.itertuples(index=False)]
    path.write_text(
        "# Delay pool evidence (demo legs, toward NY, AM peak 06:00–10:00)\n\n"
        "Generated by `python -m src.build.pools`. Weekday runs only; "
        f"min runs per level = {config.POOL_MIN_RUNS}. Alight delay in minutes over the "
        "effective runs at the chosen level (`delay_s` at NY, `delay_vs_gtfs_s` elsewhere; "
        "check A, provisional). Cancelled runs are invisible (optimistic bias).\n\n"
        + "\n".join([header, sep, *body]) + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    pools = build_pools(pd.read_parquet(config.OBSERVATIONS_PARQUET))
    pools.to_parquet(config.POOLS_PARQUET, index=False)
    log.info("wrote %d pool rows to %s", len(pools), config.POOLS_PARQUET)
    table = evidence_table(pools)
    log.info("pool evidence:\n%s", table.to_string(index=False))
    write_evidence(table)


if __name__ == "__main__":
    main()
