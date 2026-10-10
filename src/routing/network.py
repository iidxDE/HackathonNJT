"""Network-agnostic RAPTOR network: GTFS feeds in, numpy arrays out.

Nothing operator-specific lives here. A feed is described by a FeedSpec
(namespace prefix, directory, which stops.txt column names a stop); the
caller adds operator rules and asserts on top.

Stops are namespaced "<prefix>:<key>". Platforms collapse to their station:
parent_station when the feed sets it, otherwise stops sharing the key column
(e.g. a feed that lists one stop per direction under the same stop_name).

Times are int32 seconds after service-day midnight (≥ 86,400 allowed).
Patterns are identical stop sequences, split until FIFO at every stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src import gtfs

# GTFS pickup_type / drop_off_type: 0 or empty = allowed, 1 = not available,
# 2/3 = phone / coordinate with driver (treated as allowed).
_NOT_AVAILABLE = "1"


@dataclass(frozen=True)
class FeedSpec:
    prefix: str  # namespace, e.g. "NJT"
    feed_dir: Path
    stop_key: str  # stops.txt column that names a station after the parent collapse


@dataclass(frozen=True)
class FeedTimetable:
    """One feed's active stop_times on a service date, stops collapsed and namespaced."""
    prefix: str
    stop_times: pd.DataFrame  # trip, stop, seq, arr_s, dep_s, board_ok, alight_ok
    trips: pd.DataFrame  # trip, route_id, label, service_id
    stop_map: pd.DataFrame  # raw stop_id → stop (namespaced station key)
    n_active_services: int


def collapse_stops(stops: pd.DataFrame, prefix: str, stop_key: str) -> pd.Series:
    """raw stop_id → namespaced station id.

    A stop with a parent_station takes the parent's key; otherwise its own
    stop_key value. Stops with an empty key fall back to their stop_id.
    """
    s = stops.set_index("stop_id")
    key = s[stop_key].where(s[stop_key] != "", s.index.to_series())
    if "parent_station" in s:
        parent = s["parent_station"]
        has_parent = (parent != "") & parent.isin(key.index)
        key = key.where(~has_parent, key.reindex(parent).to_numpy())
    return prefix + ":" + key.astype(str)


def strip_label(label: pd.Series) -> pd.Series:
    """Trip labels (train numbers) are strings with leading zeros stripped."""
    out = label.str.strip().str.lstrip("0")
    return out.where(out != "", label.str.strip())


