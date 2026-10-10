# IMPLEMENTATION_PLAN.md — Crossing

Build plan for HackRU Fall 2026. Read alongside `CLAUDE.md` (architecture, contracts, conventions, what not to do).

**Timing assumption:** about 3h of the 24h went to design. This plan budgets **~21h of build time**, counted from **H0 = build start (Sat 10-10, ~12:00 ET)**. Fill in the real submission deadline below and rebase if it differs.

- Submission deadline: `Sun 2026-10-11 10AM` (fill in)
- Demo / presentation: Sun 2026-10-11
- Demo **query** scenario: Mon 2026-10-12, AM peak, origin near Chatham, destination NY Penn

**If time runs short, cut in this order:** Phase 5 (stretch) → Phase 4 (alerts) → map polish in Phase 3. Never cut Phase 1 or Phase 6.

---

## Overview

| Phase | Hours | Goal | Exit checkpoint |
|---|---|---|---|
| 0 | H0–H1.5 | Foundation: contracts, config, reference data, clean + join | Join rate ≥99% per line, logged |
| 1 | H1.5–H4 | **Safety net**, end to end | **Ranked table for the demo scenario renders** |
| 2 | H4–H8 | Router: RAPTOR per access station + Pareto merge | Golden tests pass |
| — | **H8** | **Go / no-go on the router** | Decision recorded |
| 3 | H8–H11 | UI: full Streamlit, map, exposure, confidence | Demo-able end to end |
| 4 | H11–H12.5 | Gemini alerts (hard timebox) | Accuracy on labeled set reported |
| — | H12.5–H16 | Sleep | — |
| 5 | H16–H18 | Stretch: outage toggle, then persistence nowcast | Optional |
| 6 | **H18** | **Feature freeze**, refresh data, evaluation | Results table |
| 7 | H18–H21 | README, backup video, pitch, offline check | Submitted |

---

## Phase 0 — Foundation (H0–H1.5)

**0.1 Housekeeping (10 min)**
- Check whether EC2 is writing bus feeds. If not, fix the poller now (irrecoverable data loss otherwise). If only rsync skipped them, defer.
- Repo skeleton per `CLAUDE.md` §7; `.gitignore` for `data/artifacts/`, `.env`.
- Done when: skeleton committed; bus feed status known.

**0.2 Config and contracts (20 min)**
- One config module: paths, N samples, seed, period boundaries, fallback threshold (~8 runs), transfer minimums, drive-variance assumption, lateness buckets.
- Contracts module: Leg, Candidate, DelaySamples, ScoredOption (`CLAUDE.md` §5).
- Done when: contracts importable; one hand-built Candidate constructs without error.

**0.3 Reference data (30 min, can overlap with 0.4)**
- Price CSVs with `source_url`, `checked_date`:
  - NJ Transit O–D fares: each of the six stations → NY Penn; one-way, off-peak, weekly, monthly.
  - PATH flat fare.
  - Parking: one row per lot at each station (`daily_rate`, `permit_only`, `operator`).
  - Tolls fallback table; Manhattan driving costs (tunnel toll, garage, congestion charge); gas price.
- Transfers file: Hoboken drive → PATH; PATH 33rd St → Penn walk; Secaucus minimum; Newark Penn NJ Transit ↔ PATH.
- Infrastructure map: segments → Portal Bridge / North River tunnels / Penn Station / PATH tunnels.
- Done when: every row is dated and sourced; no permit-only lot is the only option at any station.

**0.4 Clean + join pipeline (40 min)**
- Snapshot filters (low count, stale). NY local → UTC. Dedupe.
- Join with leading zeros stripped; adjacent-service-day retry; GTFS canonical line + direction.
- Observations table: run × stop, scheduled time, delay.
- Done when: per-line join rate logged and ≥99%; dropped-snapshot counts logged; the Fri 16:03–16:07 glitch is filtered.
- Test: zero-padded vs. unpadded train numbers match.

---

## Phase 1 — Safety net (H1.5–H4) ★ most important phase

