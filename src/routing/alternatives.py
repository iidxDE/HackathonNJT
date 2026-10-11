"""Phase 3.1 exclusion runs → eval/alternatives.md.

    python -m src.routing.alternatives

Checks 1–3 fail the build (non-zero exit, AssertionError):
  1. no exclusion-run journey or Option boards or alights at an excluded stop (NJT:NY);
  2. the exposure tagger reports neither NORTH_RIVER_TUNNELS nor NY_PENN on any
     flagged row, catch-next options included (independent cross-check);
  3. exclusion=None reproduces router-v1 (journeys + every scored row, eval/reference/).
Checks 4–9 are reported: new keys per station, trains added to existing keys,
baseline-key scores, new rows, unscored alternatives, core-claim sweep, timing.
"""

from __future__ import annotations

import logging
import statistics
import sys
import time
from dataclasses import replace

import pandas as pd

from src import config
from src.contracts import InfraAsset, LegMode
from src.pricing.exposure import legs_exposure
from src.routing.adapter import describe_option, leg_stop, route_all
from src.routing.raptor import route_profile
from src.routing.snapshot import (JOURNEYS_REF, SCORED_REF, diff_frames, journeys_snapshot,
                                  scored_snapshot)
from src.safety_net.candidates import HB_PATH_STATIONS
from src.scoring.pipeline import build_stamp, evaluate, load_demo_data, sweep
from src.scoring.scorer import Settings

log = logging.getLogger(__name__)

REPORT_MD = config.REPO_ROOT / "eval" / "alternatives.md"
FORBIDDEN_ASSETS = frozenset({InfraAsset.NORTH_RIVER_TUNNELS, InfraAsset.NY_PENN})
TIMING_REPEATS = 5


