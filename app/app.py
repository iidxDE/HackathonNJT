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

NOTICE = "Midtown Direct to Penn suspended weekdays Oct 11–Nov 14 (Portal cutover)"
EXPOSURE_LABELS = {
    InfraAsset.HACKENSACK_PORTAL: "Portal bridges (final cutover in progress)",
    InfraAsset.NORTH_RIVER_TUNNELS: "North River tunnels (1910; lasting bottleneck)",
    InfraAsset.NY_PENN: "NY Penn",
    InfraAsset.PATH_TUNNELS: "PATH tunnels",
}
FLAG_LABELS = {
    "infeasible": "no train meets deadline", "partial": "cost partial", "not_priced": "cost not priced",
    "fills_early": "lot fills early", "no_daily_parking": "no daily parking data",
    "parking_rate_unknown": "parking rate unknown", "parking_unavailable": "no non-resident parking", "l4_pool": "no delay data",
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


def infeasible_reason(s: ScoredOption) -> str:
    """Why a row is not an option, in plain words."""
    c = s.candidate
    if "parking_unavailable" in s.flags:
        return "no daily parking for a non-resident at this time (details in the cost breakdown)"
    return c.infeasible_reason or "no train meets the deadline"


def row_order(s: ScoredOption) -> tuple:
    if "infeasible" in s.flags:
        return (3, s.candidate_id)
    if s.rank is not None:
        return (0, s.rank)
    return (1, 0) if s.candidate.access_station == "DRIVE" else (2, s.candidate_id)


def variant_line(v: ScoredOption) -> str:
    t = v.candidate.planned_train
    on_time = "—" if v.p_on_time is None else f"{round(100 * v.p_on_time)}/100"
    return f"{t.train_number if t else 'drive'}: leave {hhmm(v.candidate.leave_home)}, on time {on_time}"


def latest_variant(s: ScoredOption) -> ScoredOption:
    return s.variants[0] if s.variants else s


def ranked_table(scored: list[ScoredOption]) -> pd.DataFrame:
    rows = []
    for s in sorted(scored, key=row_order):
        c = s.candidate
        train = c.planned_train
        reference = c.access_station == "DRIVE"
        infeasible = "infeasible" in s.flags
        latest = latest_variant(s)
        rows.append({
            "Rank": "—" if infeasible else (str(s.rank) if s.rank is not None else ("ref" if reference else "—")),
            "Option": s.candidate_id,
            "Leave home": hhmm(c.leave_home),
            "Train (chosen)": train.train_number if train else ("drive" if reference else "—"),
            "Timetable plan": ("—" if infeasible or latest.candidate.variant == c.variant
                               else variant_line(latest) + " (latest)"),
            "On time (of 100)": "—" if s.p_on_time is None else str(round(100 * s.p_on_time)),
            "Arrival p50 / p90": "—" if s.arrival_p50 is None else f"{hhmm(s.arrival_p50)} / {hhmm(s.arrival_p90)}",
            "$/day": "cost not priced" if reference else usd(s.money_usd),
            "Risk-adjusted $": usd(s.gen_cost_usd),
            "Schedule-only rank": "—" if infeasible or s.sched_rank is None else str(s.sched_rank),
            "Depends on": ", ".join(EXPOSURE_LABELS[a] for a in sorted(c.exposure or (), key=lambda a: a.value))
                          or ("road crossing (not modeled)" if reference else "—"),
            "Flags": infeasible_reason(s) if infeasible else flags_text(s),
            "_infeasible": infeasible,
            "_flip": not infeasible and s.rank is not None and (s.rank != s.sched_rank
                                                                 or latest.candidate.variant != c.variant),
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
           f"On time = arriving at NY Penn by the deadline + {config.ON_TIME_GRACE_S} s grace",
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
scored = reprice(simulate(settings.deadline_s, settings.margin_s, settings.mpg), settings, data.inputs)

# --- Main view ---------------------------------------------------------------
st.title("Crossing: Chatham-area commute to NY Penn")
st.info("Reliability is estimated from about one weekday of observed NJ Transit delays (Fri 10-09 AM peak). "
        "Estimates are rough and improve as the archive grows.")
st.warning("Delay history predates the Portal cutover; Hoboken/PATH crowding and PATH delays are not modeled "
           "(optimistic for Hoboken options).")

best = next((s for s in scored if s.rank == 1 and "infeasible" not in s.flags), None)
if best:
    train = best.candidate.planned_train
    latest = latest_variant(best)
    contrast = ("" if latest.candidate.variant == best.candidate.variant
                else f" The timetable plan would be {variant_line(latest)}.")
    st.subheader(f"Best option: {best.candidate_id}, train {train.train_number if train else '—'}. "
                 f"Leave {hhmm(best.candidate.leave_home)}, on time {round(100 * best.p_on_time)} of 100 "
                 f"mornings, {usd_md(best.money_usd)}/day, risk-adjusted {usd_md(best.gen_cost_usd)}.{contrast}")
else:
    st.warning("No option can be ranked for these settings.")

no_train = [s for s in scored if not s.candidate.legs]
scored = [s for s in scored if s.candidate.legs]  # rows without a train become one notice
if no_train:
    st.info(f"{NOTICE}: no one-seat ride to NY Penn from "
            f"{', '.join(s.candidate.access_station for s in sorted(no_train, key=lambda s: s.candidate_id))}. "
            "Their rail option is Hoboken + PATH (rows below).")
table = ranked_table(scored)
st.dataframe(style(table), hide_index=True, width="stretch")
st.caption(r"Highlighted: risk changes this option's rank or train. Grey: not an option (reason in Flags). "
           r"The chosen train is the cheapest of the last 3 on-time trains on risk-adjusted cost; the timetable plan "
           r"is the latest on-time train. Risk-adjusted \$ = \$/day + value of time from leaving home to the "
           r"deadline + expected cost of lateness. \$/day is a round trip.")

st.header("Options in detail")
for s in sorted(scored, key=row_order):
    c = s.candidate
    with st.expander(s.candidate_id + (" (no train)" if not c.legs else "")):
        if "infeasible" in s.flags:
            st.write(f"**Not an option:** {infeasible_reason(s)}.")
        if len(s.variants) > 1:
            st.markdown("**Trains considered** (last on-time trains; the cheapest risk-adjusted is chosen)")
            st.dataframe(pd.DataFrame([{
                "Train": v.candidate.planned_train.train_number if v.candidate.planned_train else "—",
                "Plan": "latest (timetable)" if v.candidate.variant == 0 else f"{v.candidate.variant} earlier",
                "Leave home": hhmm(v.candidate.leave_home),
                "On time (of 100)": "—" if v.p_on_time is None else str(round(100 * v.p_on_time)),
                "Risk-adjusted $": usd(v.gen_cost_usd),
                "Chosen": "yes" if v.candidate.variant == c.variant else "",
            } for v in s.variants]), hide_index=True)
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
                "Boarding delay zero-filled": f"{u.board_zero_share:.0%}",
                "Pool key": f"line {u.pool_key.line or 'all'}, toward NY {u.pool_key.toward_ny}, "
                            f"period {u.pool_key.period or 'all'}",
                "Delay field": ", ".join(f"{k}: {v.value}" for k, v in u.delay_field_at_stop.items()),
            } for u in s.pools]), hide_index=True, width="stretch")
        st.markdown("**Assumptions used**")
        st.markdown("\n".join(f"- {a}" for a in assumptions(c, settings)))
        if c.notes:
            st.markdown("**Notes**\n" + "\n".join(f"- {n}" for n in c.notes))