**1.1 Delay pools (40 min)**
- Pools keyed line × toward_ny × period on `delay_vs_gtfs_s`, storing **whole runs**.
- Fallback hierarchy with level + run count recorded.
- Done when: Morris & Essex inbound AM peak pool has ~15+ runs; a deliberately empty cell falls back correctly.

**1.2 Drive cache (20 min)**
- Routes API: origin → each of six stations, and origin → Manhattan garage, for Mon 10-12 departures across 06:30–08:30.
- Cached to JSON. Request path reads cache only.
- Done when: the app runs with the network disabled.

**1.3 Hard-coded candidates (30 min)**
- For the demo window, hand-pick one representative journey per station (from the new-timetable GTFS), plus drive-all-the-way.
- Emitted as Candidates with `source = "safety_net"`.
- Done when: 7 Candidates (6 stations + drive) exist with correct legs and scheduled times.

**1.4 Pricer + exposure tagger (20 min)**
- Itemized base cost per Candidate; exposure tags from the infrastructure map.
- Done when: costs reconcile by hand for two stations; Hoboken shows no North River tunnel exposure.

**1.5 Scorer (40 min)**
- Monte Carlo per `CLAUDE.md` §4.8: drive sample, whole-run rail sample, missed-train → next departure, transfer cascade, PATH assumption.
- Risk-adjusted cost from user lateness settings.
- Done when: fixed seed gives identical output twice; P(on time) moves in the expected direction when the deadline is tightened.

**1.6 Minimal UI (20 min)**
- Streamlit page: inputs + ranked table (P on time, p50/p90, base cost, risk-adjusted cost, exposure).
- **Checkpoint:** working demo exists. Commit and tag `safety-net`.
- **Check the core claim:** is there a deadline / lateness setting where the risk-adjusted ranking differs from the scheduled-time ranking? If not, note it now; it shapes the pitch.

---

## Phase 2 — Router (H4–H8)

**2.1 Network build (60 min)**
- Load NJ Transit rail (new timetable, Mon 10-12 service) + PATH GTFS into RAPTOR arrays (port from Tetsudle).
- Add transfers file. Handle GTFS times past 24:00.
- Done when: stop/route/trip counts logged; Penn and 33rd St reachable.

**2.2 Per-station runs + merge (60 min)**
- Run RAPTOR (arrival, transfers) once per station, seeded with cached drive arrival.
- Price each journey post-routing (4.6), tag exposure, merge, Pareto-filter on (arrival, cost, transfers).
- Emit Candidates with `source = "router"`.

**2.3 Golden tests (40 min)**
- A known Chatham → Penn Midtown Direct train appears.
- Hoboken → PATH → 33rd St → walk → Penn appears with correct transfer times.
- A Secaucus transfer journey respects the minimum transfer time.
- No journey in the Pareto set is dominated by another.

**2.4 Integration (40 min)**
- Router Candidates flow through the same scorer and UI as the safety net. A toggle switches source.

### H8 go / no-go

**Go** if all golden tests pass and the router's options for the demo scenario include (or beat) the safety-net journeys.
**No-go** otherwise: the safety net is the demo. Keep router code on a branch; mention it as "engine in progress" in the pitch. Do not keep debugging past H8.

---

## Phase 3 — UI (H8–H11)

