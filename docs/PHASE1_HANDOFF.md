# Crossing: Phase 1 handoff

*Sat 2026-10-10, about 18:45 ET. Read this with `CLAUDE.md`, `IMPLEMENTATION_PLAN.md` (decision log at
the end) and `README.md` before starting Phase 2. **Start with §0.***

---

## 0. Read first

**Phase 1 is complete and tagged `safety-net-v2` (`ccbc984`, = `master`).** The safety-net demo runs
offline end to end: plan → price → tag → Monte Carlo score → Streamlit table.

**The core claim holds, but at the train level, not the station level** (build `98fc4b2`,
`eval/core_claim.md`):
- Comparing fairly (best of the last 3 on-time trains per station), **the station never changes
  (0 / 12 sweep settings) but the train does (9 / 12: every setting with a lateness penalty ≥ $25)**.
- Demo preset (Tue 10-13, arrive NY Penn by 09:00, VOT $30/h, penalty $75, margin 5 min):
  **NP → NY.** Timetable plan = train 3926, leave 07:56, on time 30 / 100. Risk-aware plan = **3224,
  leave 07:40 (16 min earlier), on time 99 / 100.**
- Mechanism: NY arrivals ran ~3.5 min late on Fri 10-09; the latest-train plan (3926, arr 08:59)
  has zero destination slack.
- **Pitch line:** "the timetable says leave 07:56; risk says leave 07:40 and you're on time 99
  mornings of 100, not 30."

**Two things are still open before presenting:**
1. **Published-timetable check (check A, H1 vs H2).** 3224 and 3828 are among the runs whose feed
   schedule at NY is exactly +780 s vs GTFS. Look up NJ Transit's published NY arrival for 3224 / 3828
   and compare with GTFS. Until then quote ranges: under H2 (`delay_vs_gtfs_s` at NY) NP 3926 / 3828 /
   3224 on time = 0.20 / 0.51 / 0.86 vs 0.30 / 0.84 / 0.99 under H1. **The recommendation (NP on
   3224) is the same under both.**
2. **Demo backup:** screenshots + a 60-second recording of the demo preset (the in-app browser's
   screenshots time out here; the user records it).

---

## 1. Status at a glance

| Step | Commit | Result |
|---|---|---|
| Check A (delay field) | — | Feed-relative `delay_s` at NY, `delay_vs_gtfs_s` elsewhere (provisional) |
| 1.1 Delay pools | `cdd80b2` | Superseded by 1.8 A (alight-anchored) |
| 1.2 Drive cache | `9bdc04e` | 252 Routes API calls, all cached; request time is lookup only |
| 1.3 Candidates | `5a1adaa` | Station policies with planned train, leave_home, catch-next list |
| 1.4 Pricer + exposure | `e2ff9d7` | Sourced `CostItem`s, round-trip day; exposure from `infra.csv` |
| 1.5 Scorer | `0efed9b` | Monte Carlo, common random numbers, gen_cost, rank vs sched_rank |
| 1.6 Streamlit | `05e457e` | `streamlit run app/app.py`, offline, reruns < 1 s |
| 1.7 Review fixes | `d6fa750` | PATH $0 cross-honor, ST/RT + PATH rows, planned-train rule, … |
| L1b + RT diagnostic | `791d34d` | **tag `safety-net`** (Phase 1 checkpoint) |
| 1.8 Train-consistent pools + train choice | `641e227` | Alight-anchored pools; 3 train variants per station |
| 1.9 Pre-tag checks | `98fc4b2`, `ccbc984` | **tag `safety-net-v2`** |

**Tests:** 91 pass (`python -m pytest tests -q`, with `CROSSING_DATA_DIR` set; the golden / app
tests skip without data).

**Git:** branch `claude/phase-1-delay-checks-e1c9b3` = `origin/master` = `ccbc984`. All pushed. The main
checkout's local `master` is behind: `git pull` there first.

---

## 2. Environment

- `.venv` (main checkout): Python 3.13.9, uv. Pinned list in `requirements.txt` (pandas 3.0.6, numpy
  2.5.3, pyarrow 25.0.1, streamlit 1.65.0, tzdata, requests, python-dotenv, pytest). Install with
  `uv pip install --python .venv -r requirements.txt`.
