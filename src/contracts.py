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
    """Fragile assets a leg traverses (infra.csv columns noted)."""

    HACKENSACK_PORTAL = "hackensack_portal"  # Portal / Portal North bridges (infra.csv portal_bridge)
    NORTH_RIVER_TUNNELS = "north_river_tunnels"
    NY_PENN = "ny_penn"  # infra.csv penn_station
    PATH_TUNNELS = "path_tunnels"


class FallbackLevel(str, Enum):
    """Delay pool granularity, finest first. Pools are anchored on the alighting stop: the
    population is every run of the cell observed at the alight stop (boarding delay =
    that run's observed delay, else 0). Direction means toward_ny, not GTFS direction_id."""

    LINE_TOWARD_NY_PERIOD = "line_toward_ny_period"  # L1: runs observed at the alight stop
    TOWARD_NY_PERIOD = "toward_ny_period"  # L2: as L1, all lines
    TOWARD_NY = "toward_ny"  # L3: as L2, all periods
    NO_DATA = "no_data"  # L4: zero delays (schedule baseline); UI must warn


class DelayField(str, Enum):
    """Where a pooled delay value came from (traceability in PoolUsage)."""

    GTFS = "delay_vs_gtfs_s"  # estimated time − published GTFS time
    FEED = "delay_s"  # SEC_LATE, relative to the feed's own schedule
    ASSUMED_ZERO = "assumed_zero"  # no observation used; delay set to 0


class AssumptionFlag(str, Enum):
    PATH_UNMEASURED = "path_unmeasured"
    PATH_DELAY_NOT_MODELED = "path_delay_not_modeled"  # PATH runs to schedule (Phase 1)
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
    terminal: str | None = None  # rail only: the trip's last stop (same stop ids as from/to)

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
        elif self.train_number is not None or self.terminal is not None:
            raise ValueError(f"train_number / terminal are rail-only, got one on a {self.mode.value} leg")

    @property
    def duration_s(self) -> float:
        return (self.sched_arr - self.sched_dep).total_seconds()


@dataclass(frozen=True)
class CostItem:
    """One priced line in USD with its source. usd=None means unknown and needs a note."""

    usd: float | None
    source_url: str | None = None
    checked_date: str | None = None  # ISO date the source was checked
    note: str = ""

    def __post_init__(self) -> None:
        if self.usd is None:
            if not self.note:
                raise ValueError("An unknown (None) cost needs a note")
        else:
            if self.usd < 0:
                raise ValueError(f"Cost must be non-negative, got {self.usd}")
            if not self.source_url:
                raise ValueError(f"A priced cost needs a source_url ({self.usd} USD)")


@dataclass(frozen=True)
class CostBreakdown:
    """Itemized base cost for one commuting day (round trip), USD.

    total_usd sums the known items; partial is True if any item is unknown.
    priced=False marks a reference row (drive all the way): items may be
    shown, but total_usd is None.
    """

    fare: CostItem  # NJ Transit origin–destination fare
    parking: CostItem
    tolls: CostItem
    fuel: CostItem
    path_fare: CostItem | None = None  # only when the journey uses PATH
    priced: bool = True
    flags: frozenset[str] = frozenset()  # e.g. no_daily_parking, fills_early

    def __post_init__(self) -> None:
        object.__setattr__(self, "flags", frozenset(self.flags))

    @property
    def items(self) -> dict[str, CostItem]:
        items = {"fare": self.fare, "path_fare": self.path_fare, "parking": self.parking,
                 "tolls": self.tolls, "fuel": self.fuel}
        return {k: v for k, v in items.items() if v is not None}

    @property
    def partial(self) -> bool:
        return any(item.usd is None for item in self.items.values())

    @property
    def total_usd(self) -> float | None:
        if not self.priced:
            return None
        return sum(item.usd for item in self.items.values() if item.usd is not None)

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(f"{k}: {v.note}" for k, v in self.items.items() if v.note)


def _check_contiguous(legs: tuple[Leg, ...]) -> None:
    for prev, nxt in zip(legs, legs[1:]):
        if nxt.from_stop != prev.to_stop:
            raise ValueError(f"Legs not contiguous: {prev.to_stop} → {nxt.from_stop}")
        if nxt.sched_dep < prev.sched_arr:
            raise ValueError(f"Leg departs before previous leg arrives at {nxt.from_stop}")


