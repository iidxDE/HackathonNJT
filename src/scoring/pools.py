"""Delay pool lookup (request time): pools.parquet → DelaySamples + PoolUsage.

Pure functions over the pools table built by src/build/pools.py. Sampling
draws whole runs uniformly with replacement, so a run's boarding and
alighting delays always come from the same historical run. Fallback, first
level whose effective run count reaches min_runs:

  L1  line × toward_ny × period  runs observed at BOTH stops
  L1b line × toward_ny × period  runs observed at the alight stop; board delay = 0
  L2  toward_ny × period         as L1b, all lines
  L3 toward_ny                   as L2, all periods
  L4 no data                     zeros (schedule baseline), run_count = 0

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
    run_ids[i] ("service_date|train|trip_id") names row i.
    """

    level: FallbackLevel
    key: PoolKey
    delays_s: np.ndarray
    fields: dict[str, DelayField]
    run_ids: tuple[str, ...] = ()

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


def _resolve(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str, toward_ny: bool, period: str,
             min_runs: int) -> ResolvedPool:
    if board_stop == alight_stop:
        raise ValueError(f"Board and alight stops are both {board_stop}")
    l1 = PoolKey(line, toward_ny, period)
    rows = _cell(pools, FallbackLevel.LINE_TOWARD_NY_PERIOD, l1)
    both = _runs_by_stop(rows, [board_stop, alight_stop]).dropna()
    if len(both) >= min_runs:
        return ResolvedPool(FallbackLevel.LINE_TOWARD_NY_PERIOD, l1, both.to_numpy(float),
                            {board_stop: _field(rows, board_stop), alight_stop: _field(rows, alight_stop)},
                            _ids(both.index))

    # L1b keeps the line and drops the boarding-stop requirement: line identity matters
    # more than boarding coverage (e.g. RT: most AM-peak M&E runs skip Short Hills).
    for level, key, cell_level in (
            (FallbackLevel.LINE_TOWARD_NY_PERIOD_ALIGHT, l1, FallbackLevel.LINE_TOWARD_NY_PERIOD),
            (FallbackLevel.TOWARD_NY_PERIOD, PoolKey(None, toward_ny, period), FallbackLevel.TOWARD_NY_PERIOD),
            (FallbackLevel.TOWARD_NY, PoolKey(None, toward_ny, None), FallbackLevel.TOWARD_NY)):
        rows = _cell(pools, cell_level, key)
        alight = _runs_by_stop(rows, [alight_stop])[alight_stop].dropna()
        if len(alight) >= min_runs:
            delays = np.column_stack([np.zeros(len(alight)), alight.to_numpy(float)])
            return ResolvedPool(level, key, delays,
                                {board_stop: DelayField.ASSUMED_ZERO, alight_stop: _field(rows, alight_stop)},
                                _ids(alight.index))

    return ResolvedPool(FallbackLevel.NO_DATA, PoolKey(None, None, None), np.zeros((0, 2)),
                        {board_stop: DelayField.ASSUMED_ZERO, alight_stop: DelayField.ASSUMED_ZERO})


def level_counts(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str,
                 toward_ny: bool, period: str) -> dict[FallbackLevel, int]:
    """Effective run count at every fallback level for one leg (evidence / diagnostics)."""
    l1_rows = _cell(pools, FallbackLevel.LINE_TOWARD_NY_PERIOD, PoolKey(line, toward_ny, period))
    l2_rows = _cell(pools, FallbackLevel.TOWARD_NY_PERIOD, PoolKey(None, toward_ny, period))
    l3_rows = _cell(pools, FallbackLevel.TOWARD_NY, PoolKey(None, toward_ny, None))

    def alight_only(rows: pd.DataFrame) -> int:
        return int(_runs_by_stop(rows, [alight_stop])[alight_stop].notna().sum())

    return {
        FallbackLevel.LINE_TOWARD_NY_PERIOD: len(_runs_by_stop(l1_rows, [board_stop, alight_stop]).dropna()),
        FallbackLevel.LINE_TOWARD_NY_PERIOD_ALIGHT: alight_only(l1_rows),
        FallbackLevel.TOWARD_NY_PERIOD: alight_only(l2_rows),
        FallbackLevel.TOWARD_NY: alight_only(l3_rows),
    }


def sample(pools: pd.DataFrame | PoolIndex, board_stop: str, alight_stop: str, line: str,
           toward_ny: bool, period: str, n: int, rng: np.random.Generator,
           min_runs: int = config.POOL_MIN_RUNS) -> tuple[DelaySamples, PoolUsage]:
    """n whole-run draws of (board, alight) delay in seconds, with the pool used.

    Each draw picks a run id uniformly with replacement and reads that run's
    board and alight delays together.
    """
    pool = resolve(pools, board_stop, alight_stop, line, toward_ny, period, min_runs)
    if pool.run_count:
        drawn = rng.choice(np.asarray(pool.run_ids), size=n)
        row_of = {rid: i for i, rid in enumerate(pool.run_ids)}
        draws = pool.delays_s[[row_of[rid] for rid in drawn]]
    else:
        draws = np.zeros((n, 2))
    stops = (board_stop, alight_stop)
    samples = DelaySamples(draws, stops, pool.key, pool.level, pool.run_count)
    usage = PoolUsage(pool.key, pool.level, pool.run_count, pool.fields)
    return samples, usage
