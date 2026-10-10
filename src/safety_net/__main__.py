"""Print the safety-net candidates, priced and tagged:

python -m src.safety_net [--deadline HH:MM] [--mpg 28]
"""

from __future__ import annotations

import argparse

import pandas as pd

from src import config
from src.contracts import Candidate, LegMode
from src.gtfs import load_feed
from src.pricing.exposure import load_infra, tag_candidate
from src.pricing.pricer import load_price_tables, price_candidate
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


def _money(v: float | None) -> str:
    return "  —  " if v is None else f"{v:6.2f}"


def describe_costs(c: Candidate) -> str:
    k = c.costs
    items = "  ".join(f"{name} {_money(item.usd)}" for name, item in k.items.items())
    total = "not priced" if not k.priced else f"{k.total_usd:.2f}{' (partial)' if k.partial else ''}"
    exposure = ",".join(sorted(a.value for a in c.exposure)) or "{}"
    lines = [f"{c.label:<34} total {total:<16} {items}",
             f"{'':<34} exposure {exposure}  flags {sorted(k.flags | c.flags) or '-'}"]
    lines += [f"{'':<34} - {n}" for n in (*k.notes, *c.notes)]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deadline", default="09:00", help="arrive-by at NY Penn, HH:MM local")
    parser.add_argument("--mpg", type=float, default=config.DEFAULT_MPG)
    args = parser.parse_args()
    h, m = map(int, args.deadline.split(":"))
    cache = load_drive_cache()
    njt, path = load_feed(config.GTFS_NJT_RAIL_DIR), load_feed(config.GTFS_PATH_DIR)
    cands = build_candidates(njt, path, lambda dest, t: lookup(cache, dest, t).best_guess_s,
                             deadline_s=h * 3600 + m * 60)
    tables, infra = load_price_tables(), load_infra()
    routes = dict(zip(njt.routes["route_id"], njt.routes["route_long_name"]))
    cands = [tag_candidate(price_candidate(c, tables, cache, args.mpg), infra, routes) for c in cands]
    print(f"{config.DEMO_SERVICE_DATE}, deadline {args.deadline} at NY Penn")
    for c in cands:
        print(describe(c))
    print("\nBase cost per commuting day (round trip), USD")
    for c in cands:
        print(describe_costs(c))


if __name__ == "__main__":
    main()
