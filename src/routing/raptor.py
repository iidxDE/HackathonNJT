"""Range RAPTOR (rRAPTOR) over a Network: one access stop → one target, a profile.

Network-agnostic: stops, trips and footpaths come from src/routing/network.py;
the target and the feed → mode names are parameters.

Criteria: departure from the access stop (later is better), arrival at the
target, number of rides. Departure iterations run latest → earliest and the
round labels τ[k][stop] persist across iterations, so a journey is emitted only
when it improves the profile. Cost and reliability are never criteria here.

Pruning keeps one best arrival per stop *per ride budget* (best over rounds
≤ k) instead of a single global best, so a journey with fewer rides that
departs earlier and arrives at the same time is not lost.

Times are seconds after service-day midnight.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping

import numpy as np

from src import config
from src.routing.exclusion import Exclusion
from src.routing.network import Network

log = logging.getLogger(__name__)

INF = 1 << 40


@dataclass(frozen=True)
class JourneyLeg:
    mode: str  # e.g. "rail" | "path" | "walk"
    board_stop: str
    alight_stop: str
    dep: int  # seconds after service-day midnight
    arr: int
    trip_id: str | None = None  # namespaced; None for walks
    train_no: str | None = None  # trip label (train number or synthetic)
    route_id: str | None = None  # namespaced pattern route


@dataclass(frozen=True)
class Journey:
    access_stop: str
    dep_time: int  # leaving the access stop (first leg's departure)
    arr_time: int  # at the target
    rides: int  # non-walk legs
    legs: tuple[JourneyLeg, ...]


@dataclass
class ProfileStats:
    iterations: int = 0
    rounds: int = 0
    patterns_scanned: int = 0
    labels_touched: int = 0
    emitted: int = 0
    dropped_rides: int = 0  # journeys over max_rides_out (logged, not returned)
    runtime_s: float = 0.0


@dataclass
class _Prepared:
    """Python-native views of a Network for the scan loop (built once per network)."""
    p_stops: list[list[int]]
    p_dep_cols: list[list[np.ndarray]]  # [pattern][pos] → dep of each trip (FIFO, sorted)
    p_arr: list[list[list[int]]]  # [pattern][trip][pos]
    p_dep: list[list[list[int]]]
    p_board: list[list[list[bool]]]
    p_alight: list[list[list[bool]]]
    p_trip0: list[int]  # global index of the pattern's first trip
    stop_routes: list[list[tuple[int, int]]]  # stop → [(pattern, pos)]
    foot: list[list[tuple[int, int]]]  # stop → [(to, walk_s)]
    sidx: dict[str, int]
    feed_of_pattern: list[str]


def _prepare(net: Network) -> _Prepared:
    cached = net.__dict__.get("_raptor_prep")
    if cached is not None:
        return cached
    p_stops, p_dep_cols, p_arr, p_dep, p_board, p_alight, p_trip0, feed = ([] for _ in range(8))
    for p in range(net.n_patterns):
        stops = net.pattern_stops[net.ps_ptr[p]:net.ps_ptr[p + 1]].tolist()
        lo, hi = int(net.pt_ptr[p]), int(net.pt_ptr[p + 1])
        n = len(stops)
        sl = slice(int(net.tt_ptr[lo]), int(net.tt_ptr[hi]))
        dep = net.dep[sl].reshape(hi - lo, n)
        p_stops.append(stops)
        p_dep_cols.append([np.ascontiguousarray(dep[:, i]) for i in range(n)])
        p_arr.append(net.arr[sl].reshape(hi - lo, n).tolist())
        p_dep.append(dep.tolist())
        p_board.append(net.board_ok[sl].reshape(hi - lo, n).tolist())
        p_alight.append(net.alight_ok[sl].reshape(hi - lo, n).tolist())
        p_trip0.append(lo)
        feed.append(str(net.pattern_route[p]).split(":", 1)[0])
    stop_routes = [list(zip(net.sp_pattern[net.sp_ptr[s]:net.sp_ptr[s + 1]].tolist(),
                            net.sp_pos[net.sp_ptr[s]:net.sp_ptr[s + 1]].tolist()))
                   for s in range(net.n_stops)]
    foot: list[list[tuple[int, int]]] = [[] for _ in range(net.n_stops)]
    for f, t, w in zip(net.tr_from.tolist(), net.tr_to.tolist(), net.tr_min_s.tolist()):
        foot[f].append((t, w))
    prep = _Prepared(p_stops, p_dep_cols, p_arr, p_dep, p_board, p_alight, p_trip0,
                     stop_routes, foot, net.stop_index(), feed)
    net.__dict__["_raptor_prep"] = prep
    return prep


@dataclass(frozen=True)
class _Banned:
    """An Exclusion resolved to network indices (empty sets = no exclusion)."""
    no_board: frozenset[int] = frozenset()
    no_alight: frozenset[int] = frozenset()
    trips: frozenset[int] = frozenset()  # global trip indices
    patterns: frozenset[int] = frozenset()  # patterns of banned routes
    foot: frozenset[tuple[int, int]] = frozenset()


def _resolve(net: Network, pr: _Prepared, ex: Exclusion | None) -> _Banned:
    """Exclusion ids → indices. Ids not in the network are ignored with a warning."""
    if not ex:
        return _Banned()

    def stops(ids, what):
        unknown = sorted(i for i in ids if i not in pr.sidx)
        if unknown:
            log.warning("exclusion %s: stops not in the network ignored: %s", what, unknown)
        return frozenset(pr.sidx[i] for i in ids if i in pr.sidx)

    routes = [str(r) for r in net.pattern_route.tolist()]
    unknown_routes = sorted(set(ex.banned_routes) - set(routes))
    if unknown_routes:
        log.warning("exclusion banned_routes: routes not in the network ignored: %s", unknown_routes)
    bad_trips = sorted(t for t in ex.banned_trips if not 0 <= t < len(net.trip_ids))
    if bad_trips:
        log.warning("exclusion banned_trips: trip indices out of range ignored: %s", bad_trips)
    foot = frozenset((pr.sidx[a], pr.sidx[b]) for a, b in ex.banned_footpaths
                     if a in pr.sidx and b in pr.sidx)
    if len(foot) != len(ex.banned_footpaths):
        log.warning("exclusion banned_footpaths: pairs with unknown stops ignored")
    return _Banned(stops(ex.no_board, "no_board"), stops(ex.no_alight, "no_alight"),
                   frozenset(ex.banned_trips) - frozenset(bad_trips),
                   frozenset(p for p, r in enumerate(routes) if r in ex.banned_routes), foot)


def route_profile(network: Network, access_stop: str, service_date: date, deadline: int, *,
                  target: str = config.ROUTER_DEST_STOP,
                  max_rounds: int = config.ROUTER_MAX_ROUNDS,
                  max_rides_out: int = config.ROUTER_MAX_RIDES_OUT,
                  window_before_s: int = config.ROUTER_WINDOW_BEFORE_S,
                  arrive_after_s: int = config.ROUTER_ARRIVE_AFTER_S,
                  depart_after_s: int = config.ROUTER_DEPART_AFTER_S,
                  mode_by_feed: Mapping[str, str] = config.ROUTER_MODE_BY_FEED,
                  exclusion: Exclusion | None = None,
                  stats: ProfileStats | None = None) -> list[Journey]:
    """Pareto profile of journeys access_stop → target.

    Departures from access_stop (or from a stop one footpath away, counted from
    the access stop) in [deadline − window_before_s, deadline + depart_after_s]; journeys
    arriving at target ≤ deadline + arrive_after_s. Returns journeys with
    rides ≤ max_rides_out, sorted by departure; longer ones are counted in
    stats.dropped_rides and logged. Same-stop transfers use the network's
    same-stop minimum; a stop reached on foot needs no extra minimum.

    exclusion: stops where no ride may start / end, trips and routes that may
    not be ridden, footpaths that may not be walked (set lookups in the scan;
    the network is not changed). None (or an empty Exclusion) is the plain run.
    """
    t_start = time.perf_counter()
    st = stats if stats is not None else ProfileStats()
    if network.service_date != service_date.strftime("%Y%m%d"):
        raise ValueError(f"network is for {network.service_date}, asked {service_date}")
    pr = _prepare(network)
    ban = _resolve(network, pr, exclusion)
    src, tgt = pr.sidx[access_stop], pr.sidx[target]
    n, K = network.n_stops, max_rounds
    xfer = int(network.same_stop_transfer_s)
    t_lo, t_hi = deadline - window_before_s, deadline + depart_after_s
    limit = deadline + arrive_after_s + 1  # strict upper bound on any useful arrival

    # Departure times (from the access stop) within the window, latest first.
    deps: set[int] = set()
    starts = [(src, 0)] + [(to, w) for to, w in pr.foot[src] if (src, to) not in ban.foot]
    for s, walk in starts:
        if s in ban.no_board:
            continue
        for p, i in pr.stop_routes[s]:
            if p in ban.patterns:
                continue
            for row, (row_dep, row_ok) in enumerate(zip(pr.p_dep[p], pr.p_board[p])):
                t = row_dep[i] - walk
                if row_ok[i] and t_lo <= t <= t_hi and pr.p_trip0[p] + row not in ban.trips:
                    deps.add(t)

    tau = [[INF] * n for _ in range(K + 1)]
    # parent[k][s]: ("ride", p, trip_row, board_pos, alight_pos) | ("walk", from, walk_s) | ("start",)
    parent: list[list[tuple | None]] = [[None] * n for _ in range(K + 1)]
    walked = [[False] * n for _ in range(K + 1)]  # label reached on foot (no extra transfer)

    def best_le(k: int, s: int) -> int:
        b = tau[1][s] if k >= 1 else INF
        for j in range(2, k + 1):
            if tau[j][s] < b:
                b = tau[j][s]
        return b

    out: list[Journey] = []
    for t in sorted(deps, reverse=True):
        st.iterations += 1
        improved_dest: set[int] = set()
        tau[0][src] = t
        parent[0][src] = ("start",)
        walked[0][src] = False
        marked = {src}
        for to, w in pr.foot[src]:
            if (src, to) in ban.foot:
                continue
            if t + w < tau[0][to]:
                tau[0][to] = t + w
                parent[0][to] = ("walk", src, w)
                walked[0][to] = True
                marked.add(to)
        for k in range(1, K + 1):
            st.rounds += 1
            queue: dict[int, int] = {}
            for s in marked:
                for p, i in pr.stop_routes[s]:
                    if p in ban.patterns:
                        continue
                    if queue.get(p, 1 << 30) > i:
                        queue[p] = i
            new_marked: set[int] = set()
            prev, cur = tau[k - 1], tau[k]
            for p, i0 in queue.items():
                st.patterns_scanned += 1
                stops = pr.p_stops[p]
                arr_rows, dep_rows = pr.p_arr[p], pr.p_dep[p]
                board_rows, alight_rows = pr.p_board[p], pr.p_alight[p]
                trip = -1
                board_i = -1
                t0 = pr.p_trip0[p]
                for i in range(i0, len(stops)):
                    s = stops[i]
                    if trip >= 0 and alight_rows[trip][i] and s not in ban.no_alight:
                        a = arr_rows[trip][i]
                        bound = min(best_le(k, s), best_le(k, tgt), limit)
                        if a < bound:
                            cur[s] = a
                            parent[k][s] = ("ride", p, trip, board_i, i)
                            walked[k][s] = False
                            new_marked.add(s)
                            st.labels_touched += 1
                    if prev[s] >= INF or s in ban.no_board:
                        continue
                    ready = prev[s] + (0 if (k == 1 or walked[k - 1][s]) else xfer)
                    if trip >= 0 and ready > dep_rows[trip][i]:
                        continue
                    col = pr.p_dep_cols[p][i]
                    j = int(np.searchsorted(col, ready, side="left"))
                    while j < len(col) and (not board_rows[j][i] or t0 + j in ban.trips):
                        j += 1
                    if j < len(col) and (trip < 0 or j < trip):
                        trip, board_i = j, i
            # Footpaths from stops reached by a ride this round (no chained walks).
            for s in list(new_marked):
                for to, w in pr.foot[s]:
                    if (s, to) in ban.foot:
                        continue
                    a = cur[s] + w
                    if a < min(best_le(k, to), best_le(k, tgt), limit):
                        cur[to] = a
                        parent[k][to] = ("walk", s, w)
                        walked[k][to] = True
                        new_marked.add(to)
                        st.labels_touched += 1
            if tgt in new_marked:
                improved_dest.add(k)
            marked = new_marked
            if not marked:
                break
        for k in sorted(improved_dest):
            j = _reconstruct(network, pr, parent, k, tgt, access_stop, mode_by_feed)
            st.emitted += 1
            if j.rides > max_rides_out:
                st.dropped_rides += 1
                log.info("dropped %d-ride journey %s dep %s", j.rides, access_stop, _hhmm(j.dep_time))
            else:
                out.append(j)
    st.runtime_s = time.perf_counter() - t_start
    log.info("%s: %d iterations, %d rounds, %d patterns scanned, %d labels touched, "
             "%d journeys (%d dropped > %d rides), %.3f s", access_stop, st.iterations, st.rounds,
             st.patterns_scanned, st.labels_touched, len(out), st.dropped_rides, max_rides_out,
             st.runtime_s)
    return sorted(out, key=lambda j: (j.dep_time, j.arr_time, j.rides))


def _reconstruct(net: Network, pr: _Prepared, parent, k: int, tgt: int, access_stop: str,
                 mode_by_feed: Mapping[str, str]) -> Journey:
    """Walk the parent chain back from τ[k][tgt]; fill walk times forward."""
    raw: list[tuple] = []
    s, kk = tgt, k
    while True:
        par = parent[kk][s]
        if par is None or par[0] == "start":
            break
        if par[0] == "walk":
            raw.append(("walk", par[1], s, par[2]))
            s = par[1]
        else:
            _, p, trip, bi, ai = par
            stops = pr.p_stops[p]
            raw.append(("ride", p, trip, bi, ai))
            s = stops[bi]
            kk -= 1
    raw.reverse()
    legs: list[JourneyLeg] = []
    for idx, r in enumerate(raw):
        if r[0] == "ride":
            _, p, trip, bi, ai = r
            g = pr.p_trip0[p] + trip
            legs.append(JourneyLeg(
                mode=mode_by_feed.get(pr.feed_of_pattern[p], pr.feed_of_pattern[p].lower()),
                board_stop=str(net.stop_ids[pr.p_stops[p][bi]]),
                alight_stop=str(net.stop_ids[pr.p_stops[p][ai]]),
                dep=int(pr.p_dep[p][trip][bi]), arr=int(pr.p_arr[p][trip][ai]),
                trip_id=str(net.trip_ids[g]), train_no=str(net.trip_labels[g]),
                route_id=str(net.pattern_route[p])))
        else:
            _, f, to, w = r
            if idx == 0:  # access walk: leave just in time for the next ride
                nxt = raw[1] if len(raw) > 1 else None
                end = int(pr.p_dep[nxt[1]][nxt[2]][nxt[3]]) if nxt and nxt[0] == "ride" else w
                dep = end - w
            else:
                dep = legs[-1].arr
            legs.append(JourneyLeg("walk", str(net.stop_ids[f]), str(net.stop_ids[to]), dep, dep + w))
    rides = sum(1 for leg in legs if leg.mode != "walk")
    return Journey(access_stop, legs[0].dep, legs[-1].arr, rides, tuple(legs))


def _hhmm(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


def describe(j: Journey) -> str:
    """One-line human-readable journey."""
    parts = []
    for leg in j.legs:
        if leg.mode == "walk":
            parts.append(f"walk {leg.board_stop}→{leg.alight_stop} {(leg.arr - leg.dep) // 60}m")
        else:
            parts.append(f"{leg.train_no} {leg.board_stop} {_hhmm(leg.dep)}→{leg.alight_stop} {_hhmm(leg.arr)}")
    return f"{_hhmm(j.dep_time)}→{_hhmm(j.arr_time)} r{j.rides}: " + " | ".join(parts)
