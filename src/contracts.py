"""Data contracts between modules (CLAUDE.md §5).

Router and safety net both emit Candidate; the scorer emits ScoredOption.
All datetimes are timezone-aware UTC. Money is USD, delays are seconds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

import numpy as np


class LegMode(str, Enum):
    DRIVE = "drive"
    RAIL = "rail"
    PATH = "path"
    WALK = "walk"


class InfraAsset(str, Enum):
    PORTAL_BRIDGE = "portal_bridge"
    NORTH_RIVER_TUNNELS = "north_river_tunnels"
    PENN_STATION = "penn_station"
    PATH_TUNNELS = "path_tunnels"


class FallbackLevel(str, Enum):
    """Delay pool granularity, finest first. Direction means toward_ny, not GTFS direction_id."""

    LINE_TOWARD_NY_PERIOD = "line_toward_ny_period"  # L1: runs observed at board AND alight
    TOWARD_NY_PERIOD = "toward_ny_period"  # L2: runs observed at alight; board delay = 0
    TOWARD_NY = "toward_ny"  # L3: as L2, all periods
    NO_DATA = "no_data"  # L4: zero delays (schedule baseline); UI must warn


class DelayField(str, Enum):
    """Where a pooled delay value came from (traceability in PoolUsage)."""

    GTFS = "delay_vs_gtfs_s"  # estimated time − published GTFS time
    FEED = "delay_s"  # SEC_LATE, relative to the feed's own schedule
    ASSUMED_ZERO = "assumed_zero"  # no observation used; delay set to 0


class AssumptionFlag(str, Enum):
    PATH_UNMEASURED = "path_unmeasured"
    DRIVE_VARIANCE_ASSUMED = "drive_variance_assumed"
    DRIVE_VARIANCE_DEGENERATE = "drive_variance_degenerate"  # all three traffic models equal
    DRIVE_SLOT_EXTRAPOLATED = "drive_slot_extrapolated"  # departure outside cached slots


CandidateSource = Literal["router", "safety_net"]


def normalize_train_number(raw: str) -> str:
    """Strip leading zeros: GTFS '0400' and API '400' both become '400'."""
    s = str(raw).strip()
    return s.lstrip("0") or ("0" if s else s)


def _require_utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC, got {value!r}")


@dataclass(frozen=True)
class Leg:
    """One movement in a journey. Rail fields are None for non-rail legs."""

    mode: LegMode
    from_stop: str
    to_stop: str
    sched_dep: datetime  # UTC
    sched_arr: datetime  # UTC
    train_number: str | None = None  # rail only, leading zeros stripped
    gtfs_line: str | None = None  # GTFS route_id
    direction: int | None = None  # GTFS direction_id
    toward_ny: bool | None = None  # trip terminal in config.TOWARD_NY_TERMINALS (pool key)

    def __post_init__(self) -> None:
        _require_utc("sched_dep", self.sched_dep)
        _require_utc("sched_arr", self.sched_arr)
        if self.sched_arr < self.sched_dep:
            raise ValueError(f"Leg arrives before it departs: {self}")
        if self.mode is LegMode.RAIL:
            if (not self.train_number or self.gtfs_line is None or self.direction is None
                    or self.toward_ny is None):
                raise ValueError("Rail legs need train_number, gtfs_line, direction and toward_ny")
            object.__setattr__(self, "train_number", normalize_train_number(self.train_number))
        elif self.train_number is not None:
            raise ValueError(f"train_number is rail-only, got it on a {self.mode.value} leg")

    @property
    def duration_s(self) -> float:
        return (self.sched_arr - self.sched_dep).total_seconds()


@dataclass(frozen=True)
class CostBreakdown:
    """Itemized base cost in USD. Garage fees count as parking."""

    fuel_usd: float = 0.0
    tolls_usd: float = 0.0
    parking_usd: float = 0.0
    fare_usd: float = 0.0  # NJ Transit origin–destination fare
    path_fare_usd: float = 0.0
    congestion_usd: float = 0.0  # Manhattan congestion charge (drive-all-the-way)

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")

    @property
    def total_usd(self) -> float:
        return (
            self.fuel_usd
            + self.tolls_usd
            + self.parking_usd
            + self.fare_usd
            + self.path_fare_usd
            + self.congestion_usd
        )


@dataclass(frozen=True)
class Candidate:
    """A priced, tagged journey: router or safety net → scorer."""

    access_station: str  # station code, or "DRIVE" for drive-all-the-way
    legs: tuple[Leg, ...]
    costs: CostBreakdown
    exposure: frozenset[InfraAsset]
    source: CandidateSource

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("Candidate needs at least one leg")
        if self.source not in ("router", "safety_net"):
            raise ValueError(f"Unknown source {self.source!r}")
        for prev, nxt in zip(self.legs, self.legs[1:]):
            if nxt.from_stop != prev.to_stop:
                raise ValueError(f"Legs not contiguous: {prev.to_stop} → {nxt.from_stop}")
            if nxt.sched_dep < prev.sched_arr:
                raise ValueError(f"Leg departs before previous leg arrives at {nxt.from_stop}")
        object.__setattr__(self, "legs", tuple(self.legs))
        object.__setattr__(self, "exposure", frozenset(self.exposure))

    @property
    def base_cost_usd(self) -> float:
        return self.costs.total_usd

    @property
    def sched_dep(self) -> datetime:
        return self.legs[0].sched_dep

    @property
    def sched_arr(self) -> datetime:
        return self.legs[-1].sched_arr

    @property
    def n_transfers(self) -> int:
        """Boardings of rail/PATH vehicles after the first one."""
        rides = sum(leg.mode in (LegMode.RAIL, LegMode.PATH) for leg in self.legs)
        return max(rides - 1, 0)


@dataclass(frozen=True)
class PoolKey:
    """Delay pool cell; None means 'all' at coarser fallback levels.

    toward_ny=False is NOT "outbound": it is every trip whose terminal is not
    NY or HB, which includes NY-direction trips ending at Newark Penn, Long
    Branch, etc. as well as trips heading away from New York.
    """

    line: str | None  # GTFS route_id
    toward_ny: bool | None  # trip terminates at NY or HB (config.TOWARD_NY_TERMINALS)
    period: str | None  # config.PERIODS name, from the run's terminal arrival


@dataclass(frozen=True, eq=False)
class DelaySamples:
    """Delay draws for one rail leg: delay model → scorer.

    delays_s[i, j] is draw i's delay in seconds at stop_ids[j]. Each row is
    one whole historical run drawn with replacement, so the stops in a row
    stay correlated. run_count is the number of distinct runs the draws come
    from (the sample size), never the number of draws or stop observations.
    """

    delays_s: np.ndarray  # shape (n_draws, len(stop_ids)); no NaN
    stop_ids: tuple[str, ...]
    pool_key: PoolKey
    fallback_level: FallbackLevel
    run_count: int

    def __post_init__(self) -> None:
        arr = np.array(self.delays_s, dtype=float)  # copy, so freezing can't leak
        if arr.ndim != 2 or arr.shape[1] != len(self.stop_ids):
            raise ValueError(f"delays_s shape {arr.shape} does not match {len(self.stop_ids)} stops")
        if np.isnan(arr).any():
            raise ValueError("delays_s contains NaN; unobserved stops must not be sampled")
        if self.run_count < 0:
            raise ValueError(f"run_count must be >= 0, got {self.run_count}")
        arr.setflags(write=False)
        object.__setattr__(self, "delays_s", arr)
        object.__setattr__(self, "stop_ids", tuple(self.stop_ids))

    @property
    def n_draws(self) -> int:
        return int(self.delays_s.shape[0])


@dataclass(frozen=True)
class PoolUsage:
    """Which pool scored a rail leg, for the UI confidence indicator.

    run_count is the effective sample at the chosen level: at L1, runs
    observed at both the boarding and alighting stops; at L2/L3, runs
    observed at the alighting stop; 0 at NO_DATA. delay_field_at_stop says
    which delay measure each stop's values came from.
    """

    pool_key: PoolKey
    fallback_level: FallbackLevel
    run_count: int
    delay_field_at_stop: Mapping[str, DelayField]
    train_number: str | None = None  # set by the scorer, which knows the leg

    def __post_init__(self) -> None:
        object.__setattr__(self, "delay_field_at_stop", dict(self.delay_field_at_stop))


@dataclass(frozen=True)
class ScoredOption:
    """Scorer → UI."""

    candidate: Candidate
    p_on_time: float  # 0–1
    arrival_p50: datetime  # UTC
    arrival_p90: datetime  # UTC
    expected_late_min: float
    risk_adjusted_cost_usd: float
    pools: tuple[PoolUsage, ...] = ()
    active_alerts: tuple[str, ...] = ()
    assumption_flags: frozenset[AssumptionFlag] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not 0.0 <= self.p_on_time <= 1.0:
            raise ValueError(f"p_on_time must be in [0, 1], got {self.p_on_time}")
        _require_utc("arrival_p50", self.arrival_p50)
        _require_utc("arrival_p90", self.arrival_p90)
        if self.arrival_p90 < self.arrival_p50:
            raise ValueError("arrival_p90 is earlier than arrival_p50")
        object.__setattr__(self, "assumption_flags", frozenset(self.assumption_flags))