- **Worktrees have no `data/`** (git-ignored): set `CROSSING_DATA_DIR=C:/Users/anton/PycharmProjects/HackathonNJT/data`.
- Set `PYTHONIOENCODING=utf-8` on Windows (→, ×, – in logs).
- `.env` (main checkout): `GOOGLE_MAPS_KEY` (Routes API, build time only), `GEMINI_AI_KEY` (Phase 4).
- `.claude/launch.json` (worktree, git-ignored) starts the app in the preview pane as `crossing-app`.
- pandas 3: string columns are `str` dtype; `.map()` keeps it (use `.str` ops for booleans).
- Bash heredocs with nested quotes broke twice; write patch scripts to the scratchpad instead.

---

## 3. Commands

```bash
python -m src.build                        # raw archive + GTFS → observations, pools
python -m src.build.pools                  # pools + eval/pool_evidence.md (+ RT diagnostic; --no-diagnose)
python -m src.build.drive_cache --offline  # drive_cache.parquet from cached raw responses (no network)
python -m src.build.drive_cache            # network: fills missing Routes API calls only
python -m src.safety_net --deadline 09:00  # candidates, costs, exposure (CLI)
python -m src.scoring                      # demo table + sweep → eval/core_claim.md (git-stamped)
python -m streamlit run app/app.py         # the demo (offline)
python -m pytest tests -q
```

To regenerate `core_claim.md` with a clean stamp: commit code first, then run `python -m src.scoring`
and commit the file.

---

## 4. Request path (what each module does)

`src/scoring/pipeline.py` is the single entry point used by both the CLI and the app:
`load_demo_data()` (GTFS, price tables, indexes: `index_pools`, `index_drive_cache`, `index_infra`,
`station_options`) → `simulate(data, settings)` → `reprice(scored, settings, inputs)` →
`sweep(data, settings, simulate_fn=…)`. `build_stamp()` = short git hash (+ `-dirty`).

| Module | Role |
|---|---|
| `src/safety_net/candidates.py` | `station_options` (deadline-independent GTFS scan), `plan_variants` (last `TRAIN_CHOICE_T`=3 on-time trains per station), `plan_station`, `solve_leave_home` (fixed point + bisection guard), `drive_all_the_way` |
| `src/pricing/pricer.py` | `price_candidate`: fare, PATH fare (dated rule), parking (`non_resident_from`), tolls, fuel. Lookups memoized on `PriceTables.memo` |
| `src/pricing/exposure.py` | `tag_candidate`: infra.csv by (from, to, route pattern); PATH static fallback; `exposure_varies` |
| `src/scoring/drive.py` | Drive lookup (interpolate between 15-min slots; nearest + flag outside), triangular sampling |
| `src/scoring/pools.py` | Alight-anchored pools: `resolve`, `sample`, `level_counts`, `period_of`, `PoolIndex` (memoized) |
| `src/scoring/scorer.py` | `score_candidate` (vectorized MC), `choose` (train choice per station), `rank_options`, `reprice` (exact VOT / penalty re-rank) |
| `src/scoring/__main__.py` | CLI table + sweep → `eval/core_claim.md` |
| `app/app.py` | Streamlit UI; caches `demo_data()` (resource) and `simulate(deadline, margin, mpg)` (data) |

### Scorer, per draw (N = 2000, seed 42)
1. Shared u_i → drive_i = triangular inverse CDF over (OPTIMISTIC, BEST_GUESS, PESSIMISTIC) at
   leave_home; ready = leave_home + drive + station buffer.
2. Each catch-next train draws (board, alight) from one whole run; rng keyed by **(seed, train
   number)**, so a train draws identical runs in every candidate / variant. board = max(0, board).
3. Catchable if ready ≤ sched_dep + board; rider takes the catchable train with the earliest
   **scheduled** arrival; nothing catchable → deadline + 60 min (censored).
