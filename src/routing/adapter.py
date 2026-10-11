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
- Exclusion runs (3.1): per access station one plain run plus one run per
  config.ROUTER_EXCLUSIONS entry. Options are unioned by signature and
  deduplicated on the exact leg tuple. A signature only an exclusion run
  produced is flagged f"{config.ALT_FLAG_PREFIX}{name}"; exclusion-run trains
  landing on an existing signature are merged and counted. Exclusion-run shapes
  the scorer can't simulate go to AdapterReport.unscored_alternatives.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src import config
from src.contracts import Leg, LegMode
from src.gtfs import gtfs_to_utc
from src.routing.exclusion import Exclusion
from src.routing.network import Network
from src.routing.raptor import Journey, ProfileStats, route_profile
from src.safety_net.candidates import (DIRECT_STATIONS, HB, HB_PATH_STATIONS, NY, PATH_33, PATH_HB,
                                       Option, hb_path_label)

log = logging.getLogger(__name__)

_MODE = {"rail": LegMode.RAIL, "path": LegMode.PATH, "walk": LegMode.WALK}

Signature = tuple[str, tuple[tuple[str, str, str], ...]]


@dataclass
class AdapterReport:
    """Counts for eval/router_validation.md and eval/alternatives.md."""
    journeys: Counter = field(default_factory=Counter)  # per access station (plain run)
    dropped: Counter = field(default_factory=Counter)  # reason → count (plain run)
    profile: dict[str, ProfileStats] = field(default_factory=dict)  # plain run per station
    # Exclusion runs (3.1)
    exclusion_profile: dict[tuple[str, str], ProfileStats] = field(default_factory=dict)  # (station, name)
    label_flags: dict[str, frozenset[str]] = field(default_factory=dict)  # alternative label → flags
    trains_added_by_exclusion: Counter = field(default_factory=Counter)  # existing label → new options
    added_trains: dict[str, list[str]] = field(default_factory=dict)  # existing label → train numbers
    exclusion_options: list[tuple[str, str, Option]] = field(default_factory=list)  # (name, label, option)
    unscored_alternatives: list[dict] = field(default_factory=list)  # one row per unscored signature


@dataclass(frozen=True)
class RouterResult:
    options: dict[str, list[Option]]
    label_flags: dict[str, frozenset[str]]
    report: AdapterReport


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
    if len(rides) == 2 and rides[0][0] == "rail" and rides[0][2] == HB and rides[1] == ("path", PATH_HB, PATH_33):
        return f"{access} → {rides[0][1]} → HB + PATH"  # walk to another platform first (SE → TS)
    parts, prev = [], access
    for mode, board, alight in rides:
        if board != prev:
            parts[-1:] = [f"{parts[-1]}/{board}"] if parts else [board]
        parts.append(alight.replace("PATH:", "PATH ") if mode == "path" else alight)
        prev = alight
    return f"{access} → " + " → ".join(parts)


def _supported(rides: tuple[tuple[str, str, str], ...]) -> bool:
    """Shapes the scorer can simulate: rail; rail → rail; rail → HB + PATH Hoboken → 33rd St."""
    return unsupported_reason(rides) is None


def unsupported_reason(rides: tuple[tuple[str, str, str], ...]) -> str | None:
    """None if the scorer can simulate the shape, else why not."""
    modes = tuple(m for m, _, _ in rides)
    if modes in (("rail",), ("rail", "rail")):
        return None
    if modes == ("rail", "path") and rides[1][1:] == (PATH_HB, PATH_33):
        return None
    if "path" in modes:
        legs = ", ".join(f"{b.removeprefix('PATH:')} → {a.removeprefix('PATH:')}"
                         for m, b, a in rides if m == "path")
        return (f"PATH {legs}: the scorer simulates PATH Hoboken → 33rd St only "
                "(no other PATH schedule in the scorer; PATH delays not measured)")
    return f"shape {' → '.join(modes)} not simulated by the scorer"


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


def _exclusions_key(exclusions: Mapping[str, Exclusion]) -> tuple:
    return tuple(sorted(exclusions.items(), key=lambda kv: kv[0]))