- Ranked cards/table with itemized cost breakdown (fuel · tolls · parking · fare).
- Confidence indicator: pool fallback level + run count.
- Assumption flags: "PATH: assumed, not measured"; drive variance assumed.
- Exposure badges per option.
- pydeck map: origin, six stations, rail/PATH lines, selected option highlighted.
- A "scheduled-time ranking vs. risk-adjusted ranking" comparison view (the demo's key moment).
- Done when: full demo walkthrough works in under 2 minutes, offline.

---

## Phase 4 — Alerts (H11–H12.5, hard stop)

- Gemini structured-output parse over archived station messages; cache results.
- Hand-label ~20 messages; report extraction accuracy per field.
- Flag Candidates on affected lines in the UI.
- At H12.5, stop regardless of state. If incomplete, drop from the demo.

---

## Sleep (H12.5–H16)

Before sleeping: commit, push, confirm the `safety-net` tag runs from a clean checkout.

---

## Phase 5 — Stretch (H16–H18, in this order)

1. **Tunnel-outage toggle:** rerun the router excluding trips through North River tunnels / Portal Bridge; show the best alternative and its cost. (Requires router "go".)
2. **Persistence nowcast:** if a candidate train is already running late, shift its samples by the current delay.

---

## Phase 6 — Feature freeze + evaluation (H18)

- Rsync the latest archive; rerun the full build.
- **Task 1 (pre-trip):** train Thu evening + Fri < 12:00, test Fri ≥ 12:00. MAE vs. schedule and pooled baselines; calibration in 3–4 bins.
- **Task 2 (nowcast):** upstream delay → Penn delay; vs. persistence and schedule.
- **Weekend robustness:** reported separately.
- One "finding from the data" for the pitch (e.g., how often Penn arrivals exceed X min late in the AM peak, by line).
- Output: a results table saved to `eval/` and screenshot-ready.

---

## Phase 7 — Ship (H18–H21)

- README: problem, how it works, results table, limitations, Japan transferability, roadmap.
- Record a backup demo video (in case live demo fails).
- Pitch practice: one sentence → problem → live demo → how it works → one finding → Japan precedent → next steps.
- **Offline check:** run the full demo with Wi-Fi off.
- Submit with margin. Do not submit in the last 15 minutes.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Router incorrect at H8 | Medium | Safety net is demo-complete by H4; go/no-go is binding |
| Sparse pools give unstable estimates | High | Fallback hierarchy; show run counts honestly |
| GTFS lacks Mon 10-12 calendar | Low | Verify in Phase 0; fall back to the Fri 10-09 schedule with a stated caveat |
| Routes API quota / failure | Low | Build-time cache; request path never calls it |
| Gemini output unreliable | Medium | Hard timebox; report accuracy; drop if poor |
| Venue Wi-Fi fails | Medium | Offline-capable request path; backup video |
| No ranking flip in the demo scenario | Medium | Check at Phase 1.6; adjust deadline / lateness defaults or pick a scenario where it occurs, and say so honestly |
| Fatigue errors late | High | Sleep block is mandatory; feature freeze at H18 |

---

## Decision log

| Decision | Choice | Why |
|---|---|---|
| Destination | NY Penn only | Scope; strongest corridor story |
| Access stations | Chatham, Summit, Short Hills, Secaucus, Newark Penn, Hoboken | Local options + tunnel-avoiding PATH option |
| Demo query | Mon 10-12 AM peak | Next weekday commute after Sunday demo; new timetable |
| Pooling | line × direction × period, whole runs | Sparse data; timetable change; preserves stop correlation |
| Router | RAPTOR per station, cost post-routing | O–D fares are non-additive; simple and correct |
| Quantile LightGBM | Cut | ~1.5 weekdays of data |
| PostGIS | Cut | No visible value at this scale |
| PATH reliability | Assumed on schedule + headway penalty | No PATH data collected |
| Evaluation | Time-block split + weekend robustness | Avoid leakage; honest framing |
| Delay measure (review 10-10 14:10) — **superseded by check A below** | Pools and scorer use `delay_vs_gtfs_s` only | 25 NY arrivals carry a feed schedule exactly 13 min later than GTFS. GTFS arrival_s == departure_s at NY (all 1,467 NY stop_times), so arrival_s changes nothing. Last-seen time in the feed confirms the trains really ran ~12 min behind the published time (median last_seen − GTFS: 11.8 min affected vs 0.1 min unaffected; last_seen − feed estimate −2.2 min). |
| Pool direction key | `toward_ny` = trip terminal ∈ {NY, HB}, not `direction_id` | NJT direction 0 also includes trips ending at Newark Penn, Long Branch, Atlantic City, Princeton (95 of 420 dir-0 runs) |
| One delay per stop | Approach delay: last record with NEXT_STOP = stop; also used as the departure delay | Station-snapped feed has no separate departure event; dwell not modeled. Stated limitation |
| Observation row selection | Positional last row per (service day, train, stop) | `groupby().last()` takes last non-null per column and can splice fields from different records |
| Stop IDs in legs | NJT 2-char codes; PATH `PATH:<stop_name>`; Secaucus = SE; explicit WALK legs between systems | Same keys as infra.csv; Candidate enforces contiguous legs |
| Scorer sampling (Phase 1.5) | Sample only runs observed at both boarding and alighting stops; report that count in `PoolUsage.run_count` | Not every stop of every run is observed; an unobserved stop must not become a silent zero |
| Delay measure (check A, 10-10 ~14:45; PROVISIONAL) | `delay_used_s` = `delay_s` (SEC_LATE) at NY, `delay_vs_gtfs_s` elsewhere incl. HB; `config.TERMINAL_DELAY_SOURCE`; recorded per stop in `PoolUsage.delay_field_at_stop` | SEC_LATE is signed (min −1,089 s, 15.4% negative): not clamped. 26/206 NY-terminating runs have a feed schedule exactly +780 s vs GTFS at NY only; `delay_vs_gtfs_s` jumps +766 s (median) from the previous stop while SEC_LATE moves +2 s. GTFS arr = dep at NY (1,467/1,467). HB: 0/150 runs offset. Affected runs cluster Fri AM peak 07:17–09:24 (16 of 59 weekday AM-peak NY arrivals), which keeps H2 (real padded schedule) open. The earlier "last seen" evidence is withdrawn: the feed drops trains at its own estimate, so it is not independent. To finalize: check NJT published times for 2–3 affected trains (e.g. 3124, 3712, 3888) |
| Pool periods (1.1) | early <06, AM peak 06–10, midday 10–16, PM peak 16–19:30, evening ≥19:30, from the run's scheduled terminal arrival (NY local) | One period per run, so all of a run's stops share a pool cell |
| Pool fallback (1.1) | Min 5 effective runs. L1 line × toward_ny × period (both stops observed) → L2 toward_ny × period (alight observed, board delay 0) → L3 toward_ny → L4 zeros + UI warning | ~1 weekday of data; 8 left most demo cells empty |
| Pool service days (1.1) | Weekday runs only | Weekend runs are the robustness set (§6); Sat AM-peak runs would dilute Monday's cells |
| M&E vs Gladstone into HB (1.1) | Not pooled together; separate L1 cells | Both reach L1 at Summit (7 / 6 runs); revisit only if a cell falls back |
| Demo date (1.2, 10-10 ~15:00) | **Tue 10-13** AM peak, deadline 09:00 at NY Penn; supersedes Mon 10-12 | Avoids Columbus Day; same Portal Cutover weekday schedule |
| Drive cache (1.2) | 6 destinations (lots from OSM + Midtown garage) × 14 slots × 3 traffic models = 252 Routes calls, raw JSON per call, skip-if-cached | Triangular(OPTIMISTIC, BEST_GUESS, PESSIMISTIC) replaces the lognormal σ = 0.15 assumption: spread comes from Google, not invented |
| Demo origin storage | `data/reference/demo_origin.csv` (git-ignored) | Personal location; repo is on GitHub |
| Safety-net candidates (1.3) | 7 station policies: CM/ST/RT/SE/NP → NY, CM → HB + PATH, drive all the way (not priced). Planned = latest option arriving ≤ deadline; leave_home by fixed point on BEST_GUESS drive; catch-next K = 4 | On Tue 10-13 CM/ST/RT → NY have no trains (Portal Cutover; 0 vs 53 on Fri 10-09): emitted as infeasible, so the UI shows why. CM → HB + PATH is the Chatham rail option |
| Buffers (1.3) | Margin 5 min; station buffer CM/ST/RT 4, NP 6, SE 8; HB → PATH 5; 33rd St → Penn 4 (was 10); garage → Penn 5 | Estimates, flagged in the UI |
| Cost basis (1.4) | One commuting day, round trip: 2 × one-way fare (+ 2 × PATH), daily parking, 2 × toll, 2 × fuel | Answer (a). Passes deferred to Phase 3 |
| Exposure names (1.4) | `HACKENSACK_PORTAL` (was portal_bridge), `NY_PENN` (was penn_station) | Portal North Bridge is in service; the asset is the Hackensack crossing |
| SE → NY exposure (1.4) | {NORTH_RIVER_TUNNELS, NY_PENN}, no Portal; kept as the data says | Spec expected Portal for #1–5; infra.csv (GTFS shapes × OSM gates) has portal_bridge = 0 on every SE → NY pattern, and Secaucus is east of the bridges. Investigated, not overridden |
| Parking rates (1.4, user-provided 10-10) | SE $30 weekday (both lots; weekend $16, event $68); NP 986 Raymond Blvd $13 flat (cheapest), 160 Edison Pl $21 standard / $19 early bird | Closes the SE / NP partial totals; source = user, dated 10-10 |
| Scorer (1.5) | Common random numbers for drive; whole-run delay per catch-next train; earliest-scheduled-arrival catch rule; PATH on schedule; gen_cost = money + VOT × door-to-deadline + E[VOT × L + penalty × 1(L>0)] | Spec 1.5. Replaces the bucketed cost_of_lateness |
| leave_home guard (1.5) | After the fixed point, bisect for the latest departure that parks on time | With margin 0 the 30 s tolerance let the drive overrun the walk start |
| Core claim (1.5, 10-10) | **Holds** at the demo preset: rank 1 = CM → HB + PATH, sched_rank 1 = NP → NY. Flips in 6 of 12 sweep settings (every margin at penalty ≥ $75; none at $0 / $25) | Driven by NP / SE planning 3926 (arr 08:59, 1 min slack) with ~3–4 min typical Penn delay. Caveat: CM → HB has 5 effective runs, so its P(on time) = 0.80 is 4 of 5 runs |
| Phase 1 review fixes (1.7) | C1 PATH $0 cross-honor rule (dated, sourced); C2 ST/RT → HB + PATH candidates (M&E + Gladstone), direct M&E → NY infeasible as "Midtown Direct diverted to Hoboken (Portal cutover)"; C3 pre-cutover banner; C4 Portal time-scoped framing; F1 planned = latest on-time departure; F2 ScoringInputs typed/indexed; F3 delay draws keyed by (seed, train number), drawing run ids; L1–L5 | Code review 10-10 |
| Short Hills data (1.7, user-provided 10-10) | RT → HB $9.10 via ND / $10.30 via SE; Lots 1–3 residents only; Lot 4 non-residents only from 12:00 (`parking.csv` `non_resident_from`) | Demo origin is in Chatham, so a non-resident has no morning parking at Short Hills |
| Known-unavailable parking (1.7) | Lots exist but none usable by this rider at the parking time → `parking_unavailable`, ranked as infeasible (last); not-collected parking stays `no_daily_parking` (partial, ranked) | Otherwise RT → HB + PATH ranked #1 on a missing parking cost. Revert = drop the one line in scorer.score_candidate |
| Core claim after 1.7 (10-10) | **Holds** at the demo preset: rank 1 = ST → HB + PATH, sched_rank 1 = NP → NY. Flips in 8 of 12 sweep settings (every margin at penalty ≥ $75; margins 0 and 5 at $25; none at $0) | NP / SE plan 3926 (1 min slack); Hoboken options have ~5 min PATH slack and PATH is $0 under cross-honoring. Caveats: CM → HB 5 runs, ST → HB 7 runs; PATH delays and Hoboken crowding not modeled (optimistic for Hoboken) |
| Pool fallback L1b (evidence review, 10-10) | L1 → **L1b** (same line × toward_ny × period, alight observed, board 0) → L2 → L3 → L4 | RT→HB fell to L2 (49 runs, p50 0.3 min) by mixing Main/Bergen/Pascack/MOBO arrivals at HB vs 3.8–4.1 min for M&E. Now L1b, 7 runs, p50 3.8 min (= ST→HB). RT diagnostic: 51 of 52 archived ST+HB runs that schedule RT have an RT record (the 1 missing is overnight train 442 on 10-08: no NEXT_STOP=RT record); in the AM-peak M&E cell only 2 of 7 runs stop at RT (the rest run express). Thin L1 = timetable, not lost data. Details in eval/pool_evidence.md |
| Train-consistent pools (1.8 A) | Pools anchored on the alight stop (line × toward_ny × period runs observed there); board delay observed or 0 (`board_zero_share`); fallback L1 → L2 → L3 → L4; draws keyed by (seed, train) over sorted run ids. Replaces L1/L1b | The same train (872 from CM/ST/RT; 3926 from SE/NP) now draws identical runs; p_on_time differs only through drive and catching. M&E → HB = 7 runs (zero-filled board: CM 29%, ST 0%, RT 71%) |
| Train choice (1.8 B) | Plan the last 3 on-time trains per station; pick the lowest gen_cost; sched_rank on the latest-train variant | Answers "which train should I take". Demo preset: NP picks 3224 (dep 08:22, on time 0.99) over 3926 (0.30); SE picks 8624 (0.92) over 3926 (0.29) |
| Core claim after 1.8 (10-10) | **Holds at the option level, not the station level.** Top option (station + train) differs from the timetable plan in 9 of 12 sweep settings (every setting with penalty ≥ $25; none at $0); the station alone differs in 0 of 12: NP → NY wins both rankings once it may take an earlier train | Pitch: "risk changes which train you take (leave 16 min earlier for 3224), not where you drive". Earlier station-level flip (HB + PATH) came from forcing the latest train |
| Cutover pricing (1.8 C) | Rule needs rail leg → HB on lines 7/8/2/3; `lines` column; PATH $0 note "cross-honored, Hoboken–33rd St only" | Acceptance "CM + PATH money < CM → NY money" cannot hold: njt_fares.csv has CM→HB = CM→NY = $13.05, so with PATH $0 they are equal (CM→NY is infeasible on 10-13 anyway) |
| Check A for the demo trains (1.9, 10-10) | **Not moot.** 3224 and 3828 carry the +780 s NY offset in the Fri archive (3926 does not; 8624 not observed); the pools they draw from are 11 / 27 (NEC) and 4 / 12 (NJCL) affected runs. Sensitivity: with `delay_vs_gtfs_s` at NY (H2) NP 3926 / 3828 / 3224 on time = 0.20 / 0.51 / 0.86 vs 0.30 / 0.84 / 0.99 under H1; the recommendation (NP on 3224 at penalty $75) is unchanged | Published-timetable check for 3224 / 3828 (H1 vs H2) still due before presenting; quote the on-time numbers as a range |
| No-train rows (1.9) | CM / ST / RT → NY replaced by one notice: "Midtown Direct to Penn suspended weekdays Oct 11–Nov 14 (Portal cutover)"; no money shown for candidates without legs | A cost for a trip that cannot run misleads |
| Check A resolved (10-10 evening) | **H1 confirmed**: keep `delay_s` (SEC_LATE) at NY | NJT `getStationSchedule` (Fri 10-09) has `DWELL_TIME = 780` at NY on exactly the +780 s runs (21 / 21; 0 of 304 other NY runs, incl. 23 with dwell 1560 and no offset). The feed's NY time = arrival + terminal dwell; scheduled arrival = GTFS (3224: dep NP 08:20, arr NY 08:42, sched dep 08:55, dwell 780). Published PDF not retrieved; an undated news item gives 3224 a scheduled Penn arrival of 8:41, consistent with GTFS. Last-seen times cannot discriminate (trains stay in the feed through the dwell). H2 sensitivity figures are retired |
| Network build (2.1, 10-10 evening) | `src/routing/network.py` (agnostic: FeedSpec prefix + stop key column; parent_station else same-key collapse) + `src/build/network.py` (NJT/PATH specs, asserts, `eval/network_report.md`); arrays in `data/artifacts/network_20261013.npz` (repo convention, not `data/derived/`). Pattern = route × stop sequence. PATH stops = `PATH:<stop_name>` (no parent_station in the feed; matches the Leg contract), so the 33rd St row is `PATH:33rd Street`. Same-stop minimum = 5 min = scorer's HB → PATH buffer; `TRANSFER_MIN_S` secaucus 6 → 5 (unused) | NJT 771 trips / 243 patterns / 0 splits; PATH 952 / 10 → 15 (5 splits); 176 stops. Build asserts that transfers.csv agrees with `TRANSFER_MIN_S` until the scorer reads transfers.csv (2.4). transfers.csv is under git-ignored `data/reference/` like the other reference CSVs |
| Range RAPTOR (2.2, 10-10 evening) | `src/routing/raptor.py` `route_profile` per spec (K = 3, window [deadline − 150, deadline], arrivals ≤ deadline + 30, rides ≤ 2 out, 3 logged). Two refinements: (1) pruning keeps the best arrival per stop **per ride budget** (min over rounds ≤ k), not one global best, so an earlier, fewer-ride journey with the same arrival is not lost (the spec's Pareto over rides); (2) the same-stop minimum is not added again on a stop reached by a footpath (HB → PATH is the 5-min footpath only, as in the scorer). Journey legs carry `route_id`; synthetic PATH labels `<route>@HH:MM <first stop>` | **PATH GTFS writes after-midnight times as 00:xx** (5 trips, e.g. ATW 23:40 → 33rd St 0:12); unfixed, a bogus 00:16 arrival pruned every real journey. Loader now un-wraps per trip (arr/dep interleaved) and asserts monotone times; PATH patterns 15 → 10 (the earlier 5 'FIFO splits' were this artifact). Profiles contain every safety-net plan with GTFS times (CM/ST/RT 872…, SE 8624/8622/3926, NP 3224/3828/3926); 0.03–0.09 s per station |
| Adapter + scorer chain (2.3, 10-10 evening) | `src/routing/adapter.py` `router_options()` → same dict as `station_options()`; every hard-coded label present (empty → infeasible row kept); key = signature, hard-coded labels kept. Network keeps `trip_direction`, `trip_synthetic`; terminal = pattern's last stop. PATH legs keep `gtfs_line` (the hard-coded ones do). Router window widened: departures to deadline + 60, arrivals ≤ deadline + 60 (= `LATE_CAP_MIN`; spec said dep ≤ deadline, arr ≤ +30), else catch-next loses post-deadline trains (SE 3926: 4 → 1). Scorer: `can_board()` shared rule, rail → rail nested catch-next over second legs, mid-trip alight = pool at that stop with the leg's own period, flagged `PoolUsage.intermediate_alight` (replaces NotImplementedError); walk before first rail leg counted. Pricer fare O–D first rail board → last rail alight. `Settings.source` / `--source` / app radio; default still safety_net until GO | Every router option equals a hard-coded one (dataclass equality, walks included); missing = 3 profile-dominated ST trains (802, 312, 6320) + trains arriving after 10:00. No new signatures at plannable stations for deadlines 07:00–10:00. Demo preset table identical under both sources |
| Router gate (2.4, 10-10 evening) | **GO**: app default `DEFAULT_OPTION_SOURCE = "router"`; `station_options()` kept as the regression reference (`tests/test_adapter.py`, `--source safety_net`, app radio) | `eval/router_validation.md`: 0 unexplained missing options (3 profile-dominated ST trains, the rest arrive after 10:00); top option agrees in 12 / 12 sweep settings; demo preset rank / train / P(on time) / gen_cost identical. Catch-next can be shorter from the router (RT 872: 2 vs 4) with no score change at the preset. No new keys at the plannable stations; HB is not planned (no drive destination) |