def _md(df: pd.DataFrame) -> str:
    if df is None or not len(df):
        return "None."
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(str(v).replace("|", "/") for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(out)


def _hhmm(t) -> str:
    return "—" if t is None else pd.Timestamp(t).tz_convert(config.LOCAL_TZ).strftime("%H:%M")


def _is_alt(s) -> bool:
    return any(f.startswith(config.ALT_FLAG_PREFIX) for f in s.flags)


def check_no_excluded_stops(data, sd, dl, res) -> list[str]:
    """Check 1 over raw journeys (every shape, scored or not) and over adapted Options."""
    bad = []
    for name, ex in config.ROUTER_EXCLUSIONS.items():
        for s in config.ROUTER_ACCESS_STATIONS:
            for j in route_profile(data.network, config.ROUTER_NJT_PREFIX + s, sd, dl, exclusion=ex):
                for leg in j.legs:
                    if leg.mode == "walk":
                        continue
                    if leg.board_stop in ex.no_board or leg.alight_stop in ex.no_alight:
                        bad.append(f"{name} {s}: {leg.train_no} {leg.board_stop}→{leg.alight_stop}")
        no_board = {leg_stop(x) for x in ex.no_board}
        no_alight = {leg_stop(x) for x in ex.no_alight}
        for n, label, opt in res.report.exclusion_options:
            if n != name:
                continue
            for leg in opt:
                if leg.mode in (LegMode.RAIL, LegMode.PATH) and (leg.from_stop in no_board
                                                               or leg.to_stop in no_alight):
                    bad.append(f"{name} {label}: {describe_option(opt)}")
    return bad


def check_exposure(scored, data) -> tuple[list[str], pd.DataFrame]:
    """Check 2: flagged rows (every variant, every catch-next option) never touch NRT / NY Penn."""
    bad, rows = [], []
    for chosen in scored:
        for v in chosen.variants or (chosen,):
            if not _is_alt(v):
                continue
            c = v.candidate
            hit = set(c.exposure or ()) & FORBIDDEN_ASSETS
            for o in c.catch_next:
                hit |= legs_exposure(o, data.infra, data.route_names)[0] & FORBIDDEN_ASSETS
            if hit:
                bad.append(f"{v.candidate_id} v{c.variant}: {sorted(a.value for a in hit)}")
            rows.append({"row": v.candidate_id, "variant": c.variant,
                         "train": c.planned_train.train_number if c.planned_train else "—",
                         "exposure (tagger)": ", ".join(sorted(a.value for a in c.exposure or ())) or "none",
                         "catch-next options checked": len(c.catch_next),
                         "NRT / NY Penn": "**yes**" if hit else "no"})
    return bad, pd.DataFrame(rows)


def check_router_v1(data) -> tuple[list[str], int, int]:
    """Check 3: plain runs and exclusion_runs=False scores equal the router-v1 reference."""
    diffs = []
    ref_j = JOURNEYS_REF.read_text(encoding="utf-8").splitlines()
    for name, kwargs in (("exclusion=None", {}), ("empty Exclusion", {"exclusion": config.Exclusion()})):
        new_j = journeys_snapshot(data.network, **kwargs)
        if new_j != ref_j:
            diffs.append(f"journeys ({name}) differ: {len(ref_j)} reference vs {len(new_j)}")
    ref = pd.read_csv(SCORED_REF, dtype=str, keep_default_na=False)
    new = scored_snapshot(data, Settings(exclusion_runs=False))
    diffs += diff_frames(ref, new)
    return diffs, len(ref_j), len(ref)


def time_router(data, sd, dl) -> tuple[pd.DataFrame, float, float]:
    """Standalone router time per query (all stations, plain + exclusion runs): median of repeats."""
    route_all(data.network, sd, dl, fresh=True)  # warm-up: array preparation, trip index
    totals, per = [], {}
    for _ in range(TIMING_REPEATS):
        t0 = time.perf_counter()
        res = route_all(data.network, sd, dl, fresh=True)
        totals.append(time.perf_counter() - t0)
        for s, st in res.report.profile.items():
            per.setdefault((s, "plain"), []).append(st.runtime_s)
        for (s, name), st in res.report.exclusion_profile.items():
            per.setdefault((s, name), []).append(st.runtime_s)
    rows = []
    for s in config.ROUTER_ACCESS_STATIONS:
        runs = {k[1]: statistics.median(v) for k, v in per.items() if k[0] == s}
        rows.append({"station": s, **{f"{k} s": f"{v:.3f}" for k, v in runs.items()},
                     "station total s": f"{sum(runs.values()):.3f}"})
    df = pd.DataFrame(rows)
    return df, statistics.median(totals), max(float(x) for x in df["station total s"])


def baseline_key_scores(plain, alts) -> tuple[pd.DataFrame, list[str]]:
    """Check 5: rows present in both runs at the preset; any change is a finding."""
    a = {s.candidate_id: s for s in plain}
    rows, findings = [], []
    for s in alts:
        if s.candidate_id not in a:
            continue
        p = a[s.candidate_id]
        fields = {
            "train": (p.candidate.planned_train.train_number if p.candidate.planned_train else "—",
                      s.candidate.planned_train.train_number if s.candidate.planned_train else "—"),
            "P(on time)": tuple("—" if x.p_on_time is None else f"{x.p_on_time:.4f}" for x in (p, s)),
            "gen_cost": tuple("—" if x.gen_cost_usd is None else f"{x.gen_cost_usd:.2f}" for x in (p, s)),
            "catch-next": tuple(" ".join(o[0].train_number or "?" for o in x.candidate.catch_next)
                                for x in (p, s)),
            "rank": tuple("—" if x.rank is None else str(x.rank) for x in (p, s)),
            "sched_rank": tuple("—" if x.sched_rank is None else str(x.sched_rank) for x in (p, s)),
        }
        changed = [k for k, (x, y) in fields.items() if x != y]
        score_changed = [k for k in changed if k not in ("rank", "sched_rank")]
        if score_changed:
            findings.append(f"{s.candidate_id}: {', '.join(f'{k} {fields[k][0]} → {fields[k][1]}' for k in score_changed)}")
        rows.append({"row": s.candidate_id,
                     **{k: (x if x == y else f"{x} → {y}") for k, (x, y) in fields.items()},
                     "scores unchanged": "yes" if not score_changed else "**no**"})
    return pd.DataFrame(rows), findings


def new_rows(alts) -> pd.DataFrame:
    """Check 6: alternative rows at the preset (chosen train and every variant)."""
    rows = []
    for s in alts:
        if not _is_alt(s):
            continue
        for v in s.variants or (s,):
            c = v.candidate
            chosen = c.variant == s.candidate.variant
            costs = c.costs
            rows.append({
                "row": v.candidate_id, "variant": f"{c.variant}{' (chosen)' if chosen else ''}",
                "train": c.planned_train.train_number if c.planned_train else "—",
                "leave": _hhmm(c.leave_home),
                "rank": "—" if s.rank is None or not chosen else s.rank,
                "sched_rank": "—" if s.sched_rank is None or not chosen else s.sched_rank,
                "P(on time)": "—" if v.p_on_time is None else f"{v.p_on_time:.2f}",
                "arr p50 / p90": f"{_hhmm(v.arrival_p50)} / {_hhmm(v.arrival_p90)}",
                "money $": "—" if v.money_usd is None else f"{v.money_usd:.2f}",
                "risk-adj $ (exp. late cost)": "—" if v.exp_late_cost_usd is None else f"{v.exp_late_cost_usd:.2f}",
                "gen_cost $": "—" if v.gen_cost_usd is None else f"{v.gen_cost_usd:.2f}",
                "pools (train: level / runs)": ", ".join(f"{u.train_number}: {u.fallback_level.value} / {u.run_count}"
                                                        for u in v.pools) or "—",
                "train taken": ", ".join(f"{k} {x:.0%}" for k, x in sorted(v.train_taken_share.items(),
                                                                          key=lambda kv: -kv[1])) or "—",
                "exposure": ", ".join(sorted(a.value for a in c.exposure or ())) or "none",
                "flags": ", ".join(sorted(v.flags)),
            })
            if costs is not None and chosen:
                rows[-1]["cost notes"] = "; ".join(
                    f"{k} {'?' if it.usd is None else f'{it.usd:.2f}'} ({it.note})"
                    for k in ("fare", "path_fare", "parking") if (it := getattr(costs, k)) is not None)
    return pd.DataFrame(rows).fillna("")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    data = load_demo_data()
    sd, dl = data.inputs.service_date, config.DEMO_DEADLINE_S
    res = route_all(data.network, sd, dl, fresh=True)
    rep = res.report
    settings = Settings()

    # Hard checks
    bad1 = check_no_excluded_stops(data, sd, dl, res)
    alts = evaluate(data, settings)
    plain = evaluate(data, replace(settings, exclusion_runs=False))
    bad2, exposure_df = check_exposure(alts, data)
    bad3, n_ref_j, n_ref_rows = check_router_v1(data)

    # 4: new keys and merged trains per station
    plain_labels = set(route_all(data.network, sd, dl, exclusions={}).options)
    key_rows = []
    for s in config.ROUTER_ACCESS_STATIONS:
        new = sorted(label for label in res.label_flags if res.options[label] and
                     res.options[label][0][0].from_stop == s)
        added = {label: n for label, n in rep.trains_added_by_exclusion.items()
                 if res.options[label] and res.options[label][0][0].from_stop == s}
        key_rows.append({
            "station": s, "new keys": ", ".join(new) or "none",
            "options in new keys": sum(len(res.options[k]) for k in new),
            "trains_added_by_exclusion": ", ".join(f"{k}: {n} ({', '.join(rep.added_trains[k])})"
                                                   for k, n in added.items()) or "0",
            "expected": "no new key" if s in HB_PATH_STATIONS else "—",
            "status": ("**unexpected new key**" if s in HB_PATH_STATIONS and new else "ok"),
        })
    keys_df = pd.DataFrame(key_rows)
    cm_st_rt_ok = not any(r["status"] != "ok" for r in key_rows)

    # 5, 6
    base_df, base_findings = baseline_key_scores(plain, alts)
    new_df = new_rows(alts)

    # 7: unscored
    uns = pd.DataFrame(rep.unscored_alternatives)

    # 8: core-claim sweep from the router (exclusion runs on)
    sw = sweep(data, settings)
    alt_ids = {s.candidate_id for s in alts if _is_alt(s)}
    sw["top is alternative"] = sw["top_rank"].map(lambda t: "**yes**" if t and t.rsplit(" (", 1)[0] in alt_ids
                                                  else "no")
    sw_plain = sweep(data, replace(settings, exclusion_runs=False))
    sw["top (router-v1)"] = sw_plain["top_rank"].values
    sw["changed vs router-v1"] = (sw["top_rank"] != sw_plain["top_rank"].values).map({True: "**yes**", False: "no"})
    sweep_findings = [f"penalty ${r['hard_penalty_usd']}, margin {r['margin_min']}: top-1 = {r['top_rank']}"
                      for _, r in sw.iterrows() if r["top is alternative"] == "**yes**"]
    sw_out = sw[["hard_penalty_usd", "margin_min", "top_rank", "top_sched_rank", "flips_option",
                 "top is alternative", "top (router-v1)", "changed vs router-v1"]]

    # 9: timing
    time_df, total_s, max_station_s = time_router(data, sd, dl)

    hard_ok = not (bad1 or bad2 or bad3)
    excl = "; ".join(f"`{n}` = no_board {sorted(e.no_board)}, no_alight {sorted(e.no_alight)}"
                     + (f", banned_routes {sorted(e.banned_routes)}" if e.banned_routes else "")
                     + (f", banned_trips {len(e.banned_trips)}" if e.banned_trips else "")
                     + (f", banned_footpaths {sorted(e.banned_footpaths)}" if e.banned_footpaths else "")
                     for n, e in config.ROUTER_EXCLUSIONS.items())
    lines = [
        f"# Exclusion runs: alternatives ({sd:%a %Y-%m-%d}, deadline {dl // 3600:02d}:{dl % 3600 // 60:02d} at NY Penn)",
        "",
        f"Generated by `python -m src.routing.alternatives` at build `{build_stamp()}`. Per access station: "
        f"one plain range-RAPTOR run plus one run per exclusion ({excl}). Options are unioned by signature "
        "and deduplicated on the exact leg tuple; a signature only an exclusion run produced is flagged "
        f"`{config.ALT_FLAG_PREFIX}<name>` and labelled \"Avoids Penn Station and the North River tunnels.\" "
        "Proxy assumption: every NJT revenue train through the North River tunnels stops at NY Penn, so "
        "banning boarding and alighting at NJT:NY bans the tunnels. "
        f"Demo preset: penalty ${settings.hard_penalty_usd:g}, margin {settings.margin_s // 60} min, "
        f"VOT ${settings.vot_usd_per_h:g}/h, N = {settings.n}, seed {settings.seed}.",
        "",
        f"## Hard checks: **{'PASS' if hard_ok else 'FAIL'}**",
        "",
        f"1. No exclusion-run journey or Option boards or alights at an excluded stop: "
        f"**{'pass' if not bad1 else 'FAIL'}** ({len(rep.exclusion_options)} scored-shape Options, "
        "plus every raw journey, unscored shapes included)." + ("".join(f"\n   - {b}" for b in bad1)),
        f"2. Exposure tagger on flagged rows reports neither NORTH_RIVER_TUNNELS nor NY_PENN (every variant, "
        f"every catch-next option): **{'pass' if not bad2 else 'FAIL'}** ({len(exposure_df)} rows)."
        + ("".join(f"\n   - {b}" for b in bad2)),
        f"3. exclusion=None identical to router-v1: **{'pass' if not bad3 else 'FAIL'}** "
        f"({n_ref_j} journeys; {n_ref_rows} scored rows = 2 sources × 12 sweep settings × every station "
        "variant, compared as strings against `eval/reference/`, written at router-v1 code)."
        + ("".join(f"\n   - {b}" for b in bad3[:30])),
        "",
        "### Exposure cross-check (check 2)",
        "",
        _md(exposure_df),
        "",
        "## 4. New keys and trains added per station",
        "",
        f"CM / ST / RT add no key: **{'yes' if cm_st_rt_ok else 'NO'}**. trains_added_by_exclusion counts "
        "exclusion-run Options merged into a key the plain run already had (not found by the plain run).",
        "",
        _md(keys_df),
        "",
        "## 5. Baseline-key scores at the preset (plain runs → with exclusion runs)",
        "",
        "Rank changes alone are expected when an alternative row is ranked above a baseline row; train, "
        "P(on time), gen_cost and catch-next must not change.",
        "",
        _md(base_df),
        "",
        "Findings: " + ("; ".join(base_findings) if base_findings else "none (every baseline key scores identically)."),
        "",
        "## 6. New rows scored at the preset",
        "",
        "Risk-adjusted cost = expected lateness cost (VOT × E[late] + penalty × P(late)); gen_cost = money + "
        "time cost + risk-adjusted cost. Pools are alight-anchored (the TS → HB leg uses runs observed at HB).",
        "",
        _md(new_df),
        "",
        "## 7. Unscored alternatives",
        "",
        "Exclusion-run journeys whose shape the scorer cannot simulate. Listed, never dropped silently.",
        "",
        _md(uns),
        "",
        "## 8. Core-claim sweep from the router (exclusion runs on)",
        "",
        _md(sw_out),
        "",
        "Findings (alternative is top-1): " + ("; ".join(sweep_findings) if sweep_findings else "none."),
        "",
        "## 9. Router time per query (standalone)",
        "",
        f"All stations, plain + exclusion runs, median of {TIMING_REPEATS} fresh queries after one warm-up: "
        f"**{total_s:.3f} s** per query (target < 1 s: **{'met' if total_s < 1 else 'NOT met'}**), "
        f"max per station {max_station_s:.3f} s. Per-run times are RAPTOR only; the query total includes "
        "adapting journeys to Options.",
        "",
        _md(time_df),
        "",
    ]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {REPORT_MD}: hard checks {'PASS' if hard_ok else 'FAIL'}")
    assert not bad1, f"check 1 failed: {bad1}"
    assert not bad2, f"check 2 failed: {bad2}"
    assert not bad3, f"check 3 failed: {bad3[:10]}"


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
