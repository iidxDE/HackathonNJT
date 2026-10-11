"""End-to-end request path: plan (1.3) → price (1.4) → tag (1.4) → score + rank (1.5).

Reads only local files (GTFS, reference CSVs, artifacts); no network. Used by
the CLI (python -m src.scoring) and the Streamlit app.

simulate() depends on deadline, margin, mpg, seed, N and the sensitivity
knobs; VOT and hard penalty are applied afterwards by scorer.reprice, which is
exact, so the UI can re-rank without re-simulating.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from src import config
from src.contracts import ScoredOption
from src.gtfs import GtfsFeed, load_feed, stop_pairs
from src.pricing.exposure import InfraIndex, index_infra, load_infra, tag_candidate
from src.pricing.pricer import PriceTables, load_price_tables, price_candidate
from src.routing.adapter import route_all
from src.routing.network import Network, load_transfers
from src.safety_net.candidates import Option, build_candidates, station_options, to_utc
from src.scoring.alerts import NO_ALERTS, AlertSet, build_alert_set
from src.scoring.drive import index_drive_cache, load_drive_cache, lookup
from src.scoring.pools import index_pools, load_pools
from src.scoring.scorer import ScoringInputs, Settings, reprice, score_all


@dataclass(frozen=True, eq=False)
class DemoData:
    njt: GtfsFeed
    path: GtfsFeed
    tables: PriceTables
    infra: InfraIndex
    inputs: ScoringInputs  # pools / drive cache as pre-built indexes
    options: dict[str, list[Option]]  # station_options for the service date
    network: Network | None = None  # router network (None if not built)

    def options_for(self, settings: Settings) -> dict[str, list[Option]]:
        """Hard-coded options, or router options for this deadline (cached on the network)."""
        return self.options_and_flags(settings)[0]

    def options_and_flags(self, settings: Settings) -> tuple[dict[str, list[Option]], dict[str, frozenset[str]]]:
        """(options per label, flags per label). Flags mark exclusion-run alternatives."""
        if settings.source == "safety_net":
            return self.options, {}
        if settings.source != "router":
            raise ValueError(f"unknown option source {settings.source!r}")
        if self.network is None:
            raise FileNotFoundError("router network not built: python -m src.build.network")
        res = route_all(self.network, self.inputs.service_date, settings.deadline_s,
                        exclusions=None if settings.exclusion_runs else {}, base=settings.alert_set.exclusion)
        return res.options, res.label_flags

    def alert_set(self, alert_id: str, deadline_s: int = config.DEMO_DEADLINE_S) -> AlertSet:
        """AlertSet for a UI choice ("none", "penn_scenario", an archived alert_id); cached per id."""
        cache = self.__dict__.setdefault("_alert_sets", {})
        if (alert_id, deadline_s) not in cache:
            base = self.options_and_flags(Settings(deadline_s=deadline_s))[0] if self.network else self.options
            name_to_code = dict(zip(self.njt.stops["stop_name"].str.upper().str.replace(".", "", regex=False),
                                    self.njt.stops["stop_code"].astype(str)))
            cache[(alert_id, deadline_s)] = build_alert_set(alert_id, base, name_to_code)
        return cache[(alert_id, deadline_s)]

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
    same_stop = (load_transfers(config.TRANSFERS_CSV).same_stop_s if config.TRANSFERS_CSV.exists()
                 else config.SAME_STOP_TRANSFER_S)
    inputs = ScoringInputs(index_pools(load_pools()), index_drive_cache(load_drive_cache()),
                           path_schedule(path, service_date), service_date, same_stop)
    npz = config.network_npz(service_date)
    return DemoData(njt, path, load_price_tables(), index_infra(load_infra()), inputs,
                    station_options(njt, path, service_date),
                    Network.load(npz) if npz.exists() else None)


def simulate(data: DemoData, settings: Settings) -> list[ScoredOption]:
    cache = data.inputs.drive_cache
    options, label_flags = data.options_and_flags(settings)
    cands = build_candidates(None, None, lambda dest, t: lookup(cache, dest, t).best_guess_s,
                             data.inputs.service_date, settings.deadline_s, settings.margin_s,
                             options=options, source=settings.source, label_flags=label_flags)
    if settings.alert_set.exclusion:
        cands = mark_unavailable(cands, data.options_and_flags(replace(settings, alert_set=NO_ALERTS))[0],
                                 settings.alert_set)
    cands = [tag_candidate(price_candidate(c, data.tables, cache, settings.mpg), data.infra,
                           data.route_names) for c in cands]
    return score_all(cands, data.inputs, settings)


def mark_unavailable(cands, no_alert_options: dict[str, list[Option]], alert_set: AlertSet):
    """Rows that have trains without the alert and none with it: infeasible, alert named."""
    out = []
    for c in cands:
        if c.infeasible and not c.legs and no_alert_options.get(c.label):
            c = replace(c, infeasible_reason=f"unavailable under the alert: {alert_set.name}",
                        flags=c.flags | {"alert_unavailable"})
        out.append(c)
    return out


def evaluate(data: DemoData, settings: Settings) -> list[ScoredOption]:
    return reprice(simulate(data, settings), settings, data.inputs)


def build_stamp() -> str:
    """Short git hash of the code that produced a result (+ "-dirty" with uncommitted changes)."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=config.REPO_ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip()

    try:
        head = git("rev-parse", "--short", "HEAD")
        # Generated reports under eval/ do not count: regenerating one must not mark the next dirty.
        dirty = git("status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)eval")
        return (head or "unknown") + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001 — a stamp must never break the app
        return "unknown"