4. Hoboken + PATH: HB actual arrival + 5 min → next scheduled PATH → 33rd St + 4 min walk.
5. `gen_cost = money + VOT × (deadline − leave_home) + mean(VOT × L + penalty × 1(L > 0))`.
6. `choose`: per station keep the lowest-gen_cost variant; `sched_rank` uses the **latest-train
   variant's** money + time (timetable baseline). Infeasible rows rank last; drive-all-the-way is an
   unranked reference row.

### Pools (1.8 A, replaces 1.1 / L1b)
- Population for a rail leg = runs of line × toward_ny × period **observed at the alight stop**;
  boarding delay = that run's observed delay, else 0 (`PoolUsage.board_zero_share`).
- Fallback (≥ `POOL_MIN_RUNS` = 5): L1 line × toward_ny × period → L2 toward_ny × period → L3
  toward_ny → L4 zeros (flagged). Weekday runs only.
- Period = the run's scheduled arrival at its GTFS terminal (NY local): early < 06, am_peak 06–10,
  midday 10–16, pm_peak 16–19:30, evening ≥ 19:30. The scorer asserts the leg alights at the trip
  terminal (NY / HB). **Phase 2 hazard:** a router leg ending mid-trip raises `NotImplementedError`.
- Delay field: `delay_s` (SEC_LATE) at NY, `delay_vs_gtfs_s` elsewhere (`config.TERMINAL_DELAY_SOURCE`).

---

## 5. Contracts (`src/contracts.py`; changed in Phase 1)

- **Leg:** + `toward_ny` (required for rail). Stop ids: NJT 2-char codes, `PATH:<stop_name>`,
  `ORIGIN`, **`LOT:<dest>`** (drive endpoint, then a WALK leg = station buffer).
- **CostItem** (usd | None, source_url, checked_date, note; None needs a note, a price needs a source)
  and **CostBreakdown** (fare, path_fare, parking, tolls, fuel; `priced`, `flags`, `partial`,
  `total_usd`, `notes`). Basis: one commuting day, round trip.
- **Candidate:** costs / exposure `None` until priced / tagged; + `catch_next` (planned option first),
  `leave_home`, `infeasible` + reason, `label`, **`variant`** (0 = latest on-time train), `flags`, `notes`.
  Legs may be empty only if infeasible.
- **InfraAsset:** `HACKENSACK_PORTAL`, `NORTH_RIVER_TUNNELS`, `NY_PENN`, `PATH_TUNNELS`.
- **FallbackLevel:** L1 / L2 / L3 / `NO_DATA` (alight-anchored semantics).
- **DelaySamples:** draws × stops, no NaN, explicit `run_count` (distinct runs ≠ draws).
- **PoolUsage:** + `delay_field_at_stop`, `board_zero_share`, `train_number` (set by the scorer).
- **ScoredOption:** p_on_time, arrival p50/p90, expected_late_min, censored_share,
  train_taken_share, money, time_cost, exp_late_cost, gen_cost, rank, sched_rank, pools, flags,
  assumption_flags, seed, N, **`variants`**, **`sched_basis_usd`**. Simulation fields None without legs.

**For Phase 2:** the router must emit the same `Candidate` (with `catch_next`, `leave_home`,
`variant`, `LOT:` access legs) so `price_candidate`, `tag_candidate`, `score_all` and the UI work
unchanged. `plan_station` / `plan_variants` already turn a list of onward options into candidates:
the cleanest integration is for RAPTOR to produce **options** (tuples of legs from the boarding stop)
per access station and reuse `plan_variants`.

---

## 6. Data (git-ignored, local to this machine; edits made in Phase 1)

`data/reference/` (backups of edited files are in the session scratchpad):

| File | Phase 1 change |
|---|---|
| `demo_origin.csv` | **New.** Personal demo origin (Chatham). Never commit. |
| `drive_destinations.csv` | **New.** CM Lot 1, ST Lot 14, RT Lot 10, SE ParkFast, NP Gateway One, NYG Meyers (Midtown), OSM coordinates + source |
| `parking.csv` | SE $30 weekday (both lots; weekend $16, event $68); NP 986 Raymond Blvd $13 (cheapest), 160 Edison Pl $21 / early bird $19; RT Lots 1–3 residents only, Lot 4 non-residents from 12:00 (`non_resident_from` column). All user-provided 10-10 |
| `njt_fares.csv` | + RT → HB $9.10 via ND, $10.30 via SE (user-provided) |
| `path_fares.csv` | + `portal_cutover_cross_honor` rule: $0, 2026-10-11 – 11-14, weekdays, `lines` 7;8;2;3, rail leg must end at HB; source = NJT release 2026-09-17 |

