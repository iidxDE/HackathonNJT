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