st.header("Does risk change the answer?")
ranked = [s for s in scored if s.rank is not None and "infeasible" not in s.flags]
st.dataframe(pd.DataFrame([{"Option": s.candidate_id, "Train (with risk)": pipeline.chosen_train(s),
                            "Rank (with risk)": s.rank, "Timetable train": pipeline.timetable_train(s),
                            "Schedule-only rank": s.sched_rank}
                           for s in sorted(ranked, key=lambda s: s.rank)]), hide_index=True)
risk, sched = pipeline.top_option(scored, "rank"), pipeline.top_option(scored, "sched_rank")
fmt = lambda o: f"{o[0]} on train {o[1]}" if o else "—"  # noqa: E731
st.write(f"With these settings the top option **{'changes' if risk != sched else 'does not change'}** when "
         f"risk is priced in: {fmt(risk)} vs schedule-only {fmt(sched)}.")

sw = pipeline.sweep(data, settings, simulate_fn=lambda x: simulate(x.deadline_s, x.margin_s, x.mpg))
st.markdown("**Sweep** (current deadline, VOT and mpg; leave-home re-planned per margin; same code as "
            "eval/core_claim.md)")
st.dataframe(sw.drop(columns=["p_on_time"]).rename(columns={
    "hard_penalty_usd": "Hard penalty $", "margin_min": "Margin min", "top_rank": "Top with risk (train)",
    "top_sched_rank": "Top schedule-only (train)", "flips_station": "Station differs",
    "flips_option": "Option differs"}), hide_index=True, width="stretch")
st.caption("Exposure: the Portal bridges matter only while the final cutover (Oct 11 – Nov 14) runs; "
           "the North River tunnels (1910) stay the fragile asset until the Hudson Tunnel Project's new "
           "tunnels open, planned for the 2030s.")
st.caption("Caveats: about one weekday of AM-peak data; the M&E → HB population is 7 runs (boarding delay "
           "zero-filled where the boarding stop was not observed); PATH runs to schedule (not measured); "
           "cancelled runs are invisible; the NY delay measure is provisional (check A).")
st.caption(f"Build {pipeline.build_stamp()} · N = {config.N_SAMPLES}, seed {config.DEFAULT_SEED}.")
