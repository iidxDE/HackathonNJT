"""Phase 2.1 network build: NJT rail + PATH GTFS → cached RAPTOR arrays + report.

    python -m src.build.network [--date 2026-10-13]

Operator-specific choices (feed prefixes, stop key columns, the Portal
cutover check, scope stations) live here; src/routing/network.py stays
network-agnostic. Any failed assert aborts the build.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from datetime import date

import numpy as np
import pandas as pd

from src import config
from src.routing.network import (FeedSpec, Network, assert_fifo, build_network,
                                 load_timetable, load_transfers)

log = logging.getLogger(__name__)

NJT = FeedSpec("NJT", config.GTFS_NJT_RAIL_DIR, stop_key="stop_code")
# PATH has no parent_station: one stop per direction share a stop_name.
PATH = FeedSpec("PATH", config.GTFS_PATH_DIR, stop_key="stop_name")


class BuildError(AssertionError):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise BuildError(msg)


def _stamp() -> str:
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           cwd=config.REPO_ROOT, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, cwd=config.REPO_ROOT).stdout.strip()
        return h + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build(service_date: date = config.DEMO_SERVICE_DATE) -> tuple[Network, str]:
    """Build, assert and save the network; return it and the markdown report."""
    njt = load_timetable(NJT, service_date)
    path = load_timetable(PATH, service_date)
    for tt in (njt, path):
        log.info("%s: %d active services, %d trips, %d stop_times, %d midnight wraps fixed",
                 tt.prefix, tt.n_active_services, len(tt.trips), len(tt.stop_times),
                 tt.n_wrapped_trips)
        _check(len(tt.trips) >= 1, f"{tt.prefix}: no active trip on {service_date}")

    # Scope stations: NJT stop_code → station, mapped once.
    scope = pd.read_csv(config.STATIONS_SCOPE_CSV, dtype=str, keep_default_na=False)
    njt_stops = set(njt.stop_map["stop"])
    missing_scope = [c for c in scope["station_code"] if f"NJT:{c}" not in njt_stops]
    _check(not missing_scope, f"scope stations without an NJT stop: {missing_scope}")
    _check(njt.stop_map.groupby("stop")["stop_id"].nunique().max() == 1,
           "NJT stop_code is not unique per stop_id")

    # Portal cutover: no M&E / Gladstone / Montclair-Boonton trip serves NY Penn.
    njt_st = njt.stop_times.merge(njt.trips[["trip", "route_id", "label"]], on="trip")
    bad = njt_st[(njt_st["stop"] == "NJT:NY")
                 & njt_st["route_id"].isin(config.CUTOVER_HOBOKEN_ROUTE_IDS)]
    _check(bad.empty, f"cutover check: {bad['trip'].nunique()} M&E/Gladstone/MOBO trips serve NY "
                      f"on {service_date}: {sorted(bad['label'].unique())[:10]}")

    transfers = load_transfers(config.TRANSFERS_CSV)
    # transfers.csv must agree with the scorer's constants until the scorer reads it (2.4).
    e = transfers.edges.set_index(["from_stop", "to_stop"])["min_transfer_s"]
    _check(e.get(("NJT:HB", "PATH:Hoboken")) == config.TRANSFER_MIN_S["hoboken_njt_to_path"],
           "transfers.csv HB → PATH differs from config.TRANSFER_MIN_S")
    _check(e.get(("PATH:33rd Street", config.ROUTER_DEST_STOP))
           == config.TRANSFER_MIN_S["path_33st_to_penn_walk"],
           "transfers.csv 33rd St → Penn differs from config.TRANSFER_MIN_S")
    _check(transfers.same_stop_s == config.TRANSFER_MIN_S["hoboken_njt_to_path"],
           "same-stop transfer ≠ scorer transfer buffer")

    net = build_network([njt, path], transfers, service_date, config.ROUTER_VIRTUAL_PREFIXES)
    not_fifo = assert_fifo(net)
    _check(not not_fifo, f"{len(not_fifo)} patterns not FIFO after splitting")

    # Destination reachable from both systems.
    sidx = net.stop_index()
    for s in ("NJT:NY", "PATH:33rd Street"):
        i = sidx[s]
        _check(net.sp_ptr[i + 1] > net.sp_ptr[i], f"{s} is served by no pattern")
    _check(config.ROUTER_DEST_STOP in sidx, "virtual destination missing")

    out = config.network_npz(service_date)
    net.save(out)
    log.info("saved %s (%d stops, %d patterns, %d trips)", out, net.n_stops, net.n_patterns,
             len(net.trip_ids))
    return net, report(net, njt, path, transfers, service_date)


def _restricted(tt) -> pd.DataFrame:
    st = tt.stop_times
    r = st[~st["board_ok"] | ~st["alight_ok"]]
    if r.empty:
        return pd.DataFrame(columns=["stop", "no_pickup", "no_drop_off", "trips"])
    return (r.assign(no_pickup=~r["board_ok"], no_drop_off=~r["alight_ok"])
             .groupby("stop").agg(no_pickup=("no_pickup", "sum"),
                                  no_drop_off=("no_drop_off", "sum"),
                                  trips=("trip", "nunique")).reset_index())


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def report(net: Network, njt, path, transfers, service_date: date) -> str:
    feeds = []
    for tt in (njt, path):
        st = tt.stop_times
        feeds.append({
            "feed": tt.prefix, "active services": tt.n_active_services, "trips": len(tt.trips),
            "stops used": st["stop"].nunique(), "stop_times": len(st),
            "patterns (before split)": net.stats["patterns_before_split"].get(tt.prefix, 0),
            "FIFO splits": net.stats["fifo_splits"].get(tt.prefix, 0),
            "patterns": int(sum(r.startswith(tt.prefix + ":") for r in net.pattern_route)),
            "midnight wraps fixed": tt.n_wrapped_trips,
            "trips past 24:00": int((st.groupby("trip")["arr_s"].max() >= config.SECONDS_PER_DAY).sum()),
            "max time": f"{int(st['arr_s'].max()) // 3600:02d}:{int(st['arr_s'].max()) % 3600 // 60:02d}",
        })
    used = set(path.stop_times["stop"])
    pm = path.stop_map[path.stop_map["stop"].isin(used)]
    path_ids = (pm.groupby("stop")["stop_id"].agg(lambda s: ", ".join(sorted(s)))
                  .reset_index().rename(columns={"stop_id": "raw stop_ids (collapsed)"}))
    no_name = njt.trips[njt.trips["synthetic_label"]]
    tr = transfers.edges.assign(min=transfers.edges["min_transfer_s"] // 60)[
        ["from_stop", "to_stop", "min"]]

    parts = [
        f"# Network report: {service_date:%a %Y-%m-%d}",
        "",
        f"Generated by `python -m src.build.network` at build `{_stamp()}`. "
        f"Output: `{config.network_npz(service_date).relative_to(config.DATA_DIR).as_posix()}` "
        "under the data directory. All build asserts passed.",
        "",
        "## Feeds",
        "",
        _md(pd.DataFrame(feeds)),
        "",
        f"Merged: {net.n_stops} stops (incl. virtual `{config.ROUTER_DEST_STOP}`), "
        f"{net.n_patterns} patterns, {len(net.trip_ids)} trips. A pattern is one route × one stop "
        "sequence; trips sorted by first departure, then split greedily until no trip overtakes "
        "its predecessor at any stop. Only trips of the service date itself are loaded; the "
        "previous day's trips running past midnight are not (irrelevant for the AM window).",
        "",
        "## Asserts",
        "",
        "- ≥ 1 active trip in each feed.",
        f"- Portal cutover: no trip of NJT routes {', '.join(config.CUTOVER_HOBOKEN_ROUTE_IDS)} "
        "(M&E, Gladstone, Montclair-Boonton) serves NJT:NY.",
        "- Times never decrease within a trip (after un-wrapping times written as 00:xx "
        "instead of 24:xx past midnight; count per feed above).",
        "- Every pattern is FIFO after splitting.",
        "- Every transfers.csv stop exists after the parent collapse (`DEST:` stops are virtual); "
        "every `stations_scope.csv` station maps to an NJT stop; NJT stop_code unique.",
        "- transfers.csv agrees with `config.TRANSFER_MIN_S` (HB → PATH, 33rd St → Penn) and the "
        "same-stop minimum equals the scorer's transfer buffer.",
        "- NJT:NY and PATH:33rd Street are served by at least one pattern.",
        "",
        "## PATH stop IDs",
        "",
        "PATH GTFS has no `parent_station`; each station is listed once per direction under the "
        "same `stop_name`, so platforms collapse on `stop_name` → `PATH:<stop_name>`.",
        "",
        _md(path_ids),
        "",
        "## Restricted pickup / drop-off (stop_times with type 1)",
        "",
    ]
    for tt in (njt, path):
        r = _restricted(tt)
        parts += [f"**{tt.prefix}:** " + ("none." if r.empty else ""), ""]
        if not r.empty:
            parts += [_md(r), ""]
    parts += [
        "## NJT trips with no trip_short_name",
        "",
        f"{len(no_name)}" + (": " + ", ".join(no_name["trip"]) if len(no_name) else "."),
        "PATH trips get a synthetic label `<route_short_name>@HH:MM <first stop>` (first departure).",
        "",
        "## Transfers (`data/reference/transfers.csv`, source = estimate on every row)",
        "",
        f"Same-stop minimum: {transfers.same_stop_s // 60} min.",
        "",
        _md(tr),
        "",
    ]
    return "\n".join(parts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", type=date.fromisoformat, default=config.DEMO_SERVICE_DATE)
    args = ap.parse_args()
    _, md = build(args.date)
    config.NETWORK_REPORT_MD.write_text(md, encoding="utf-8")
    log.info("wrote %s", config.NETWORK_REPORT_MD)


if __name__ == "__main__":
    main()