@dataclass(frozen=True)
class Candidate:
    """A station policy with a planned train: router or safety net → scorer.

    legs is the planned itinerary (access legs + the planned onward option).
    catch_next lists onward options from the boarding station, planned first,
    then the next eligible trains (possibly after the deadline); the scorer
    lets the rider take the catchable option with the earliest scheduled
    arrival. costs / exposure are None until the pricer / tagger fill them.
    An infeasible candidate (no train meets the deadline) is still emitted,
    with legs empty only if there is no eligible train at all.
    """

    access_station: str  # station code, or "DRIVE" for drive-all-the-way
    legs: tuple[Leg, ...]
    costs: CostBreakdown | None
    exposure: frozenset[InfraAsset] | None
    source: CandidateSource
    catch_next: tuple[tuple[Leg, ...], ...] = ()
    leave_home: datetime | None = None  # UTC
    infeasible: bool = False
    infeasible_reason: str | None = None
    label: str = ""
    variant: int = 0  # train choice: 0 = latest on-time train (timetable plan), k = k-th earlier
    flags: frozenset[str] = frozenset()  # e.g. exposure_varies
    notes: tuple[str, ...] = ()  # free-text caveats for the UI

    def __post_init__(self) -> None:
        object.__setattr__(self, "flags", frozenset(self.flags))
        object.__setattr__(self, "notes", tuple(self.notes))
        self._validate()

    def _validate(self) -> None:
        legs = tuple(self.legs)
        options = tuple(tuple(o) for o in self.catch_next)
        if not legs and not self.infeasible:
            raise ValueError("Candidate needs at least one leg unless infeasible")
        if self.source not in ("router", "safety_net"):
            raise ValueError(f"Unknown source {self.source!r}")
        _check_contiguous(legs)
        for opt in options:
            if not opt:
                raise ValueError("Empty catch_next option")
            _check_contiguous(opt)
            if opt[0].from_stop != options[0][0].from_stop:
                raise ValueError("catch_next options must start at the same stop")
        if options and legs[-len(options[0]):] != options[0]:
            raise ValueError("catch_next[0] must be the planned onward legs (a suffix of legs)")
        if self.leave_home is not None:
            _require_utc("leave_home", self.leave_home)
        if self.infeasible and not self.infeasible_reason:
            raise ValueError("Infeasible candidates need an infeasible_reason")
        object.__setattr__(self, "legs", legs)
        object.__setattr__(self, "catch_next", options)
        if self.exposure is not None:
            object.__setattr__(self, "exposure", frozenset(self.exposure))

    @property
    def base_cost_usd(self) -> float | None:
        """None while unpriced (before 1.4, and the drive-all-the-way reference row)."""
        return None if self.costs is None else self.costs.total_usd

    @property
    def sched_dep(self) -> datetime:
        return self.legs[0].sched_dep

    @property
    def sched_arr(self) -> datetime:
        return self.legs[-1].sched_arr

    @property
    def planned_train(self) -> Leg | None:
        """The first rail leg of the planned itinerary."""
        return next((leg for leg in self.legs if leg.mode is LegMode.RAIL), None)

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

    run_count is the population at the chosen level: runs observed at the
    alighting stop (0 at NO_DATA). board_zero_share is the share of those runs
    whose boarding delay was not observed and set to 0. delay_field_at_stop says
    which delay measure each stop's values came from.
    """

    pool_key: PoolKey
    fallback_level: FallbackLevel
    run_count: int
    delay_field_at_stop: Mapping[str, DelayField]
    train_number: str | None = None  # set by the scorer, which knows the leg
    board_zero_share: float = 0.0
    intermediate_alight: bool = False  # leg alights before its trip's terminal (period approximated)

    def __post_init__(self) -> None:
        object.__setattr__(self, "delay_field_at_stop", dict(self.delay_field_at_stop))


@dataclass(frozen=True)
class ScoredOption:
    """Scorer → UI. Money and costs in USD, lateness in minutes, times UTC.

    Simulation fields are None when the candidate has no legs (no eligible
    train). gen_cost = money + time_cost + exp_late_cost; it is None while
    money is not priced (drive-all-the-way reference row). rank orders by
    gen_cost, sched_rank by money + time_cost only (no risk); infeasible
    candidates rank after feasible ones; unrankable rows have rank None.
    """

    candidate: Candidate
    p_on_time: float | None  # 0–1
    arrival_p50: datetime | None
    arrival_p90: datetime | None
    expected_late_min: float | None
    censored_share: float | None  # draws with no catchable train
    train_taken_share: Mapping[str, float]  # train number (or "none") → share of draws
    money_usd: float | None  # CostBreakdown.total_usd (may be partial)
    time_cost_usd: float | None  # VOT × (deadline − leave_home)
    exp_late_cost_usd: float | None  # mean(VOT × L + hard_penalty × 1(L > 0))
    gen_cost_usd: float | None
    rank: int | None = None
    sched_rank: int | None = None
    pools: tuple[PoolUsage, ...] = ()
    flags: frozenset[str] = frozenset()
    assumption_flags: frozenset[AssumptionFlag] = field(default_factory=frozenset)
    active_alerts: tuple[str, ...] = ()
    seed: int | None = None
    n_samples: int = 0
    # Train choice: every planned variant of this station row (incl. this one), and the
    # latest-train variant's money + time cost, which sched_rank uses (timetable baseline).
    variants: tuple["ScoredOption", ...] = ()
    sched_basis_usd: float | None = None

    def __post_init__(self) -> None:
        if self.p_on_time is not None and not 0.0 <= self.p_on_time <= 1.0:
            raise ValueError(f"p_on_time must be in [0, 1], got {self.p_on_time}")
        for name in ("arrival_p50", "arrival_p90"):
            value = getattr(self, name)
            if value is not None:
                _require_utc(name, value)
        if self.arrival_p50 and self.arrival_p90 and self.arrival_p90 < self.arrival_p50:
            raise ValueError("arrival_p90 is earlier than arrival_p50")
        object.__setattr__(self, "train_taken_share", dict(self.train_taken_share))
        object.__setattr__(self, "flags", frozenset(self.flags))
        object.__setattr__(self, "assumption_flags", frozenset(self.assumption_flags))

    @property
    def candidate_id(self) -> str:
        return self.candidate.label or self.candidate.access_station