`data/artifacts/`: `pools.parquet`, `drive_cache.parquet` (+ Phase 0 files). `data/cache/routes_raw/`:
252 raw Routes responses (the real drive cache; never delete).

---

## 7. Findings to carry forward

- **Portal cutover (10-11 – 11-14):** weekday M&E / Gladstone / Montclair-Boonton end at Hoboken; CM /
  ST / RT have no one-seat ride to Penn (UI shows one notice). PATH cross-honored at $0 (rule above).
- **Check A:** 26 / 206 NY-terminating runs have the feed schedule +780 s vs GTFS at NY only; HB has
  none. Current choice is H1 (artifact → `delay_s` at NY). 3224 / 3828 are affected; the NY pools are
  11 / 27 (NEC) and 4 / 12 (NJCL) affected. Published-timetable check still due (§0).
- **Short Hills:** in the AM-peak M&E cell only 2 of 7 runs stop at RT (expresses skip it); every
  scheduled RT stop was observed. A non-resident has no morning parking (Lot 4 from 12:00), so
  RT → HB + PATH is infeasible (`parking_unavailable` ranks last; one-line revert in `score_candidate`).
- **SE → NY has no Portal exposure** (Secaucus is east of the bridges); NP → NY does.
- **CM → HB fare = CM → NY fare ($13.05)**, so with PATH $0 the Hoboken route costs the same as the
  (suspended) direct fare.
- Midtown toll $26.17 may include the congestion charge (drive-all-the-way is not priced anyway).
- Pools rest on about one weekday (Fri 10-09): 7 runs M&E → HB, 12–27 runs per NY pool.

---

## 8. Known gaps / cleanup (not blocking)

- `config.PATH_ASSUMED_HEADWAY_S` and `LATENESS_BUCKETS_MIN` are unused since 1.5 (the scorer uses the
  real PATH timetable and gen_cost). Remove or wire up.
- Fare sources: most `njt_fares.csv` rows are `njtransitapp` with no `checked_date` (flagged in notes).
- NP parking: the drive cache routes to Gateway One, the cheapest lot is 986 Raymond Blvd (noted).
- First UI rerun after a deadline change is ~0.96 s (19 variants + 3 sweep simulations); close to the
  1 s target.
- `evaluate` re-plans every variant per deadline; fine now, revisit if the router adds many options.
- Weekend robustness and the evaluation tasks (CLAUDE.md §6) are not started.

---

## 9. Phase 2 (mcRAPTOR) starting points

- Start from `safety-net-v2`. Go / no-go stays at H10 (22:00); if the router is not right by then, the
  safety net is the demo.
- Network: `src/gtfs.py` (`load_feed`, `active_trips`, `stop_pairs`) is network-agnostic; keep RAPTOR
  free of NJ logic. Service resolution by date from `calendar_dates` (NJT has no calendar.txt); PATH
  uses calendar.txt. Ignore trips ≥ 24:00 for the AM window (NJT after-midnight convention is
  inconsistent, Phase 0 §5.2).
- Transfers: `config.TRANSFER_MIN_S` (HB NJT → PATH 5, 33rd St → Penn 4, Secaucus 6, NP NJT ↔ PATH 5);
  `transfers.csv` does not exist yet.
- Golden tests to write (IMPLEMENTATION_PLAN 2.3): the safety-net plans (SE 3926 / NP 3224 / CM → HB +
  PATH 872) must appear in the router's Pareto set; Hoboken → PATH → 33rd St → walk respects transfer
  times; no dominated journey in the set.
- Keep the scorer's terminal-alight assertion in mind (§4) if the router produces legs that alight
  mid-trip (e.g. a Secaucus transfer).