def top(scored: list[ScoredOption], field: str) -> str | None:
    best = [s for s in scored if getattr(s, field) == 1]
    return best[0].candidate_id if best else None


def chosen_train(s: ScoredOption) -> str:
    t = s.candidate.planned_train
    return t.train_number if t else "drive"


def timetable_train(s: ScoredOption) -> str:
    """Train of the latest-train variant (the timetable plan)."""
    latest = s.variants[0] if s.variants else s
    return chosen_train(latest)


def top_option(scored: list[ScoredOption], field: str) -> tuple[str, str] | None:
    """(station row, train) of the rank-1 option; for sched_rank, the timetable train."""
    best = [s for s in scored if getattr(s, field) == 1]
    if not best:
        return None
    return best[0].candidate_id, (chosen_train(best[0]) if field == "rank" else timetable_train(best[0]))


def sweep(data: DemoData, base: Settings, penalties=(0, 25, 75, 150), margins_min=(0, 5, 10),
          simulate_fn=None) -> pd.DataFrame:
    """Top-1 with risk vs schedule-only, over penalty × margin (re-planned per margin).

    flips_station: the station row differs. flips_option: the station or the train
    differs (risk picks an earlier train at the same station). simulate_fn(settings)
    lets the UI pass its cached simulate, so CLI and UI share this code path.
    """
    simulate_fn = simulate_fn or (lambda s: simulate(data, s))
    rows = []
    for margin in margins_min:
        simulated = simulate_fn(replace(base, margin_s=margin * 60))
        for penalty in penalties:
            scored = reprice(simulated, replace(base, margin_s=margin * 60, hard_penalty_usd=penalty),
                             data.inputs)
            risk, sched = top_option(scored, "rank"), top_option(scored, "sched_rank")
            rows.append({"hard_penalty_usd": penalty, "margin_min": margin,
                         "top_rank": f"{risk[0]} ({risk[1]})" if risk else None,
                         "top_sched_rank": f"{sched[0]} ({sched[1]})" if sched else None,
                         "flips_station": bool(risk and sched and risk[0] != sched[0]),
                         "flips_option": risk != sched,
                         "p_on_time": {s.candidate_id: round(s.p_on_time, 3)
                                       for s in scored if s.p_on_time is not None}})
    return pd.DataFrame(rows)
