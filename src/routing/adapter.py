"""Router → contracts adapter: RAPTOR journeys become safety-net style Options.

router_options() returns the same type as safety_net.candidates.station_options()
(dict[label, list[Option]]), so plan_variants, the pricer, the exposure tagger,
the scorer and the UI run unchanged. This module is the boundary where router
ids meet the Leg contract; src/routing/network.py and raptor.py stay
network-agnostic.

- Stops: "NJT:<code>" → "<code>", "PATH:<name>" unchanged, the virtual target
  → config.DESTINATION_STATION. A zero-length walk that maps onto itself
  (NJT:NY → DEST:NYP) is dropped.
- Times: service-day seconds → UTC as local noon on the service date − 12 h +
  seconds (the GTFS definition; equals local midnight + seconds on non-DST days).
- Key = the journey signature (access, [(mode, board, alight) per ride]), line
  not included. Hard-coded signatures keep their safety-net labels.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src import config
from src.contracts import Leg, LegMode
from src.gtfs import gtfs_to_utc
from src.routing.network import Network
from src.routing.raptor import Journey, ProfileStats, route_profile
from src.safety_net.candidates import (DIRECT_STATIONS, HB, HB_PATH_STATIONS, NY, PATH_33, PATH_HB,
                                       Option, hb_path_label)

log = logging.getLogger(__name__)

_MODE = {"rail": LegMode.RAIL, "path": LegMode.PATH, "walk": LegMode.WALK}

Signature = tuple[str, tuple[tuple[str, str, str], ...]]


@dataclass
class AdapterReport:
    """Counts for eval/router_validation.md."""
    journeys: Counter = field(default_factory=Counter)  # per access station
    dropped: Counter = field(default_factory=Counter)  # reason → count
    profile: dict[str, ProfileStats] = field(default_factory=dict)


def gtfs_utc(service_date: date, seconds: float) -> datetime:
    """GTFS time → aware UTC: local noon on service_date − 12 h + seconds."""
    return gtfs_to_utc(service_date, seconds, config.LOCAL_TZ)


def leg_stop(stop: str) -> str:
    if stop == config.ROUTER_DEST_STOP:
        return config.DESTINATION_STATION
    return stop.removeprefix(config.ROUTER_NJT_PREFIX)


def label_for(sig: Signature) -> str:
    """Safety-net label for hard-coded signatures, else a readable route string."""
    access, rides = sig
    if rides == (("rail", access, NY),):
        return f"{access} → NY"
    if rides == (("rail", access, HB), ("path", PATH_HB, PATH_33)):
        return hb_path_label(access)
    parts, prev = [], access
    for mode, board, alight in rides:
        if board != prev:
            parts[-1:] = [f"{parts[-1]}/{board}"] if parts else [board]
        parts.append(alight.replace("PATH:", "PATH ") if mode == "path" else alight)
        prev = alight
    return f"{access} → " + " → ".join(parts)


def _supported(rides: tuple[tuple[str, str, str], ...]) -> bool:
    """Shapes the scorer can simulate: rail; rail → rail; rail → HB + PATH Hoboken → 33rd St."""
    modes = tuple(m for m, _, _ in rides)
    if modes in (("rail",), ("rail", "rail")):
        return True
    return modes == ("rail", "path") and rides[1][1:] == (PATH_HB, PATH_33)


def journey_to_option(j: Journey, net: Network, service_date: date,
                      trip_index: dict[str, tuple[int, int]]) -> tuple[Option | None, str | None]:
    """Journey → (Option, None), or (None, drop reason)."""
    legs: list[Leg] = []
    for jl in j.legs:
        frm, to = leg_stop(jl.board_stop), leg_stop(jl.alight_stop)
        if jl.mode == "walk":
            if frm == to and jl.arr == jl.dep:
                continue
            legs.append(Leg(LegMode.WALK, frm, to, gtfs_utc(service_date, jl.dep),
                            gtfs_utc(service_date, jl.arr)))
            continue
        g, p = trip_index[jl.trip_id]
        route = jl.route_id.split(":", 1)[1]
        mode = _MODE[jl.mode]
        if mode is LegMode.RAIL:
            if net.trip_synthetic[g]:
                return None, "rail trip without trip_short_name"
            direction = int(net.trip_direction[g])
            if direction < 0:
                return None, "rail trip without direction_id"
            terminal = leg_stop(net.terminal_of_pattern(p))
            legs.append(Leg(mode, frm, to, gtfs_utc(service_date, jl.dep), gtfs_utc(service_date, jl.arr),
                            train_number=jl.train_no, gtfs_line=route, direction=direction,
                            toward_ny=terminal in config.TOWARD_NY_TERMINALS, terminal=terminal))
        else:
            legs.append(Leg(mode, frm, to, gtfs_utc(service_date, jl.dep), gtfs_utc(service_date, jl.arr),
                            gtfs_line=route))
    return tuple(legs), None


def signature(access: str, option: Option) -> Signature:
    return access, tuple((leg.mode.value, leg.from_stop, leg.to_stop)
                         for leg in option if leg.mode is not LegMode.WALK)


def router_options(network: Network, service_date: date = config.DEMO_SERVICE_DATE,
                   deadline_s: int = config.DEMO_DEADLINE_S,
                   stations: tuple[str, ...] = config.ROUTER_ACCESS_STATIONS,
                   report: AdapterReport | None = None) -> dict[str, list[Option]]:
    """Router Options per signature label (same type as station_options).

    Every hard-coded label is present (empty list when the router finds no such
    journey, e.g. CM → NY during the Portal cutover), so build_candidates keeps
    its infeasible rows. Options within a key are sorted by departure. Cached on
    the network per (service_date, deadline_s, stations) unless a report is asked.
    """
    cache = network.__dict__.setdefault("_router_options", {})
    key = (service_date, deadline_s, stations)
    if report is None and key in cache:
        return cache[key]
    rep = report if report is not None else AdapterReport()
    tindex = network.__dict__.get("_trip_index") or network.__dict__.setdefault(
        "_trip_index", network.trip_index())
    out: dict[str, list[Option]] = {f"{s} → NY": [] for s in DIRECT_STATIONS}
    out.update({hb_path_label(s): [] for s in HB_PATH_STATIONS})
    for s in stations:
        stats = ProfileStats()
        js = route_profile(network, config.ROUTER_NJT_PREFIX + s, service_date, deadline_s, stats=stats)
        rep.profile[s] = stats
        if stats.dropped_rides:
            rep.dropped[f"more than {config.ROUTER_MAX_RIDES_OUT} rides"] += stats.dropped_rides
        for j in js:
            opt, why = journey_to_option(j, network, service_date, tindex)
            if opt is None:
                rep.dropped[why] += 1
                continue
            sig = signature(s, opt)
            if not _supported(sig[1]):
                rep.dropped["shape not simulated by the scorer"] += 1
                continue
            rep.journeys[s] += 1
            out.setdefault(label_for(sig), []).append(opt)
    for opts in out.values():
        opts.sort(key=lambda o: (o[0].sched_dep, o[-1].sched_arr))
    if report is None:
        cache[key] = out
    return out
