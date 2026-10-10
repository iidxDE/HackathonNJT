"""Crossing: minimal Streamlit table (Phase 1.6).

streamlit run app/app.py

Fully offline: reads GTFS, data/reference price tables, pools.parquet and
drive_cache.parquet. No API calls. Simulations are cached by the settings
that change them (deadline, margin, mpg); VOT and the hard penalty re-rank
the cached simulation exactly (scorer.reprice).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `src`

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from src import config  # noqa: E402
from src.contracts import InfraAsset, ScoredOption  # noqa: E402
from src.scoring import pipeline  # noqa: E402
from src.scoring.scorer import Settings, reprice  # noqa: E402

st.set_page_config(page_title="Crossing", layout="wide")

EXPOSURE_LABELS = {
    InfraAsset.HACKENSACK_PORTAL: "Portal bridges (Hackensack)",
    InfraAsset.NORTH_RIVER_TUNNELS: "North River tunnels",
    InfraAsset.NY_PENN: "NY Penn",
    InfraAsset.PATH_TUNNELS: "PATH tunnels",
}
FLAG_LABELS = {
    "infeasible": "no train meets deadline", "partial": "cost partial", "not_priced": "cost not priced",
    "fills_early": "lot fills early", "no_daily_parking": "no daily parking data",
    "parking_rate_unknown": "parking rate unknown", "l4_pool": "no delay data",
    "drive_variance_degenerate": "no drive spread", "exposure_varies": "exposure varies by train",
    "censored": "may miss every listed train",
}


@st.cache_resource(show_spinner="Loading timetables and delay pools…")
def demo_data() -> pipeline.DemoData:
    return pipeline.load_demo_data()


@st.cache_data(show_spinner=False, max_entries=64)
def simulate(deadline_s: int, margin_s: int, mpg: float) -> list[ScoredOption]:
    return pipeline.simulate(demo_data(), Settings(deadline_s=deadline_s, margin_s=margin_s, mpg=mpg))


def hhmm(t) -> str:
    return "—" if t is None else pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M")


def usd(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def usd_md(v: float | None) -> str:
    """usd() for markdown text: Streamlit renders $…$ as LaTeX, so escape the sign."""
    return usd(v).replace("$", r"\$")


def origin_label() -> str:
    try:
        return str(pd.read_csv(config.DEMO_ORIGIN_CSV)["label"].iloc[0])
    except Exception:  # noqa: BLE001 — the label is cosmetic
        return "demo origin"


def flags_text(s: ScoredOption) -> str:
    return ", ".join(FLAG_LABELS.get(f, f) for f in sorted(s.flags) if f in FLAG_LABELS) or ""


def row_order(s: ScoredOption) -> tuple:
    if s.rank is not None:
        return (0, s.rank)
    return (1, 0) if s.candidate.access_station == "DRIVE" else (2, s.candidate_id)


def ranked_table(scored: list[ScoredOption]) -> pd.DataFrame:
    rows = []
    for s in sorted(scored, key=row_order):
        c = s.candidate
        train = c.planned_train
        reference = c.access_station == "DRIVE"
        rows.append({
            "Rank": str(s.rank) if s.rank is not None else ("ref" if reference else "—"),
            "Option": s.candidate_id,
            "Leave home": hhmm(c.leave_home),
            "Planned train": train.train_number if train else ("drive" if reference else "—"),
            "On time (of 100)": "—" if s.p_on_time is None else str(round(100 * s.p_on_time)),
            "Arrival p50 / p90": "—" if s.arrival_p50 is None else f"{hhmm(s.arrival_p50)} / {hhmm(s.arrival_p90)}",
            "$/day": "cost not priced" if reference else usd(s.money_usd),
            "Risk-adjusted $": usd(s.gen_cost_usd),
            "Schedule-only rank": str(s.sched_rank) if s.sched_rank is not None else "—",
            "Depends on": ", ".join(EXPOSURE_LABELS[a] for a in sorted(c.exposure or (), key=lambda a: a.value))
                          or ("road crossing (not modeled)" if reference else "—"),
            "Flags": flags_text(s),
            "_infeasible": "infeasible" in s.flags, "_flip": s.rank is not None and s.rank != s.sched_rank,
        })
    return pd.DataFrame(rows)


def style(df: pd.DataFrame):
    def paint(row):
        if df.loc[row.name, "_infeasible"]:
            return ["color: #9aa0a6"] * len(row)
        if df.loc[row.name, "_flip"]:
            return ["background-color: rgba(255, 196, 0, 0.18)"] * len(row)
        return [""] * len(row)

    return df.style.apply(paint, axis=1).hide(["_infeasible", "_flip"], axis="columns")


def cost_rows(s: ScoredOption) -> pd.DataFrame:
    k = s.candidate.costs
    return pd.DataFrame([{"Item": name, "USD/day": usd(item.usd), "Note": item.note,
                          "Source": item.source_url or "—", "Checked": item.checked_date or "—"}
                         for name, item in k.items.items()])


def assumptions(c, settings: Settings) -> list[str]:
    out = [f"Safety margin {settings.margin_s // 60} min before the station buffer (setting)",
           f"Fuel at {settings.mpg:g} mpg (setting, assumed)",
           "Drive time: triangular over Google OPTIMISTIC / BEST_GUESS / PESSIMISTIC (Routes API, cached)"]
    if c.access_station in config.STATION_BUFFER_S:
        out.append(f"Station buffer at {c.access_station}: {config.STATION_BUFFER_S[c.access_station] // 60} min "
                   "(park → platform, estimate)")
    if any(leg.to_stop.startswith("PATH") for leg in c.legs):
        out += [f"Hoboken NJT → PATH transfer {config.TRANSFER_MIN_S['hoboken_njt_to_path'] // 60} min (estimate)",
                f"33rd St → NY Penn walk {config.TRANSFER_MIN_S['path_33st_to_penn_walk'] // 60} min (estimate)",
                "PATH runs to schedule: delays assumed, not measured"]
    if c.access_station == "DRIVE":
        out.append(f"Garage → NY Penn walk {config.GARAGE_TO_PENN_WALK_S // 60} min (estimate)")
    return out


# --- Sidebar -----------------------------------------------------------------
with st.sidebar:
    st.header("Settings")
    deadline = st.time_input("Arrive at NY Penn by", value=pd.Timestamp("09:00").time(), step=300)
    vot = st.number_input("Value of time ($/h)", min_value=0.0, value=config.DEFAULT_VOT_USD_PER_H, step=5.0)
    penalty = st.number_input("Hard-deadline penalty ($; 0 = soft)", min_value=0.0,
                              value=config.DEFAULT_HARD_PENALTY_USD, step=25.0)
    margin_min = st.number_input("Safety margin (min)", min_value=0, max_value=30,
                                 value=config.SAFETY_MARGIN_S // 60, step=1)
    mpg = st.number_input("Car fuel economy (mpg)", min_value=5.0, value=config.DEFAULT_MPG, step=1.0)
    st.caption(f"Scenario: Tue {config.DEMO_SERVICE_DATE:%m-%d}, regular weekday, origin = {origin_label()}, "
               "destination = NY Penn.")
    st.caption(f"Monte Carlo: N = {config.N_SAMPLES}, seed {config.DEFAULT_SEED}.")

settings = Settings(deadline_s=deadline.hour * 3600 + deadline.minute * 60, vot_usd_per_h=vot,
                    hard_penalty_usd=penalty, margin_s=int(margin_min) * 60, mpg=mpg)
data = demo_data()
scored = reprice(simulate(settings.deadline_s, settings.margin_s, settings.mpg), settings,
                 data.inputs.service_date)

# --- Main view ---------------------------------------------------------------
st.title("Crossing: Chatham-area commute to NY Penn")
st.info("Reliability is estimated from about one weekday of observed NJ Transit delays (Fri 10-09 AM peak). "
        "Estimates are rough and improve as the archive grows.")

best = next((s for s in scored if s.rank == 1), None)
if best:
    train = best.candidate.planned_train
    st.subheader(f"Best option: {best.candidate_id}. Leave {hhmm(best.candidate.leave_home)}, "
                 f"train {train.train_number if train else '—'}, on time {round(100 * best.p_on_time)} of 100 "
                 f"mornings, {usd_md(best.money_usd)}/day, risk-adjusted {usd_md(best.gen_cost_usd)}.")
else:
    st.warning("No option can be ranked for these settings.")

table = ranked_table(scored)
st.dataframe(style(table), hide_index=True, width="stretch")
st.caption(r"Highlighted: risk changes this option's rank. Grey: no train meets the deadline on Tue 10-13 "
           r"(Portal Cutover: Morris & Essex trains end at Hoboken). Risk-adjusted \$ = \$/day + value of time "
           r"from leaving home to the deadline + expected cost of lateness. \$/day is a round trip.")

st.header("Options in detail")
for s in sorted(scored, key=row_order):
    c = s.candidate
    with st.expander(s.candidate_id + (" (no train)" if not c.legs else "")):
        if c.infeasible_reason:
            st.write(f"**Infeasible:** {c.infeasible_reason}.")
        st.markdown("**Cost breakdown (per commuting day)**")
        st.dataframe(cost_rows(s), hide_index=True, width="stretch")
        if s.p_on_time is not None:
            st.markdown(f"**Cost of time and risk:** time {usd_md(s.time_cost_usd)} + expected lateness "
                        f"{usd_md(s.exp_late_cost_usd)} (expected {s.expected_late_min:.1f} min late).")
        if s.train_taken_share:
            st.markdown("**Which train you end up on** (mornings out of 100)")
            st.dataframe(pd.DataFrame([{"Train": t, "Mornings": round(100 * v)} for t, v in
                                       sorted(s.train_taken_share.items(), key=lambda kv: -kv[1])]),
                         hide_index=True)
        if s.pools:
            st.markdown("**Evidence (delay pools)**")
            st.dataframe(pd.DataFrame([{
                "Train": u.train_number, "Pool level": u.fallback_level.value, "Runs": u.run_count,
                "Pool key": f"line {u.pool_key.line or 'all'}, toward NY {u.pool_key.toward_ny}, "
                            f"period {u.pool_key.period or 'all'}",
                "Delay field": ", ".join(f"{k}: {v.value}" for k, v in u.delay_field_at_stop.items()),
            } for u in s.pools]), hide_index=True, width="stretch")
        st.markdown("**Assumptions used**")
        st.markdown("\n".join(f"- {a}" for a in assumptions(c, settings)))
        if c.notes:
            st.markdown("**Notes**\n" + "\n".join(f"- {n}" for n in c.notes))

st.header("Does risk change the answer?")
ranked = [s for s in scored if s.rank is not None]
st.dataframe(pd.DataFrame([{"Option": s.candidate_id, "Rank (with risk)": s.rank,
                            "Schedule-only rank": s.sched_rank} for s in sorted(ranked, key=lambda s: s.rank)]),
             hide_index=True)
flip = pipeline.top(scored, "rank") != pipeline.top(scored, "sched_rank")
st.write(f"With these settings the top option **{'changes' if flip else 'does not change'}** when risk is "
         f"priced in: {pipeline.top(scored, 'rank')} vs schedule-only {pipeline.top(scored, 'sched_rank')}.")

rows = []
for m in (0, 5, 10):
    sim = simulate(settings.deadline_s, m * 60, settings.mpg)
    for p in (0, 25, 75, 150):
        r = reprice(sim, replace(settings, margin_s=m * 60, hard_penalty_usd=p), data.inputs.service_date)
        top, sched = pipeline.top(r, "rank"), pipeline.top(r, "sched_rank")
        rows.append({"Hard penalty $": p, "Margin min": m, "Top with risk": top,
                     "Top schedule-only": sched, "Flip": "flip" if top != sched else "no flip"})
st.markdown("**Sweep** (current deadline, VOT and mpg; leave-home re-planned per margin)")
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
st.caption("Caveats: about one weekday of AM-peak data; CM → HB has 5 effective runs, so its on-time "
           "count moves in steps of 20; PATH runs to schedule (not measured); cancelled runs are invisible; "
           "the NY delay measure is provisional (check A).")