def route_all(network: Network, service_date: date = config.DEMO_SERVICE_DATE,
              deadline_s: int = config.DEMO_DEADLINE_S,
              stations: tuple[str, ...] = config.ROUTER_ACCESS_STATIONS,
              exclusions: Mapping[str, Exclusion] | None = None,
              fresh: bool = False, base: Exclusion | None = None) -> RouterResult:
    """Plain + exclusion runs per station, unioned by signature (see module doc).

    Every hard-coded label is present (empty list when the router finds no such
    journey, e.g. CM → NY during the Portal cutover), so build_candidates keeps
    its infeasible rows. Options within a key are sorted by departure. Cached on
    the network per (service_date, deadline_s, stations, exclusions); fresh=True
    recomputes (fresh timings for reports). exclusions None = config.ROUTER_EXCLUSIONS,
    {} = plain runs only (router-v1). base (e.g. an alert's bans) is composed into every run:
    the plain run uses base, each named run base ∪ that exclusion. None / empty = no base.
    """
    exclusions = config.ROUTER_EXCLUSIONS if exclusions is None else exclusions
    cache = network.__dict__.setdefault("_router_options", {})
    base = base or None  # an empty Exclusion is no base (same cache entry, same runs)
    key = (service_date, deadline_s, stations, _exclusions_key(exclusions), base)
    if not fresh and key in cache:
        return cache[key]
    rep = AdapterReport()
    tindex = network.__dict__.get("_trip_index") or network.__dict__.setdefault(
        "_trip_index", network.trip_index())
    out: dict[str, list[Option]] = {f"{s} → NY": [] for s in DIRECT_STATIONS}
    out.update({hb_path_label(s): [] for s in HB_PATH_STATIONS})
    seen: dict[str, set[Option]] = {}
    plain_labels: set[str] = set()
    alt_names: dict[str, set[str]] = {}

    def add(label: str, opt: Option) -> bool:
        """Union by signature, dedupe on the exact leg tuple. True if new."""
        if opt in seen.setdefault(label, set()):
            return False
        seen[label].add(opt)
        out.setdefault(label, []).append(opt)
        return True

    for s in stations:
        stats = ProfileStats()
        js = route_profile(network, config.ROUTER_NJT_PREFIX + s, service_date, deadline_s,
                           exclusion=base, stats=stats)
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
            label = label_for(sig)
            plain_labels.add(label)
            add(label, opt)
        for name, ex in exclusions.items():
            stats = ProfileStats()
            js = route_profile(network, config.ROUTER_NJT_PREFIX + s, service_date, deadline_s,
                               exclusion=ex.union(base), stats=stats)
            rep.exclusion_profile[(s, name)] = stats
            if stats.dropped_rides:
                rep.unscored_alternatives.append({
                    "station": s, "exclusion": name, "key": "—", "options": stats.dropped_rides,
                    "example": "", "reason": f"more than {config.ROUTER_MAX_RIDES_OUT} rides"})
            unscored: dict[str, dict] = {}
            for j in js:
                opt, why = journey_to_option(j, network, service_date, tindex)
                if opt is None:
                    rep.unscored_alternatives.append({"station": s, "exclusion": name, "key": "—",
                                                      "options": 1, "example": "", "reason": why})
                    continue
                sig = signature(s, opt)
                label = label_for(sig)
                reason = unsupported_reason(sig[1])
                if reason is not None:
                    row = unscored.setdefault(label, {"station": s, "exclusion": name, "key": label,
                                                      "options": 0, "example": describe_option(opt),
                                                      "reason": reason})
                    row["options"] += 1
                    continue
                rep.exclusion_options.append((name, label, opt))
                if label not in plain_labels:
                    alt_names.setdefault(label, set()).add(name)
                if add(label, opt) and label in plain_labels:
                    rep.trains_added_by_exclusion[label] += 1
                    rep.added_trains.setdefault(label, []).append(option_train(opt))
            rep.unscored_alternatives += unscored.values()
    for opts in out.values():
        opts.sort(key=lambda o: (o[0].sched_dep, o[-1].sched_arr))
    rep.label_flags = {label: frozenset(config.ALT_FLAG_PREFIX + n for n in sorted(names))
                       for label, names in alt_names.items()}
    result = RouterResult(out, rep.label_flags, rep)
    cache[key] = result
    return result


def option_train(o: Option) -> str:
    """Train number (or PATH line) of the first ride."""
    first = next((leg for leg in o if leg.mode is not LegMode.WALK), o[0])
    return first.train_number or first.gtfs_line or "?"


def describe_option(o: Option) -> str:
    """Rides of an option, local times: 'rail 1150 TS 07:20→HB 07:32 | path ...'."""
    def hm(t: datetime) -> str:
        return t.astimezone(config.LOCAL_TZ).strftime("%H:%M")
    return " | ".join(f"{leg.mode.value} {leg.train_number or leg.gtfs_line} {leg.from_stop} "
                      f"{hm(leg.sched_dep)}→{leg.to_stop} {hm(leg.sched_arr)}"
                      for leg in o if leg.mode is not LegMode.WALK)


def router_options(network: Network, service_date: date = config.DEMO_SERVICE_DATE,
                   deadline_s: int = config.DEMO_DEADLINE_S,
                   stations: tuple[str, ...] = config.ROUTER_ACCESS_STATIONS,
                   report: AdapterReport | None = None,
                   exclusions: Mapping[str, Exclusion] | None = None,
                   base: Exclusion | None = None) -> dict[str, list[Option]]:
    """Router Options per signature label (same type as station_options); see route_all.

    A passed report is filled from a fresh run.
    """
    res = route_all(network, service_date, deadline_s, stations, exclusions, fresh=report is not None, base=base)
    if report is not None:
        report.__dict__.update(res.report.__dict__)
    return res.options
