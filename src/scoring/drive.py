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


def lookup(cache: pd.DataFrame, dest: str, depart_utc: datetime | pd.Timestamp) -> DriveEstimate:
    rows = cache[cache["dest"] == dest]
    if rows.empty:
        raise KeyError(f"No cached drive to {dest!r}")
    t = pd.Timestamp(depart_utc)
    if t.tzinfo is None:
        raise ValueError("depart_utc must be timezone-aware")
    t_s = t.timestamp()
    wide = rows.pivot_table(index="slot_utc", columns="model", values="duration_s").sort_index()
    slots_s = np.array([s.timestamp() for s in wide.index])
    models = [_interp(slots_s, wide[m].to_numpy(float), t_s) for m in (OPTIMISTIC, BEST_GUESS, PESSIMISTIC)]
    low, mode, high = sorted(models)
    best_guess = models[1]

    flags = {AssumptionFlag.DRIVE_VARIANCE_ASSUMED}
    if t_s < slots_s[0] or t_s > slots_s[-1]:
        flags.add(AssumptionFlag.DRIVE_SLOT_EXTRAPOLATED)
    if low == high:
        flags.add(AssumptionFlag.DRIVE_VARIANCE_DEGENERATE)

    best = rows[rows["model"] == BEST_GUESS]
    nearest = best.iloc[int(np.argmin(np.abs([s.timestamp() - t_s for s in best["slot_utc"]])))]
    return DriveEstimate(dest, t.to_pydatetime(), low, mode, high, best_guess, float(nearest["distance_m"]),
                         float(nearest["toll_usd"]), _iso_date(nearest.get("fetched_at")), frozenset(flags))


def _iso_date(value) -> str:
    return "" if value is None or pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d")


def sample_drive_s(est: DriveEstimate, n: int, rng: np.random.Generator) -> np.ndarray:
    """n drive durations in seconds, triangular over the traffic models."""
    if est.low_s == est.high_s:
        return np.full(n, est.low_s)
    return rng.triangular(est.low_s, est.mode_s, est.high_s, size=n)
