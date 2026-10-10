"""Phase 2.4 router validation → eval/router_validation.md.

    python -m src.routing.validate

Compares router options with the hard-coded station_options() per key, then
scores both sources through the same pipeline (demo preset + core-claim sweep).
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pandas as pd

from src import config
from src.routing.adapter import AdapterReport, router_options
from src.safety_net.candidates import to_utc
from src.scoring.pipeline import build_stamp, evaluate, load_demo_data, sweep
from src.scoring.scorer import Settings

log = logging.getLogger(__name__)

REPORT_MD = config.REPO_ROOT / "eval" / "router_validation.md"


def _hhmm(t) -> str:
    return "—" if t is None else pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M")


def _md(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(str(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(out)


def _opt(o) -> str:
    return f"{o[0].train_number} {_hhmm(o[0].sched_dep)}→{_hhmm(o[-1].sched_arr)}"


def compare_options(hard, router, service_date, deadline_s):
    """Per key: (summary rows, missing rows). Missing reason: dominated (by which) or late."""
    lo = to_utc(service_date, deadline_s - config.ROUTER_WINDOW_BEFORE_S)
    hi = to_utc(service_date, deadline_s + config.ROUTER_DEPART_AFTER_S)
    arr_max = to_utc(service_date, deadline_s + config.ROUTER_ARRIVE_AFTER_S)
    summary, missing = [], []
    for key in sorted(set(hard) | set(router)):
        h = [o for o in hard.get(key, []) if lo <= o[0].sched_dep <= hi]
        r = router.get(key, [])
        rs, hs = set(r), set(hard.get(key, []))
        n_dom = n_late = 0
        for o in h:
            if o in rs:
                continue
            by = [x for x in r if x[0].sched_dep >= o[0].sched_dep and x[-1].sched_arr <= o[-1].sched_arr]
            if o[-1].sched_arr > arr_max:
                reason, n_late = f"arrives after {_hhmm(arr_max)} (outside the router window)", n_late + 1
            elif by:
                reason, n_dom = f"profile-dominated by {_opt(by[0])}", n_dom + 1
            else:
                reason = "**unexplained**"
            missing.append({"key": key, "hard-coded option": _opt(o), "reason": reason})
        summary.append({"key": key, "hard-coded (window)": len(h), "router": len(r),
                        "matched": sum(o in rs for o in h), "dominated": n_dom, "late": n_late,
                        "router-only": sum(o not in hs for o in r),
                        "status": "new key" if key not in hard else
                        ("no train (cutover)" if not h and not r else "ok")})
    return pd.DataFrame(summary), pd.DataFrame(missing)


def score_rows(data, settings):
    rows = {}
    for src in ("safety_net", "router"):
        for s in evaluate(data, replace(settings, source=src)):
            c = s.candidate
            t = c.planned_train
            latest = s.variants[0] if s.variants else s
            r = rows.setdefault(s.candidate_id, {"option": s.candidate_id})
            tag = "SN" if src == "safety_net" else "R"
            r[f"{tag} rank"] = "—" if s.rank is None or "infeasible" in s.flags else s.rank
            r[f"{tag} train"] = t.train_number if t else "—"
            r[f"{tag} P(on time)"] = "—" if s.p_on_time is None else f"{s.p_on_time:.2f}"
            r[f"{tag} gen_cost"] = "—" if s.gen_cost_usd is None else f"{s.gen_cost_usd:.2f}"
            r[f"{tag} catch-next (latest plan)"] = (", ".join(o[0].train_number or "?" for o in
                                                              latest.candidate.catch_next) or "—")
    df = pd.DataFrame(rows.values())
    same = all(df[f"SN {c}"].equals(df[f"R {c}"]) for c in ("rank", "train", "P(on time)", "gen_cost"))
    return df, same


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    data = load_demo_data()
    sd, dl = data.inputs.service_date, config.DEMO_DEADLINE_S
    rep = AdapterReport()
    router = router_options(data.network, sd, dl, report=rep)
    summary, missing = compare_options(data.options, router, sd, dl)
    settings = Settings()
    scores, same_scores = score_rows(data, settings)
    sweeps = {src: sweep(data, replace(settings, source=src)) for src in ("safety_net", "router")}
    sw = pd.DataFrame({
        "penalty": sweeps["safety_net"]["hard_penalty_usd"], "margin": sweeps["safety_net"]["margin_min"],
        "safety net top (rank)": sweeps["safety_net"]["top_rank"],
        "router top (rank)": sweeps["router"]["top_rank"],
        "safety net top (sched)": sweeps["safety_net"]["top_sched_rank"],
        "router top (sched)": sweeps["router"]["top_sched_rank"],
    })
    sw["agree"] = ((sw["safety net top (rank)"] == sw["router top (rank)"])
                   & (sw["safety net top (sched)"] == sw["router top (sched)"])).map({True: "yes", False: "**no**"})
    unexplained = int((missing["reason"] == "**unexplained**").sum()) if len(missing) else 0
    new_keys = summary[summary["status"] == "new key"]["key"].tolist()
    go = unexplained == 0 and bool((sw["agree"] == "yes").all())
    prof = pd.DataFrame([{"station": s, "iterations": p.iterations, "patterns scanned": p.patterns_scanned,
                          "labels touched": p.labels_touched, "journeys": p.emitted - p.dropped_rides,
                          "dropped (> 2 rides)": p.dropped_rides, "runtime s": f"{p.runtime_s:.3f}"}
                         for s, p in rep.profile.items()])
    lines = [
        f"# Router validation ({sd:%a %Y-%m-%d}, deadline {dl // 3600:02d}:{dl % 3600 // 60:02d} at NY Penn)",
        "",
        f"Generated by `python -m src.routing.validate` at build `{build_stamp()}`. Router = range RAPTOR "
        f"(K = {config.ROUTER_MAX_ROUNDS}, ≤ {config.ROUTER_MAX_RIDES_OUT} rides out) per access station over "
        f"`network_{sd:%Y%m%d}.npz`, adapted to Options by `src/routing/adapter.py`. Window: departures "
        f"{config.ROUTER_WINDOW_BEFORE_S // 60} min before to {config.ROUTER_DEPART_AFTER_S // 60} min after the "
        f"deadline, arrivals ≤ deadline + {config.ROUTER_ARRIVE_AFTER_S // 60} min.",
        "",
        f"## Gate: **{'GO' if go else 'NO-GO'}**",
        "",
        f"- Unexplained missing hard-coded options: {unexplained}.",
        f"- Top option (rank and sched_rank) agrees with the safety net in "
        f"{int((sw['agree'] == 'yes').sum())} of {len(sw)} sweep settings.",
        f"- Demo preset scores identical (rank, train, P(on time), gen_cost): {'yes' if same_scores else 'no'}.",
        f"- New keys: {', '.join(new_keys) if new_keys else 'none'} (no new signature at the plannable "
        "stations CM / ST / RT / SE / NP; HB has no drive destination and is not planned).",
        "",
        "## Options per key",
        "",
        "Window = hard-coded options departing inside the router window; matched = equal as dataclasses, "
        "walk legs included.",
        "",
        _md(summary),
        "",
        "### Missing hard-coded options",
        "",
        _md(missing) if len(missing) else "None.",
        "",
        "Dropped router journeys: " + (", ".join(f"{k}: {v}" for k, v in rep.dropped.items()) or "none") + ".",
        "",
        "## Router runs",
        "",
        _md(prof),
        "",
        f"## Demo preset scored from both sources (penalty ${settings.hard_penalty_usd:g}, "
        f"margin {settings.margin_s // 60} min, N = {settings.n}, seed {settings.seed})",
        "",
        "SN = safety net (hard-coded options), R = router options. Catch-next of the latest-train plan shows "
        "where profile pruning changes the onward list.",
        "",
        _md(scores),
        "",
        "## Core-claim sweep from both sources",
        "",
        _md(sw),
        "",
        "Catch-next lists can be shorter from the router: trains arriving after the window "
        f"(deadline + {config.ROUTER_ARRIVE_AFTER_S // 60} min, = the scorer's late cap) and profile-dominated "
        "trains are not returned. A draw that misses every listed train is censored at the late cap, which is "
        "the most such a later train could have scored.",
        "",
    ]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {REPORT_MD}: {'GO' if go else 'NO-GO'}")


if __name__ == "__main__":
    main()
