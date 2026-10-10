"""Delay pool lookup (request time): pools.parquet → DelaySamples + PoolUsage.

Pure functions over the pools table built by src/build/pools.py. Pools are
anchored on the ALIGHTING stop, so every boarding station of the same train
shares one population (Phase 1.8). Fallback, first level whose population
reaches min_runs:

  L1 line × toward_ny × period   runs observed at the alight stop
  L2 toward_ny × period          as L1, all lines
  L3 toward_ny                   as L2, all periods
  L4 no data                     zeros (schedule baseline), run_count = 0

Boarding delay = the run's observed delay at the boarding stop, 0 if that stop
was not observed (counted in board_zero_share). Draws pick run ids uniformly
with replacement from the sorted population, so board and alight come from the
same run, and the same rng gives the same runs whatever the boarding station.
Delays are raw (negatives kept); clamping the boarding delay is the scorer's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.contracts import DelayField, DelaySamples, FallbackLevel, PoolKey, PoolUsage

RUN_ID = ["service_date", "train_number", "trip_id"]


def period_of(t_utc: datetime | pd.Timestamp) -> str:
    """Pool period for a UTC instant, by its New York local time of day."""
    local = pd.Timestamp(t_utc).tz_convert(config.LOCAL_TZ)
    tod_s = local.hour * 3600 + local.minute * 60 + local.second
    for name, start_s, end_s in config.PERIODS:
        if start_s <= tod_s < end_s:
            return name
    raise ValueError(f"No period covers {local}")  # unreachable while PERIODS covers the day


def delay_field_for(stop_code: str,
                    source: str = config.TERMINAL_DELAY_SOURCE,
                    feed_stops: tuple[str, ...] = config.FEED_DELAY_STOPS) -> DelayField:
    """Which observed delay measure a pool uses at this stop."""
    if source not in ("gtfs", "feed"):
        raise ValueError(f"TERMINAL_DELAY_SOURCE must be 'gtfs' or 'feed', got {source!r}")
    return DelayField.FEED if source == "feed" and stop_code in feed_stops else DelayField.GTFS


def load_pools(path: Path = config.POOLS_PARQUET) -> pd.DataFrame:
    return pd.read_parquet(path)


@dataclass(frozen=True)
class ResolvedPool:
    """Effective runs for one (board, alight) leg at the chosen level.

    delays_s has one row per distinct run, columns (board, alight), no NaN;
    run_ids[i] ("service_date|train|trip_id") names row i, sorted. board_zero_share
    is the share of runs whose boarding delay was not observed and set to 0.
    """

    level: FallbackLevel
    key: PoolKey
    delays_s: np.ndarray
    fields: dict[str, DelayField]
    run_ids: tuple[str, ...] = ()
    board_zero_share: float = 0.0

    def __post_init__(self) -> None:
        self.delays_s.setflags(write=False)  # shared via PoolIndex.memo

    @property
    def run_count(self) -> int:
        return int(self.delays_s.shape[0])


@dataclass(frozen=True, eq=False)
class PoolIndex:
    """pools.parquet split by (level, line, toward_ny, period) once; resolve/sample accept it."""

    cells: dict
    empty: pd.DataFrame
    memo: dict = field(default_factory=dict)  # resolve() results; deterministic, so safe to reuse


def index_pools(pools: pd.DataFrame) -> PoolIndex:
    keys = ["level", "line", "toward_ny", "period"]
    filled = pools.assign(line=pools["line"].astype(object).where(pools["line"].notna(), None),
                          period=pools["period"].astype(object).where(pools["period"].notna(), None))
    cells = {tuple(None if pd.isna(v) else v for v in k): rows
             for k, rows in filled.groupby(keys, dropna=False)}
    return PoolIndex(cells, pools.iloc[0:0])


def _cell(pools: pd.DataFrame | PoolIndex, level: FallbackLevel, key: PoolKey) -> pd.DataFrame:
    if isinstance(pools, PoolIndex):
        return pools.cells.get((level.value, key.line, key.toward_ny, key.period), pools.empty)
    rows = pools[pools["level"] == level.value]
    for col, value in (("line", key.line), ("toward_ny", key.toward_ny), ("period", key.period)):
        rows = rows[rows[col].isna()] if value is None else rows[rows[col] == value]
    return rows


def _runs_by_stop(rows: pd.DataFrame, stops: list[str]) -> pd.DataFrame:
    """Runs × stops of delay_used_s, sorted by run id (deterministic row order)."""
    rows = rows[rows["stop_code"].isin(stops)]
    wide = rows.pivot_table(index=RUN_ID, columns="stop_code", values="delay_used_s", aggfunc="first")
    return wide.reindex(columns=stops).sort_index()


def _field(rows: pd.DataFrame, stop: str) -> DelayField:
    values = rows.loc[rows["stop_code"] == stop, "delay_field"].unique()
    if len(values) != 1:
        raise ValueError(f"Expected one delay_field at {stop}, found {list(values)}")
    return DelayField(values[0])


def resolve(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str,
            toward_ny: bool, period: str, min_runs: int = config.POOL_MIN_RUNS) -> ResolvedPool:
    """Pick the finest pool level with >= min_runs effective runs for this leg."""
    if isinstance(pools, PoolIndex):
        key = (board_stop, alight_stop, line, toward_ny, period, min_runs)
        if key not in pools.memo:
            pools.memo[key] = _resolve(pools, *key)
        return pools.memo[key]
    return _resolve(pools, board_stop, alight_stop, line, toward_ny, period, min_runs)


def _ids(index: pd.Index) -> tuple[str, ...]:
    return tuple("|".join(str(v) for v in t) for t in index)


def _anchored(rows: pd.DataFrame, board_stop: str, alight_stop: str,
              level: FallbackLevel, key: PoolKey) -> ResolvedPool:
    """Population = runs observed at alight_stop; board delay observed or 0."""
    wide = _runs_by_stop(rows, [board_stop, alight_stop])
    wide = wide[wide[alight_stop].notna()]
    unobserved = wide[board_stop].isna()
    share = float(unobserved.mean()) if len(wide) else 1.0
    if share == 1.0:
        board_field = DelayField.ASSUMED_ZERO
    else:
        board_field = _field(rows, board_stop)
    delays = np.column_stack([wide[board_stop].fillna(0.0).to_numpy(float), wide[alight_stop].to_numpy(float)])
    alight_field = _field(rows, alight_stop) if len(wide) else DelayField.ASSUMED_ZERO
    return ResolvedPool(level, key, delays.reshape(-1, 2), {board_stop: board_field, alight_stop: alight_field},
                        _ids(wide.index), share if len(wide) else 0.0)


def _resolve(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str, toward_ny: bool,
             period: str, min_runs: int) -> ResolvedPool:
    if board_stop == alight_stop:
        raise ValueError(f"Board and alight stops are both {board_stop}")
    for level, key in ((FallbackLevel.LINE_TOWARD_NY_PERIOD, PoolKey(line, toward_ny, period)),
                       (FallbackLevel.TOWARD_NY_PERIOD, PoolKey(None, toward_ny, period)),
                       (FallbackLevel.TOWARD_NY, PoolKey(None, toward_ny, None))):
        rows = _cell(pools, level, key)
        if rows.empty:
            continue
        pool = _anchored(rows, board_stop, alight_stop, level, key)
        if pool.run_count >= min_runs:
            return pool
    return ResolvedPool(FallbackLevel.NO_DATA, PoolKey(None, None, None), np.zeros((0, 2)),
                        {board_stop: DelayField.ASSUMED_ZERO, alight_stop: DelayField.ASSUMED_ZERO})


def level_counts(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str,
                 toward_ny: bool, period: str) -> dict[str, float]:
    """Population at each level plus L1 boarding coverage (evidence / diagnostics)."""
    out: dict[str, float] = {}
    for level, key in ((FallbackLevel.LINE_TOWARD_NY_PERIOD, PoolKey(line, toward_ny, period)),
                       (FallbackLevel.TOWARD_NY_PERIOD, PoolKey(None, toward_ny, period)),
                       (FallbackLevel.TOWARD_NY, PoolKey(None, toward_ny, None))):
        rows = _cell(pools, level, key)
        pool = _anchored(rows, board_stop, alight_stop, level, key) if len(rows) else None
        out[level.value] = pool.run_count if pool else 0
        if level is FallbackLevel.LINE_TOWARD_NY_PERIOD:
            out["l1_board_observed"] = round(pool.run_count * (1 - pool.board_zero_share)) if pool else 0
            out["l1_board_zero_share"] = round(pool.board_zero_share, 2) if pool else 0.0
    return out


def sample(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str,
           toward_ny: bool, period: str, n: int, rng: np.random.Generator,
           min_runs: int = config.POOL_MIN_RUNS) -> tuple[DelaySamples, PoolUsage]:
    """n whole-run draws of (board, alight) delay in seconds, with the pool used.

    Each draw picks a run id uniformly with replacement from the sorted
    population and reads that run's board and alight delays together. The
    population depends only on the alight stop, line and period, so a given
    rng draws the same runs from any boarding station.
    """
    pool = resolve(pools, board_stop, alight_stop, line, toward_ny, period, min_runs)
    if pool.run_count:
        draws = pool.delays_s[rng.integers(0, pool.run_count, size=n)]  # rows are sorted run ids
    else:
        draws = np.zeros((n, 2))
    stops = (board_stop, alight_stop)
    samples = DelaySamples(draws, stops, pool.key, pool.level, pool.run_count)
    usage = PoolUsage(pool.key, pool.level, pool.run_count, pool.fields,
                      board_zero_share=pool.board_zero_share)
    return samples, usage
