"""Drive leg lookup and sampling (request time): drive_cache.parquet only, no API calls.

For a departure time, each traffic model's duration is interpolated linearly
between the two adjacent cached slots. Outside the slot range the nearest
slot is used and DRIVE_SLOT_EXTRAPOLATED is flagged. Samples are
triangular(min = OPTIMISTIC, mode = BEST_GUESS, max = PESSIMISTIC) with the
three values sorted first; if all three are equal the drive is deterministic
and DRIVE_VARIANCE_DEGENERATE is flagged. No spread is invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.contracts import AssumptionFlag

OPTIMISTIC, BEST_GUESS, PESSIMISTIC = config.ROUTES_TRAFFIC_MODELS


def load_drive_cache(path: Path = config.DRIVE_CACHE_PARQUET) -> pd.DataFrame:
    return pd.read_parquet(path)


@dataclass(frozen=True)
class DriveEstimate:
    """Drive from the origin to dest leaving at depart_utc. Durations in seconds."""

    dest: str
    depart_utc: datetime
    low_s: float  # sorted min of the three traffic models
    mode_s: float  # sorted middle (BEST_GUESS unless the models cross)
    high_s: float  # sorted max
    best_guess_s: float  # BEST_GUESS as returned (for planning; may lie outside low–high order)
    distance_m: float
    toll_usd: float  # NaN if Google expects tolls but gave no price
    fetched_at: str = ""  # ISO date the nearest BEST_GUESS slot was fetched (source date)
    flags: frozenset[AssumptionFlag] = field(default_factory=frozenset)


def _interp(slots_s: np.ndarray, values: np.ndarray, t_s: float) -> float:
    return float(np.interp(t_s, slots_s, values))  # clamps to the end slots outside range


@dataclass(frozen=True, eq=False)
class DestDrive:
    """One destination's slots as arrays (UTC epoch seconds, ascending)."""

    slots_s: np.ndarray
    optimistic_s: np.ndarray
    best_guess_s: np.ndarray
    pessimistic_s: np.ndarray
    distance_m: np.ndarray  # BEST_GUESS rows
    toll_usd: np.ndarray
    fetched_at: tuple[str, ...]


def _index_dest(rows: pd.DataFrame) -> DestDrive:
    wide = rows.pivot_table(index="slot_utc", columns="model", values="duration_s").sort_index()
    best = rows[rows["model"] == BEST_GUESS].set_index("slot_utc").reindex(wide.index)
    fetched = best["fetched_at"] if "fetched_at" in best else pd.Series(None, index=best.index)
    return DestDrive(np.array([t.timestamp() for t in wide.index]), wide[OPTIMISTIC].to_numpy(float),
                     wide[BEST_GUESS].to_numpy(float), wide[PESSIMISTIC].to_numpy(float),
                     best["distance_m"].to_numpy(float), best["toll_usd"].to_numpy(float),
                     tuple(_iso_date(v) for v in fetched))


def index_drive_cache(cache: pd.DataFrame) -> dict[str, DestDrive]:
    """Pre-pivot the cache once per process (lookup accepts this or the raw table)."""
    return {dest: _index_dest(rows) for dest, rows in cache.groupby("dest")}


def lookup(cache: pd.DataFrame | dict[str, DestDrive], dest: str,
           depart_utc: datetime | pd.Timestamp) -> DriveEstimate:
    if isinstance(cache, dict):
        if dest not in cache:
            raise KeyError(f"No cached drive to {dest!r}")
        d = cache[dest]
    else:
        rows = cache[cache["dest"] == dest]
        if rows.empty:
            raise KeyError(f"No cached drive to {dest!r}")
        d = _index_dest(rows)
    t = pd.Timestamp(depart_utc)
    if t.tzinfo is None:
        raise ValueError("depart_utc must be timezone-aware")
    t_s = t.timestamp()
    models = [_interp(d.slots_s, v, t_s) for v in (d.optimistic_s, d.best_guess_s, d.pessimistic_s)]
    low, mode, high = sorted(models)
    best_guess = models[1]

    flags = {AssumptionFlag.DRIVE_VARIANCE_ASSUMED}
    if t_s < d.slots_s[0] or t_s > d.slots_s[-1]:
        flags.add(AssumptionFlag.DRIVE_SLOT_EXTRAPOLATED)
    if low == high:
        flags.add(AssumptionFlag.DRIVE_VARIANCE_DEGENERATE)

    i = int(np.argmin(np.abs(d.slots_s - t_s)))  # nearest slot for distance / toll
    return DriveEstimate(dest, t.to_pydatetime(), low, mode, high, best_guess, float(d.distance_m[i]),
                         float(d.toll_usd[i]), d.fetched_at[i], frozenset(flags))


def _iso_date(value) -> str:
    return "" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def sample_drive_s(est: DriveEstimate, n: int, rng: np.random.Generator) -> np.ndarray:
    """n drive durations in seconds, triangular over the traffic models."""
    if est.low_s == est.high_s:
        return np.full(n, est.low_s)
    return rng.triangular(est.low_s, est.mode_s, est.high_s, size=n)
