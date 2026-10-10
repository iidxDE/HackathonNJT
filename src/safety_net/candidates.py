"""Hard-coded safety-net candidates: station policies with a planned train.

Deterministic planning from GTFS for one service date and an arrive-by
deadline at NY Penn (local seconds since midnight):

1. Target: arrival at NY Penn <= deadline (PATH: 33rd St arrival + egress walk).
2. Planned option: the latest eligible option whose target arrival <= deadline.
   No such option -> the earliest one, and the candidate is infeasible.
3. leave_home = planned departure - station buffer - margin - drive(BEST_GUESS
   at leave_home), solved by fixed-point iteration.
4. catch_next: the planned option plus the next CATCH_NEXT_K departures,
   including ones after the deadline (the scorer needs them for lateness).

Stop ids: NJT 2-char codes, "PATH:<stop_name>", "ORIGIN", and "LOT:<dest>"
for the parking point a drive ends at (drive_destinations.csv id).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta

import pandas as pd

from src import config
from src.contracts import Candidate, Leg, LegMode
from src.gtfs import GtfsFeed, stop_pairs

DIRECT_STATIONS: tuple[str, ...] = ("CM", "ST", "RT", "SE", "NP")
NY = config.DESTINATION_STATION
HB = "HB"
PATH_HB = f"PATH:{config.PATH_HOBOKEN_STOP}"
PATH_33 = f"PATH:{config.PATH_33RD_STOP}"
# Midtown Direct: Morris & Essex (7) and Gladstone (8). Diverted to Hoboken on
# weekdays during the Portal cutover (2026-10-11 – 11-14).
MIDTOWN_DIRECT_ROUTES: tuple[str, ...] = ("7", "8")
HB_PATH_STATIONS: tuple[str, ...] = ("CM", "ST", "RT")
CUTOVER_REASON = "Midtown Direct diverted to Hoboken (Portal cutover)"

# dest id, depart time (UTC) -> BEST_GUESS drive seconds
DriveTime = Callable[[str, datetime], float]

Option = tuple[Leg, ...]


def to_utc(service_date: date, gtfs_s: float) -> datetime:
    """GTFS seconds since service-day midnight (local wall clock) -> aware UTC datetime."""
    local = pd.Timestamp(service_date) + pd.Timedelta(seconds=float(gtfs_s))
    return local.tz_localize(config.LOCAL_TZ).tz_convert("UTC").to_pydatetime()


def _rail_leg(row, service_date: date, from_stop: str, to_stop: str) -> Leg:
    return Leg(LegMode.RAIL, from_stop, to_stop,
               to_utc(service_date, row.dep_s), to_utc(service_date, row.arr_s),
               train_number=row.trip_short_name, gtfs_line=str(row.route_id),
               direction=int(row.direction_id),
               toward_ny=row.terminal in config.TOWARD_NY_TERMINALS)


def direct_options(njt: GtfsFeed, service_date: date, station: str) -> list[Option]:
    """One-seat rides station -> NY on toward-NY trips, any line, same service day."""
    pairs = stop_pairs(njt, service_date, station, NY)
    pairs = pairs[pairs["terminal"].isin(config.TOWARD_NY_TERMINALS)
                  & (pairs["arr_s"] < config.SECONDS_PER_DAY)]
    return [(_rail_leg(r, service_date, station, NY),) for r in pairs.itertuples()]


def hoboken_path_options(njt: GtfsFeed, path: GtfsFeed, service_date: date,
                         station: str = "CM") -> list[Option]:
    """Midtown Direct station -> HB, walk, first PATH Hoboken -> 33rd St that fits, walk to Penn."""
    rail = stop_pairs(njt, service_date, station, HB)
    rail = rail[rail["route_id"].isin(MIDTOWN_DIRECT_ROUTES) & (rail["arr_s"] < config.SECONDS_PER_DAY)]
    pth = stop_pairs(path, service_date, config.PATH_HOBOKEN_STOP, config.PATH_33RD_STOP,
                     key_col="stop_name").sort_values("dep_s")
    xfer = config.TRANSFER_MIN_S["hoboken_njt_to_path"]
    egress = config.TRANSFER_MIN_S["path_33st_to_penn_walk"]
    options = []
    for r in rail.itertuples():
        ok = pth[pth["dep_s"] >= r.arr_s + xfer]
        if ok.empty:
            continue
        p = ok.iloc[0]
        rail_leg = _rail_leg(r, service_date, station, HB)
        walk_in = Leg(LegMode.WALK, HB, PATH_HB, rail_leg.sched_arr,
                      rail_leg.sched_arr + timedelta(seconds=xfer))
        path_leg = Leg(LegMode.PATH, PATH_HB, PATH_33, to_utc(service_date, p.dep_s),
                       to_utc(service_date, p.arr_s), gtfs_line=str(p.route_id))
        walk_out = Leg(LegMode.WALK, PATH_33, NY, path_leg.sched_arr,
                       path_leg.sched_arr + timedelta(seconds=egress))
        options.append((rail_leg, walk_in, path_leg, walk_out))
    return options


def solve_leave_home(drive_s: DriveTime, dest: str, arrive_by: datetime) -> tuple[datetime, float]:
    """Latest departure with departure + drive(departure) ~= arrive_by. Returns (leave, drive_s)."""
    leave = arrive_by - timedelta(seconds=drive_s(dest, arrive_by))
    for _ in range(config.LEAVE_HOME_MAX_ROUNDS):
        new = arrive_by - timedelta(seconds=drive_s(dest, leave))
        done = abs((new - leave).total_seconds()) < config.LEAVE_HOME_TOL_S
        leave = new
        if done:
            break
    # The tolerance can leave the arrival up to LEAVE_HOME_TOL_S late. If so, bisect
    # for the latest on-time departure (arrival is non-decreasing in departure).
    def arrives(t: datetime) -> datetime:
        return t + timedelta(seconds=drive_s(dest, t))

    if arrives(leave) > arrive_by:
        lo, hi = leave - timedelta(hours=1), leave
        if arrives(lo) > arrive_by:
            raise ValueError(f"No departure within 1 h before {leave} reaches {dest} by {arrive_by}")
        while (hi - lo).total_seconds() > 1:
            mid = lo + (hi - lo) / 2
            lo, hi = (mid, hi) if arrives(mid) <= arrive_by else (lo, mid)
        leave = lo
    return leave, drive_s(dest, leave)


def _access_legs(drive_s: DriveTime, dest: str, board_stop: str, board_dep: datetime,
                 buffer_s: int, margin_s: int) -> tuple[datetime, tuple[Leg, ...]]:
    """DRIVE ORIGIN -> LOT:dest, margin, WALK(buffer) LOT:dest -> board_stop."""
    walk_start = board_dep - timedelta(seconds=buffer_s)
    park_by = walk_start - timedelta(seconds=margin_s)
    leave, d = solve_leave_home(drive_s, dest, park_by)
    lot = f"LOT:{dest}"
    drive = Leg(LegMode.DRIVE, "ORIGIN", lot, leave, leave + timedelta(seconds=d))
    walk = Leg(LegMode.WALK, lot, board_stop, walk_start, board_dep)
    return leave, (drive, walk)


def _hhmm(t: datetime) -> str:
    return pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M")


def plan_station(options: list[Option], station: str, drive_dest: str, deadline: datetime,
                 drive_s: DriveTime, label: str, service_date: date,
                 margin_s: int = config.SAFETY_MARGIN_S, no_train_reason: str | None = None,
                 planned: Option | None = None, variant: int = 0,
                 source: str = "safety_net") -> Candidate:
    """Pick the planned option and catch-next list; build the access legs.

    Planned = the on-time option that departs latest (tie: earliest arrival), unless
    `planned` is given (train-choice variants). If none is on time, the
    earliest-arriving option, and the candidate is infeasible.
    """
    if not options:
        return Candidate(station, (), None, None, source, label=label, infeasible=True,
                         infeasible_reason=no_train_reason or f"no eligible train from {station} on {service_date}")
    on_time = on_time_latest_first(options, deadline)
    infeasible = not on_time
    if planned is None:
        planned = on_time[0] if on_time else min(options, key=lambda o: (o[-1].sched_arr, o[0].sched_dep))
    later = sorted((o for o in options if o is not planned and o[0].sched_dep >= planned[0].sched_dep),
                   key=lambda o: (o[0].sched_dep, o[-1].sched_arr))
    catch_next = (planned, *later[:config.CATCH_NEXT_K])
    leave, access = _access_legs(drive_s, drive_dest, planned[0].from_stop, planned[0].sched_dep,
                                 config.STATION_BUFFER_S[station], margin_s)
    reason = (f"no train arrives by {_hhmm(deadline)}; earliest arrives {_hhmm(planned[-1].sched_arr)}"
              if infeasible else None)
    return Candidate(station, access + planned, None, None, source, catch_next=catch_next,
                     leave_home=leave, infeasible=infeasible, infeasible_reason=reason, label=label,
                     variant=variant)


def on_time_latest_first(options: list[Option], deadline: datetime) -> list[Option]:
    """On-time options, latest departure first (tie: earliest arrival)."""
    on_time = [o for o in options if o[-1].sched_arr <= deadline]
    return sorted(on_time, key=lambda o: (-o[0].sched_dep.timestamp(), o[-1].sched_arr))


def plan_variants(options: list[Option], station: str, drive_dest: str, deadline: datetime,
                  drive_s: DriveTime, label: str, service_date: date,
                  margin_s: int = config.SAFETY_MARGIN_S, no_train_reason: str | None = None,
                  n_variants: int = config.TRAIN_CHOICE_T, source: str = "safety_net") -> list[Candidate]:
    """Train choice: one candidate per each of the last n_variants on-time trains.

    Variant 0 is the latest on-time train (the timetable plan); each variant has its
    own leave_home and catch-next list. Infeasible stations yield one candidate.
    """
    on_time = on_time_latest_first(options, deadline)
    if not on_time:
        return [plan_station(options, station, drive_dest, deadline, drive_s, label, service_date,
                             margin_s, no_train_reason, source=source)]
    return [plan_station(options, station, drive_dest, deadline, drive_s, label, service_date, margin_s,
                         no_train_reason, planned=o, variant=k, source=source)
            for k, o in enumerate(on_time[:n_variants])]


def drive_all_the_way(deadline: datetime, drive_s: DriveTime,
                      margin_s: int = config.SAFETY_MARGIN_S) -> Candidate:
    """Reference row: drive to a Midtown garage, walk to Penn. Not priced."""
    walk_s = config.GARAGE_TO_PENN_WALK_S
    walk_start = deadline - timedelta(seconds=walk_s)
    leave, d = solve_leave_home(drive_s, config.GARAGE_DEST,
                                walk_start - timedelta(seconds=margin_s))
    lot = f"LOT:{config.GARAGE_DEST}"
    legs = (Leg(LegMode.DRIVE, "ORIGIN", lot, leave, leave + timedelta(seconds=d)),
            Leg(LegMode.WALK, lot, NY, walk_start, deadline))
    return Candidate("DRIVE", legs, None, None, "safety_net", leave_home=leave,
                     label="Drive all the way (not priced)")


def hb_path_label(station: str) -> str:
    return f"{station} → HB + PATH"


def station_options(njt: GtfsFeed, path: GtfsFeed,
                    service_date: date = config.DEMO_SERVICE_DATE) -> dict[str, list[Option]]:
    """Every onward option per candidate label for a service date (deadline-independent)."""
    opts = {f"{s} → NY": direct_options(njt, service_date, s) for s in DIRECT_STATIONS}
    for s in HB_PATH_STATIONS:
        opts[hb_path_label(s)] = hoboken_path_options(njt, path, service_date, s)
    return opts


def build_candidates(njt: GtfsFeed | None, path: GtfsFeed | None, drive_s: DriveTime,
                     service_date: date = config.DEMO_SERVICE_DATE,
                     deadline_s: int = config.DEMO_DEADLINE_S,
                     margin_s: int = config.SAFETY_MARGIN_S,
                     options: dict[str, list[Option]] | None = None,
                     n_variants: int = config.TRAIN_CHOICE_T,
                     source: str = "safety_net") -> list[Candidate]:
    """Safety-net candidates for an arrive-by deadline at NY Penn (local seconds):
    CM/ST/RT/SE/NP → NY, CM/ST/RT → HB + PATH, and drive all the way. Each station
    row comes as up to n_variants train-choice variants (same label, variant 0..).

    Pass precomputed station_options() to skip the GTFS scan (njt / path unused then),
    or router_options() with source="router": keys beyond the hard-coded ones are
    planned too, with the station read from the options' first leg.
    """
    deadline = to_utc(service_date, deadline_s)
    opts = options if options is not None else station_options(njt, path, service_date)
    cands = []
    for s in DIRECT_STATIONS:
        cands += plan_variants(opts[f"{s} → NY"], s, s, deadline, drive_s, f"{s} → NY", service_date,
                               margin_s, CUTOVER_REASON if s in HB_PATH_STATIONS else None, n_variants,
                               source)
    for s in HB_PATH_STATIONS:
        cands += plan_variants(opts[hb_path_label(s)], s, s, deadline, drive_s, hb_path_label(s),
                               service_date, margin_s, n_variants=n_variants, source=source)
    known = {f"{s} → NY" for s in DIRECT_STATIONS} | {hb_path_label(s) for s in HB_PATH_STATIONS}
    for label, extra in sorted(opts.items()):
        if label in known or not extra:
            continue
        s = extra[0][0].from_stop
        cands += plan_variants(extra, s, s, deadline, drive_s, label, service_date, margin_s,
                               n_variants=n_variants, source=source)
    cands.append(drive_all_the_way(deadline, drive_s, margin_s))
    return cands