def load_timetable(spec: FeedSpec, service_date: date) -> FeedTimetable:
    """Active trips of one feed on service_date as namespaced stop_times.

    Trips without a trip_short_name get a synthetic label
    "<route_short_name or route_id>@HH:MM" (first departure), suffixed on clash.
    """
    feed = gtfs.load_feed(spec.feed_dir)
    services = gtfs.active_service_ids(feed, service_date)
    trips = feed.trips[feed.trips["service_id"].isin(services)].copy()
    stop_map = collapse_stops(feed.stops, spec.prefix, spec.stop_key)

    st = feed.stop_times[feed.stop_times["trip_id"].isin(trips["trip_id"])]
    if st[["arrival_s", "departure_s"]].isna().any(axis=None):
        raise ValueError(f"{spec.prefix}: stop_times with missing times (untimed stops unsupported)")
    pickup = st.get("pickup_type", pd.Series("0", index=st.index))
    drop = st.get("drop_off_type", pd.Series("0", index=st.index))
    out = pd.DataFrame({
        "trip": spec.prefix + ":" + st["trip_id"],
        "stop": st["stop_id"].map(stop_map),
        "seq": st["stop_sequence"].astype(int),
        "arr_s": st["arrival_s"].astype(np.int64),
        "dep_s": st["departure_s"].astype(np.int64),
        "board_ok": pickup.to_numpy() != _NOT_AVAILABLE,
        "alight_ok": drop.to_numpy() != _NOT_AVAILABLE,
    })
    if out["stop"].isna().any():
        raise ValueError(f"{spec.prefix}: stop_times reference stops missing from stops.txt")
    out = out.sort_values(["trip", "seq"], kind="stable").reset_index(drop=True)

    first_dep = out.groupby("trip")["dep_s"].first()
    trips["trip"] = spec.prefix + ":" + trips["trip_id"]
    short = trips.get("trip_short_name", pd.Series("", index=trips.index))
    rname = trips["route_id"].map(feed.routes.set_index("route_id").get(
        "route_short_name", pd.Series(dtype=str))).fillna("")
    rname = rname.where(rname != "", trips["route_id"])
    dep = trips["trip"].map(first_dep)
    synthetic = (rname + "@" + (dep // 3600).astype(int).map("{:02d}".format)
                 + ":" + (dep % 3600 // 60).astype(int).map("{:02d}".format))
    has_short = short.str.strip() != ""
    label = strip_label(short).where(has_short, synthetic)
    dup = label.groupby(label).cumcount()
    label = label.where((dup == 0) | has_short, label + "#" + dup.astype(str))
    trips_out = pd.DataFrame({"trip": trips["trip"], "route_id": trips["route_id"].astype(str),
                              "label": label, "synthetic_label": ~has_short,
                              "service_id": trips["service_id"]}).reset_index(drop=True)
    sm = stop_map.rename("stop").rename_axis("stop_id").reset_index()
    return FeedTimetable(spec.prefix, out, trips_out, sm, len(services))


@dataclass
class Network:
    """RAPTOR arrays. Stops, patterns and trips are indexed by position.

    Pattern p: stops pattern_stops[ps_ptr[p]:ps_ptr[p+1]]; trips
    trip_ids[pt_ptr[p]:pt_ptr[p+1]] sorted by first departure. Trip t's times
    at the pattern's stops are arr/dep[tt_ptr[t]:tt_ptr[t+1]] (same order as
    the pattern stops); board_ok / alight_ok are aligned with arr/dep.
    Stop s is served by (sp_pattern, sp_pos)[sp_ptr[s]:sp_ptr[s+1]].
    """
    service_date: str  # YYYYMMDD
    stop_ids: np.ndarray  # str
    pattern_stops: np.ndarray
    ps_ptr: np.ndarray
    pt_ptr: np.ndarray
    pattern_route: np.ndarray  # str, namespaced route id
    trip_ids: np.ndarray  # str, namespaced
    trip_labels: np.ndarray  # str (train number or synthetic)
    tt_ptr: np.ndarray
    arr: np.ndarray  # int32 seconds
    dep: np.ndarray
    board_ok: np.ndarray  # bool
    alight_ok: np.ndarray
    sp_ptr: np.ndarray
    sp_pattern: np.ndarray
    sp_pos: np.ndarray
    tr_from: np.ndarray  # transfers (footpaths), sorted by tr_from
    tr_to: np.ndarray
    tr_min_s: np.ndarray
    same_stop_transfer_s: int
    stats: dict = field(default_factory=dict)

    _ARRAYS = ("stop_ids", "pattern_stops", "ps_ptr", "pt_ptr", "pattern_route", "trip_ids",
               "trip_labels", "tt_ptr", "arr", "dep", "board_ok", "alight_ok", "sp_ptr",
               "sp_pattern", "sp_pos", "tr_from", "tr_to", "tr_min_s")

    @property
    def n_stops(self) -> int:
        return len(self.stop_ids)

    @property
    def n_patterns(self) -> int:
        return len(self.ps_ptr) - 1

    def stop_index(self) -> dict[str, int]:
        return {s: i for i, s in enumerate(self.stop_ids)}

    def pattern_times(self, p: int, which: str = "dep") -> np.ndarray:
        """trips × stops matrix of arr or dep for pattern p."""
        a = self.arr if which == "arr" else self.dep
        lo, hi = self.pt_ptr[p], self.pt_ptr[p + 1]
        n = self.ps_ptr[p + 1] - self.ps_ptr[p]
        return a[self.tt_ptr[lo]:self.tt_ptr[hi]].reshape(hi - lo, n)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, **{k: getattr(self, k) for k in self._ARRAYS},
                            service_date=np.array(self.service_date),
                            same_stop_transfer_s=np.array(self.same_stop_transfer_s))

    @classmethod
    def load(cls, path: Path) -> "Network":
        with np.load(path, allow_pickle=False) as z:
            kw = {k: z[k] for k in cls._ARRAYS}
            return cls(service_date=str(z["service_date"]),
                       same_stop_transfer_s=int(z["same_stop_transfer_s"]), **kw)


def _split_fifo(dep: np.ndarray, arr: np.ndarray) -> list[list[int]]:
    """Split trips (rows, already sorted by first departure) into FIFO groups.

    Greedy: a trip joins the first group whose last trip it does not overtake
    (dep and arr ≥ that trip's at every stop); else it opens a new group.
    """
    groups: list[list[int]] = []
    for i in range(len(dep)):
        for g in groups:
            j = g[-1]
            if (dep[i] >= dep[j]).all() and (arr[i] >= arr[j]).all():
                g.append(i)
                break
        else:
            groups.append([i])
    return groups


def is_fifo(dep: np.ndarray, arr: np.ndarray) -> bool:
    """No trip overtakes its predecessor at any stop (rows sorted by trip order)."""
    return bool((np.diff(dep, axis=0) >= 0).all() and (np.diff(arr, axis=0) >= 0).all())


@dataclass(frozen=True)
class Transfers:
    """Footpaths between distinct stops (directional) + one same-stop minimum."""
    edges: pd.DataFrame  # from_stop, to_stop, min_transfer_s
    same_stop_s: int


def load_transfers(csv: Path) -> Transfers:
    """transfers.csv: from_stop,to_stop,min_transfer_s,source,note.

    from_stop = to_stop = "*" is the same-stop minimum (exactly one row).
    Rows are directional; list both directions for a two-way link.
    """
    t = pd.read_csv(csv, dtype=str, keep_default_na=False, comment="#")
    t["min_transfer_s"] = t["min_transfer_s"].astype(int)
    same = t[(t["from_stop"] == "*") & (t["to_stop"] == "*")]
    if len(same) != 1:
        raise ValueError(f"{csv}: need exactly one same-stop row (*,*), found {len(same)}")
    edges = t.drop(same.index)[["from_stop", "to_stop", "min_transfer_s"]].reset_index(drop=True)
    if edges.duplicated(["from_stop", "to_stop"]).any():
        raise ValueError(f"{csv}: duplicate transfer rows")
    return Transfers(edges, int(same["min_transfer_s"].iloc[0]))


def build_network(timetables: list[FeedTimetable], transfers: Transfers, service_date: date,
                  virtual_prefixes: tuple[str, ...] = ("DEST:",)) -> Network:
    """Merge feeds into one Network.

    Transfer endpoints must be timetable stops, or virtual stops whose id
    starts with one of virtual_prefixes (e.g. a destination with several
    station entrances).
    """
    st = pd.concat([t.stop_times for t in timetables], ignore_index=True)
    trips = pd.concat([t.trips for t in timetables], ignore_index=True).set_index("trip")
    real_stops = set(st["stop"])
    ends = set(transfers.edges["from_stop"]) | set(transfers.edges["to_stop"])
    missing = sorted(s for s in ends - real_stops if not s.startswith(virtual_prefixes))
    if missing:
        raise ValueError(f"transfer stops not in the timetable: {missing}")
    stop_ids = np.array(sorted(real_stops | ends))
    sidx = {s: i for i, s in enumerate(stop_ids)}
    st["s"] = st["stop"].map(sidx).astype(np.int32)

    # Pattern key = (route, stop sequence). Route kept so a pattern has one route.
    seqs = st.groupby("trip", sort=False)["s"].agg(tuple)
    first_dep = st.groupby("trip", sort=False)["dep_s"].first()
    tinfo = pd.DataFrame({"seq": seqs, "first_dep": first_dep})
    tinfo["route"] = trips.loc[tinfo.index, "route_id"].to_numpy()
    tinfo["feed"] = [t.split(":", 1)[0] for t in tinfo.index]
    tinfo["route"] = tinfo["feed"] + ":" + tinfo["route"]
    by_trip = {k: g for k, g in st.groupby("trip", sort=False)}

    pattern_stops, ps_ptr, pt_ptr, proute = [], [0], [0], []
    trip_ids, labels, tt_ptr = [], [], [0]
    arr_l, dep_l, bo_l, al_l = [], [], [], []
    n_split = 0
    split_by_feed: dict[str, int] = {}
    base_by_feed: dict[str, int] = {}
    for (route, seq), g in tinfo.groupby(["route", "seq"], sort=True):
        g = g.sort_values("first_dep", kind="stable")
        rows = [by_trip[t] for t in g.index]
        dep = np.stack([r["dep_s"].to_numpy() for r in rows])
        arr = np.stack([r["arr_s"].to_numpy() for r in rows])
        groups = _split_fifo(dep, arr)
        feed = route.split(":", 1)[0]
        base_by_feed[feed] = base_by_feed.get(feed, 0) + 1
        split_by_feed[feed] = split_by_feed.get(feed, 0) + len(groups) - 1
        n_split += len(groups) - 1
        for grp in groups:
            pattern_stops.extend(seq)
            ps_ptr.append(len(pattern_stops))
            proute.append(route)
            for i in grp:
                r = rows[i]
                trip_ids.append(g.index[i])
                labels.append(trips.at[g.index[i], "label"])
                arr_l.append(r["arr_s"].to_numpy())
                dep_l.append(r["dep_s"].to_numpy())
                bo_l.append(r["board_ok"].to_numpy())
                al_l.append(r["alight_ok"].to_numpy())
                tt_ptr.append(tt_ptr[-1] + len(seq))
            pt_ptr.append(len(trip_ids))

    pattern_stops_a = np.array(pattern_stops, dtype=np.int32)
    ps_ptr_a = np.array(ps_ptr, dtype=np.int64)
    # Stop → (pattern, position) index.
    n_pat = len(ps_ptr_a) - 1
    pat_of = np.repeat(np.arange(n_pat, dtype=np.int32), np.diff(ps_ptr_a))
    pos_of = np.arange(len(pattern_stops_a)) - np.repeat(ps_ptr_a[:-1], np.diff(ps_ptr_a))
    order = np.lexsort((pos_of, pat_of, pattern_stops_a))
    counts = np.bincount(pattern_stops_a, minlength=len(stop_ids))
    sp_ptr = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)

    e = transfers.edges.assign(f=transfers.edges["from_stop"].map(sidx),
                               t=transfers.edges["to_stop"].map(sidx)).sort_values(["f", "t"])
    net = Network(
        service_date=service_date.strftime("%Y%m%d"),
        stop_ids=stop_ids,
        pattern_stops=pattern_stops_a, ps_ptr=ps_ptr_a,
        pt_ptr=np.array(pt_ptr, dtype=np.int64),
        pattern_route=np.array(proute),
        trip_ids=np.array(trip_ids), trip_labels=np.array(labels),
        tt_ptr=np.array(tt_ptr, dtype=np.int64),
        arr=np.concatenate(arr_l).astype(np.int32), dep=np.concatenate(dep_l).astype(np.int32),
        board_ok=np.concatenate(bo_l), alight_ok=np.concatenate(al_l),
        sp_ptr=sp_ptr, sp_pattern=pat_of[order], sp_pos=pos_of[order].astype(np.int32),
        tr_from=e["f"].to_numpy(np.int32), tr_to=e["t"].to_numpy(np.int32),
        tr_min_s=e["min_transfer_s"].to_numpy(np.int32),
        same_stop_transfer_s=transfers.same_stop_s,
    )
    net.stats = {"patterns_before_split": base_by_feed, "fifo_splits": split_by_feed,
                 "n_patterns": n_pat, "n_trips": len(trip_ids), "n_splits": n_split}
    return net


def assert_fifo(net: Network) -> list[int]:
    """Pattern indices that are not FIFO (should be empty)."""
    return [p for p in range(net.n_patterns)
            if not is_fifo(net.pattern_times(p, "dep"), net.pattern_times(p, "arr"))]
