"""End-to-end request path: plan (1.3) → price (1.4) → tag (1.4) → score + rank (1.5).

Reads only local files (GTFS, reference CSVs, artifacts). Used by the CLI
(python -m src.scoring) and the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

import numpy as np
import pandas as pd

from src import config
from src.contracts import ScoredOption
from src.gtfs import GtfsFeed, load_feed, stop_pairs
from src.pricing.exposure import load_infra, tag_candidate
from src.pricing.pricer import PriceTables, load_price_tables, price_candidate
from src.safety_net.candidates import build_candidates, to_utc
from src.scoring.drive import load_drive_cache, lookup
from src.scoring.pools import load_pools
from src.scoring.scorer import ScoringInputs, Settings, score_all


@dataclass(frozen=True)
class DemoData:
    njt: GtfsFeed
    path: GtfsFeed
    tables: PriceTables
    infra: pd.DataFrame
    inputs: ScoringInputs

    @property
    def route_names(self) -> dict[str, str]:
        return dict(zip(self.njt.routes["route_id"], self.njt.routes["route_long_name"]))


def path_schedule(path: GtfsFeed, service_date) -> np.ndarray:
    """(m, 2) epoch seconds of PATH Hoboken departures and 33rd St arrivals, sorted by departure."""
    pairs = stop_pairs(path, service_date, config.PATH_HOBOKEN_STOP, config.PATH_33RD_STOP,
                       key_col="stop_name").sort_values("dep_s")
    return np.array([[to_utc(service_date, d).timestamp(), to_utc(service_date, a).timestamp()]
                     for d, a in zip(pairs["dep_s"], pairs["arr_s"])])


def load_demo_data(service_date=config.DEMO_SERVICE_DATE) -> DemoData:
    njt, path = load_feed(config.GTFS_NJT_RAIL_DIR), load_feed(config.GTFS_PATH_DIR)
    inputs = ScoringInputs(load_pools(), load_drive_cache(), path_schedule(path, service_date), service_date)
    return DemoData(njt, path, load_price_tables(), load_infra(), inputs)


def evaluate(data: DemoData, settings: Settings) -> list[ScoredOption]:
    cache = data.inputs.drive_cache
    cands = build_candidates(data.njt, data.path, lambda dest, t: lookup(cache, dest, t).best_guess_s,
                             data.inputs.service_date, settings.deadline_s, settings.margin_s)
    cands = [tag_candidate(price_candidate(c, data.tables, cache, settings.mpg), data.infra,
                           data.route_names) for c in cands]
    return score_all(cands, data.inputs, settings)


def top(scored: list[ScoredOption], field: str) -> str | None:
    best = [s for s in scored if getattr(s, field) == 1]
    return best[0].candidate_id if best else None


def sweep(data: DemoData, base: Settings, penalties=(0, 25, 75, 150),
          margins_min=(0, 5, 10)) -> pd.DataFrame:
    """Top-1 by gen_cost vs by money + time only, over penalty × margin (re-planned per margin)."""
    rows = []
    for margin in margins_min:
        planned = evaluate(data, replace(base, margin_s=margin * 60, hard_penalty_usd=0))
        for penalty in penalties:
            scored = planned if penalty == 0 else evaluate(
                data, replace(base, margin_s=margin * 60, hard_penalty_usd=penalty))
            rows.append({"hard_penalty_usd": penalty, "margin_min": margin,
                         "top_rank": top(scored, "rank"), "top_sched_rank": top(scored, "sched_rank"),
                         "flips": top(scored, "rank") != top(scored, "sched_rank"),
                         "p_on_time": {s.candidate_id: round(s.p_on_time, 3)
                                       for s in scored if s.p_on_time is not None}})
    return pd.DataFrame(rows)
