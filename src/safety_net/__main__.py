"""Print the safety-net candidates: python -m src.safety_net [--deadline HH:MM]"""

from __future__ import annotations

import argparse

import pandas as pd

from src import config
from src.contracts import Candidate, LegMode
from src.gtfs import load_feed
from src.safety_net.candidates import build_candidates
from src.scoring.drive import load_drive_cache, lookup


def _hhmm(t) -> str:
    return pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M")


def describe(c: Candidate) -> str:
    head = f"{c.label:<34}"
    if not c.legs:
        return f"{head} INFEASIBLE: {c.infeasible_reason}"
    rides = [leg for leg in c.legs if leg.mode in (LegMode.RAIL, LegMode.PATH)]
    plan = " + ".join(f"{leg.train_number or leg.gtfs_line} {leg.from_stop} {_hhmm(leg.sched_dep)}"
                      f"→{leg.to_stop} {_hhmm(leg.sched_arr)}" for leg in rides) or "drive"
    nxt = ", ".join(f"{o[0].train_number} {_hhmm(o[0].sched_dep)}→{_hhmm(o[-1].sched_arr)}"
                    for o in c.catch_next[1:])
    flag = f"  INFEASIBLE: {c.infeasible_reason}" if c.infeasible else ""
    return (f"{head} leave {_hhmm(c.leave_home)}  arrive {_hhmm(c.sched_arr)}  {plan}{flag}"
            + (f"\n{'':<34} next: {nxt}" if nxt else ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deadline", default="09:00", help="arrive-by at NY Penn, HH:MM local")
    args = parser.parse_args()
    h, m = map(int, args.deadline.split(":"))
    cache = load_drive_cache()
    cands = build_candidates(load_feed(config.GTFS_NJT_RAIL_DIR), load_feed(config.GTFS_PATH_DIR),
                             lambda dest, t: lookup(cache, dest, t).best_guess_s,
                             deadline_s=h * 3600 + m * 60)
    print(f"{config.DEMO_SERVICE_DATE}, deadline {args.deadline} at NY Penn")
    for c in cands:
        print(describe(c))


if __name__ == "__main__":
    main()
