"""Monte Carlo scorer: Candidate (priced, tagged) → ScoredOption.

Per draw i, vectorized over i (CLAUDE.md §4.8, Phase 1.5 spec):
1. u_i ~ U(0,1), shared by all candidates (common random numbers);
   drive_i = triangular inverse CDF at u_i over the traffic models at leave_home;
   ready_i = leave_home + drive_i + station buffer.
2. Each catch-next train k draws (board, alight) delay from ONE whole run
   (pools.sample); board = max(0, board). Trains are drawn independently.
3. Catchable: ready_i <= sched_dep_k + board_k. The rider takes the catchable
   train with the earliest SCHEDULED arrival; arrival = sched_arr + alight.
   Nothing catchable → arrival = deadline + cap (censored).
4. Hoboken + PATH: HB actual arrival + transfer → next scheduled PATH
   departure → 33rd St + egress walk. PATH runs to schedule (stated limitation).
5. Drive all the way: arrival = leave_home + drive_i + garage walk.
6. L_i = max(0, arrival_i − deadline) in minutes;
   gen_cost = money + VOT × (deadline − leave_home) + mean(VOT × L + penalty × 1(L > 0)).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from src import config
from src.contracts import (AssumptionFlag, Candidate, FallbackLevel, LegMode, PoolUsage,
                           ScoredOption)
from src.scoring.drive import DriveEstimate, lookup
from src.scoring.pools import period_of, sample


@dataclass(frozen=True)
class Settings:
    """User settings plus sensitivity knobs (delay_scale / delay_shift_s / drive_spread)."""

    deadline_s: int = config.DEMO_DEADLINE_S  # local seconds since midnight
    vot_usd_per_h: float = config.DEFAULT_VOT_USD_PER_H
    hard_penalty_usd: float = config.DEFAULT_HARD_PENALTY_USD
    margin_s: int = config.SAFETY_MARGIN_S
    n: int = config.N_SAMPLES
    seed: int = config.DEFAULT_SEED
    late_cap_min: float = config.LATE_CAP_MIN
    mpg: float = config.DEFAULT_MPG
    delay_scale: float = 1.0  # 0 = no rail delay at all (sanity check)
    delay_shift_s: float = 0.0  # added to every pooled delay (sensitivity)
    drive_spread: bool = True  # False = drive takes BEST_GUESS exactly


@dataclass(frozen=True)
class ScoringInputs:
    """Request-time artifacts: pools.parquet, drive_cache.parquet, PATH timetable."""

    pools: pd.DataFrame
    drive_cache: pd.DataFrame
    path_schedule: np.ndarray  # (m, 2) epoch seconds (Hoboken dep, 33rd St arr), sorted by dep
    service_date: date = config.DEMO_SERVICE_DATE


def common_uniforms(settings: Settings) -> np.ndarray:
    """The shared u_i for the drive draws (common random numbers across candidates)."""
    return np.random.default_rng(settings.seed).uniform(size=settings.n)


def triangular_ppf(u: np.ndarray, low: float, mode: float, high: float) -> np.ndarray:
    """Inverse CDF of triangular(low, mode, high); constant if low == high."""
    if high == low:
        return np.full_like(u, low, dtype=float)
    span = high - low
    cut = (mode - low) / span
    left = low + np.sqrt(u * span * (mode - low))
    right = high - np.sqrt((1 - u) * span * (high - mode))
    return np.where(u < cut, left, right)


def local_epoch(service_date: date, local_s: float) -> float:
    """Epoch seconds of a local time of day (seconds since midnight) on service_date."""
    local = pd.Timestamp(service_date) + pd.Timedelta(seconds=float(local_s))
    return local.tz_localize(config.LOCAL_TZ).timestamp()


def _ts(t: datetime) -> float:
    return t.timestamp()


def _utc(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(float(epoch_s), tz=timezone.utc)


def _train_rng(seed: int, c: Candidate, k: int, train: str) -> np.random.Generator:
    key = zlib.crc32(f"{c.label}|{k}|{train}".encode())
    return np.random.default_rng(np.random.SeedSequence([seed, key]))


def _drive_draws(est: DriveEstimate, u: np.ndarray, settings: Settings) -> np.ndarray:
    if not settings.drive_spread:
        return np.full_like(u, est.best_guess_s, dtype=float)
    return triangular_ppf(u, est.low_s, est.mode_s, est.high_s)


def _unscored(c: Candidate, settings: Settings) -> ScoredOption:
    money = c.costs.total_usd if c.costs else None
    flags = {"infeasible"} | (set(c.costs.flags) if c.costs else set()) | set(c.flags)
    return ScoredOption(c, None, None, None, None, None, {}, money, None, None, None,
                        flags=flags, seed=settings.seed, n_samples=0)


def score_candidate(c: Candidate, inputs: ScoringInputs, settings: Settings,
                    u: np.ndarray) -> ScoredOption:
    """Simulate one candidate; rank fields are left None (see rank_options)."""
    if not c.legs or c.leave_home is None:
        return _unscored(c, settings)
    n = settings.n
    deadline = local_epoch(inputs.service_date, settings.deadline_s) + config.ON_TIME_GRACE_S
    leave = _ts(c.leave_home)
    drive_leg = c.legs[0]
    est = lookup(inputs.drive_cache, drive_leg.to_stop.removeprefix("LOT:"), c.leave_home)
    drive = _drive_draws(est, u, settings)
    assumption = set(est.flags)
    flags: set[str] = set(c.flags) | (set(c.costs.flags) if c.costs else set())
    usages: list[PoolUsage] = []

    if c.access_station == "DRIVE":
        arrival = leave + drive + config.GARAGE_TO_PENN_WALK_S
        taken = np.array(["drive"] * n)
        censored = np.zeros(n, dtype=bool)
    else:
        ready = leave + drive + config.STATION_BUFFER_S[c.access_station]
        options = c.catch_next
        catch = np.zeros((n, len(options)), dtype=bool)
        arr = np.full((n, len(options)), np.nan)
        sched_arr = np.array([_ts(o[-1].sched_arr) for o in options])
        option_usage = []
        for k, opt in enumerate(options):
            rail = next(leg for leg in opt if leg.mode is LegMode.RAIL)
            draws, usage = sample(inputs.pools, rail.from_stop, rail.to_stop, rail.gtfs_line,
                                  rail.toward_ny, period_of(rail.sched_arr), n,
                                  _train_rng(settings.seed, c, k, rail.train_number))
            d = draws.delays_s * settings.delay_scale + settings.delay_shift_s
            board, alight = np.maximum(0.0, d[:, 0]), d[:, 1]
            catch[:, k] = ready <= _ts(rail.sched_dep) + board
            rail_arr = _ts(rail.sched_arr) + alight
            if any(leg.mode is LegMode.PATH for leg in opt):
                arr[:, k] = _path_arrival(rail_arr, inputs.path_schedule)
                assumption |= {AssumptionFlag.PATH_UNMEASURED, AssumptionFlag.PATH_DELAY_NOT_MODELED}
            else:
                arr[:, k] = rail_arr
            option_usage.append(replace(usage, train_number=rail.train_number))
        order = np.argsort(sched_arr, kind="stable")  # earliest scheduled arrival first
        catch_o, arr_o = catch[:, order], arr[:, order]
        any_catch = catch_o.any(axis=1)
        first = catch_o.argmax(axis=1)
        arrival = arr_o[np.arange(n), first]
        censored = ~any_catch | np.isnan(arrival)
        chosen = order[first]
        trains = np.array([o[0].train_number for o in options])
        taken = np.where(censored, "none", trains[chosen])
        for k in np.unique(chosen[~censored]):
            usages.append(option_usage[k])
        if any(u_.fallback_level is FallbackLevel.NO_DATA for u_ in usages):
            flags.add("l4_pool")

    arrival = np.where(censored, deadline + settings.late_cap_min * 60, arrival)
    late_min = np.maximum(0.0, arrival - deadline) / 60
    late = late_min > 0
    time_cost = settings.vot_usd_per_h * (deadline - config.ON_TIME_GRACE_S - leave) / 3600
    exp_late = float(np.mean(settings.vot_usd_per_h * late_min / 60 + settings.hard_penalty_usd * late))
    money = c.costs.total_usd if c.costs else None
    gen = None if money is None else money + time_cost + exp_late

    if c.infeasible:
        flags.add("infeasible")
    if c.costs is not None and c.costs.partial:
        flags.add("partial")
    if AssumptionFlag.DRIVE_VARIANCE_DEGENERATE in assumption:
        flags.add("drive_variance_degenerate")
    if censored.any():
        flags.add("censored")
    labels, counts = np.unique(taken, return_counts=True)
    p50, p90 = np.quantile(arrival, [0.5, 0.9])
    return ScoredOption(
        c, float(np.mean(~late)), _utc(p50), _utc(p90), float(np.mean(late_min)),
        float(np.mean(censored)), {str(t): float(k / n) for t, k in zip(labels, counts)},
        money, float(time_cost), exp_late, gen, pools=tuple(usages), flags=flags,
        assumption_flags=assumption, seed=settings.seed, n_samples=n)


def _path_arrival(hb_arrival: np.ndarray, path_schedule: np.ndarray) -> np.ndarray:
    """33rd St walk-out time for each HB arrival; NaN if no PATH departs after it."""
    ready = hb_arrival + config.TRANSFER_MIN_S["hoboken_njt_to_path"]
    idx = np.searchsorted(path_schedule[:, 0], ready, side="left")
    ok = idx < len(path_schedule)
    out = np.full(len(ready), np.nan)
    out[ok] = path_schedule[idx[ok], 1] + config.TRANSFER_MIN_S["path_33st_to_penn_walk"]
    return out


def rank_options(scored: list[ScoredOption]) -> list[ScoredOption]:
    """Assign rank (gen_cost) and sched_rank (money + time_cost); infeasible last.

    Unrankable (no simulation, or money not priced, e.g. drive all the way)
    keep rank None and are shown as reference rows.
    """
    rankable = [s for s in scored if s.gen_cost_usd is not None]

    def ordered(key) -> dict[int, int]:
        ranked = sorted(rankable, key=lambda s: ("infeasible" in s.flags, key(s)))
        return {id(s): i + 1 for i, s in enumerate(ranked)}

    rank = ordered(lambda s: s.gen_cost_usd)
    sched = ordered(lambda s: s.money_usd + s.time_cost_usd)
    return [replace(s, rank=rank.get(id(s)), sched_rank=sched.get(id(s))) for s in scored]


def reprice(scored: list[ScoredOption], settings: Settings,
            service_date: date = config.DEMO_SERVICE_DATE) -> list[ScoredOption]:
    """Re-apply VOT and hard penalty without re-simulating, then re-rank.

    Exact: exp_late_cost = VOT × E[L] / 60 + penalty × P(L > 0), and P(L > 0) = 1 − p_on_time.
    Only valid for settings that do not change the simulation (same deadline, margin, seed, N).
    """
    deadline = local_epoch(service_date, settings.deadline_s)
    out = []
    for s in scored:
        if s.p_on_time is None:
            out.append(s)
            continue
        time_cost = settings.vot_usd_per_h * (deadline - _ts(s.candidate.leave_home)) / 3600
        late = settings.vot_usd_per_h * s.expected_late_min / 60 + settings.hard_penalty_usd * (1 - s.p_on_time)
        gen = None if s.money_usd is None else s.money_usd + time_cost + late
        out.append(replace(s, time_cost_usd=time_cost, exp_late_cost_usd=late, gen_cost_usd=gen))
    return rank_options(out)


def score_all(cands: list[Candidate], inputs: ScoringInputs, settings: Settings) -> list[ScoredOption]:
    u = common_uniforms(settings)
    return rank_options([score_candidate(c, inputs, settings, u) for c in cands])
