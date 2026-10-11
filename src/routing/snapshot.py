"""Regression snapshots of router output and scores (router-v1 reference).

    python -m src.routing.snapshot      # rewrite the reference (only at a tagged router version)

journeys_snapshot(): every journey per access station, one line each.
scored_snapshot(): every scored row, both option sources × the core-claim sweep
(penalty × margin) × every train-choice variant. eval/alternatives.md compares
the exclusion=None output against the files written here at router-v1.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from src import config
from src.routing.raptor import describe, route_profile

REFERENCE_DIR = config.REPO_ROOT / "eval" / "reference"
JOURNEYS_REF = REFERENCE_DIR / "router_v1_journeys.txt"
SCORED_REF = REFERENCE_DIR / "router_v1_scored.csv"

PENALTIES = (0, 25, 75, 150)
MARGINS_MIN = (0, 5, 10)


def _hhmm(t) -> str:
    return "" if t is None else pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M:%S")


def _f(v, nd: int = 6) -> str:
    return "" if v is None else f"{v:.{nd}f}"


def journeys_snapshot(network, service_date=config.DEMO_SERVICE_DATE, deadline_s=config.DEMO_DEADLINE_S,
                      stations=config.ROUTER_ACCESS_STATIONS, **kwargs) -> list[str]:
    """One line per journey: station, then raptor.describe(); kwargs go to route_profile."""
    out = []
    for s in stations:
        for j in route_profile(network, config.ROUTER_NJT_PREFIX + s, service_date, deadline_s, **kwargs):
            out.append(f"{s}\t{describe(j)}")
    return out


def scored_snapshot(data, base=None, sources=("safety_net", "router"), simulate_fn=None) -> pd.DataFrame:
    """Every scored row (all variants) per source × margin × penalty, as strings.

    simulate_fn(data, settings) defaults to pipeline.simulate; pass one that runs
    without exclusion runs to reproduce router-v1.
    """
    from src.scoring.pipeline import simulate
    from src.scoring.scorer import Settings, reprice

    base = base or Settings()
    simulate_fn = simulate_fn or simulate
    rows = []
    for src in sources:
        for margin in MARGINS_MIN:
            sim = simulate_fn(data, replace(base, source=src, margin_s=margin * 60))
            for pen in PENALTIES:
                st = replace(base, source=src, margin_s=margin * 60, hard_penalty_usd=pen)
                for chosen in reprice(sim, st, data.inputs):
                    for v in chosen.variants or (chosen,):
                        c, t = v.candidate, v.candidate.planned_train
                        rows.append({
                            "source": src, "margin_min": margin, "penalty_usd": pen,
                            "candidate_id": v.candidate_id, "variant": c.variant,
                            "chosen": v is chosen or c.variant == chosen.candidate.variant,
                            "train": t.train_number if t else "", "leave_home": _hhmm(c.leave_home),
                            "rank": "" if chosen.rank is None else chosen.rank,
                            "sched_rank": "" if chosen.sched_rank is None else chosen.sched_rank,
                            "p_on_time": _f(v.p_on_time), "gen_cost_usd": _f(v.gen_cost_usd),
                            "money_usd": _f(v.money_usd), "arrival_p50": _hhmm(v.arrival_p50),
                            "arrival_p90": _hhmm(v.arrival_p90),
                            "catch_next": " ".join(o[0].train_number or "?" for o in c.catch_next),
                            "flags": ";".join(sorted(v.flags)),
                        })
    return pd.DataFrame(rows).astype(str)


def diff_frames(ref: pd.DataFrame, new: pd.DataFrame) -> list[str]:
    """Human-readable differences between two snapshot frames (empty = identical)."""
    if list(ref.columns) != list(new.columns):
        return [f"columns differ: {list(ref.columns)} vs {list(new.columns)}"]
    if len(ref) != len(new):
        return [f"row count differs: {len(ref)} vs {len(new)}"]
    out = []
    a, b = ref.reset_index(drop=True), new.reset_index(drop=True)
    for i in range(len(a)):
        for col in a.columns:
            if a.at[i, col] != b.at[i, col]:
                out.append(f"row {i} ({a.at[i, 'source']}, {a.at[i, 'candidate_id']}, "
                           f"v{a.at[i, 'variant']}) {col}: {a.at[i, col]!r} → {b.at[i, col]!r}")
    return out


def main() -> None:
    from src.scoring.pipeline import load_demo_data

    data = load_demo_data()
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    JOURNEYS_REF.write_text("\n".join(journeys_snapshot(data.network)) + "\n", encoding="utf-8")
    df = scored_snapshot(data)
    df.to_csv(SCORED_REF, index=False)
    print(f"wrote {JOURNEYS_REF} and {SCORED_REF} ({len(df)} scored rows)")


if __name__ == "__main__":
    main()
