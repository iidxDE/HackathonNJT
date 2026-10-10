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
MORRIS_ESSEX_ROUTE = "7"

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
    """M&E station -> HB, walk, first PATH Hoboken -> 33rd St that fits, walk to Penn."""
    rail = stop_pairs(njt, service_date, station, HB)
    rail = rail[(rail["route_id"] == MORRIS_ESSEX_ROUTE) & (rail["arr_s"] < config.SECONDS_PER_DAY)]
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
    return leave, drive_s(dest, leave)


def _access_legs(drive_s: DriveTime, dest: str, board_stop: str, board_dep: datetime,
                 buffer_s: int) -> tuple[datetime, tuple[Leg, ...]]:
    """DRIVE ORIGIN -> LOT:dest, margin, WALK(buffer) LOT:dest -> board_stop."""
    walk_start = board_dep - timedelta(seconds=buffer_s)
    park_by = walk_start - timedelta(seconds=config.SAFETY_MARGIN_S)
    leave, d = solve_leave_home(drive_s, dest, park_by)
    lot = f"LOT:{dest}"
    drive = Leg(LegMode.DRIVE, "ORIGIN", lot, leave, leave + timedelta(seconds=d))
    walk = Leg(LegMode.WALK, lot, board_stop, walk_start, board_dep)
    return leave, (drive, walk)


def _hhmm(t: datetime) -> str:
    return pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M")


def plan_station(options: list[Option], station: str, drive_dest: str, deadline: datetime,
                 drive_s: DriveTime, label: str, service_date: date) -> Candidate:
    """Pick the planned option and catch-next list; build the access legs."""
    if not options:
        return Candidate(station, (), None, None, "safety_net", label=label, infeasible=True,
                         infeasible_reason=f"no eligible train from {station} on {service_date}")
    options = sorted(options, key=lambda o: (o[-1].sched_arr, o[0].sched_dep))
    on_time = [i for i, o in enumerate(options) if o[-1].sched_arr <= deadline]
    infeasible = not on_time
    planned = options[on_time[-1]] if on_time else options[0]
    later = sorted((o for o in options if o is not planned and o[0].sched_dep >= planned[0].sched_dep),
                   key=lambda o: (o[0].sched_dep, o[-1].sched_arr))
    catch_next = (planned, *later[:config.CATCH_NEXT_K])
    leave, access = _access_legs(drive_s, drive_dest, planned[0].from_stop, planned[0].sched_dep,
                                 config.STATION_BUFFER_S[station])
    reason = (f"no train arrives by {_hhmm(deadline)}; earliest arrives {_hhmm(planned[-1].sched_arr)}"
              if infeasible else None)
    return Candidate(station, access + planned, None, None, "safety_net", catch_next=catch_next,
                     leave_home=leave, infeasible=infeasible, infeasible_reason=reason, label=label)


def drive_all_the_way(deadline: datetime, drive_s: DriveTime) -> Candidate:
    """Reference row: drive to a Midtown garage, walk to Penn. Not priced."""
    walk_s = config.GARAGE_TO_PENN_WALK_S
    walk_start = deadline - timedelta(seconds=walk_s)
    leave, d = solve_leave_home(drive_s, config.GARAGE_DEST,
                                walk_start - timedelta(seconds=config.SAFETY_MARGIN_S))
    lot = f"LOT:{config.GARAGE_DEST}"
    legs = (Leg(LegMode.DRIVE, "ORIGIN", lot, leave, leave + timedelta(seconds=d)),
            Leg(LegMode.WALK, lot, NY, walk_start, deadline))
    return Candidate("DRIVE", legs, None, None, "safety_net", leave_home=leave,
                     label="Drive all the way (not priced)")


def build_candidates(njt: GtfsFeed, path: GtfsFeed, drive_s: DriveTime,
                     service_date: date = config.DEMO_SERVICE_DATE,
                     deadline_s: int = config.DEMO_DEADLINE_S) -> list[Candidate]:
    """The 7 safety-net candidates for an arrive-by deadline at NY Penn (local seconds)."""
    deadline = to_utc(service_date, deadline_s)
    cands = [plan_station(direct_options(njt, service_date, s), s, s, deadline, drive_s,
                          f"{s} → NY", service_date)
             for s in DIRECT_STATIONS]
    cands.append(plan_station(hoboken_path_options(njt, path, service_date, "CM"), "CM", "CM",
                              deadline, drive_s, "CM → HB + PATH", service_date))
    cands.append(drive_all_the_way(deadline, drive_s))
    return cands
