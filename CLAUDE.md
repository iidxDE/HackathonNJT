# CLAUDE.md — Crossing

Guidance for Claude Code working in this repository. Read this fully before making changes.

---

## 1. What this project is

**Crossing** is a reliability-aware, total-cost trip chooser for commuters driving + riding from New Jersey into **New York Penn Station**. Built solo for HackRU Fall 2026 (24h, Oct 10–11, 2026).

For a given origin, departure time and deadline, it compares options across six access stations and ranks them by **risk-adjusted cost**: fare + parking + tolls + fuel + the expected cost of arriving late.

**The one claim the demo must prove:** ranking by risk-adjusted total cost picks a different option than ranking by scheduled travel time. Every feature either produces that moment or makes it credible. Anything else is roadmap.

### Locked scope decisions

| Decision | Value |
|---|---|
| Destination | **NY Penn Station only** |
| Access stations | Chatham, Summit, Short Hills, Secaucus, Newark Penn, Hoboken (via PATH to 33rd St + walk) |
| Extra baseline | Drive all the way into Manhattan (tunnel toll, garage, congestion charge) |
| Demo scenario | Presentation is Sun 2026-10-11 (end of hackathon). The demo **queries** a fixed weekday trip: **Mon 2026-10-12, AM peak** ("tomorrow morning's commute"), not "now". Weekend service is sparse and unrepresentative. Mon 10-12 is also day one of the new timetable. |
| Routing timetable | The **new** NJ Transit timetable effective Mon 2026-10-12 |
| Delay data | Own archive of NJ Transit rail vehicle data, Thu 2026-10-08 evening → present |

### Feature tiers

| Tier | Features |
|---|---|
| Core | Total-cost comparison · empirical reliability · risk-adjusted cost · infrastructure exposure labels · drive-all-the-way row |
| Engine | RAPTOR run once per access station, merged into a Pareto set |
| Module (timeboxed 1.5h) | Gemini alert parsing, flagging affected options |
| Stretch | Tunnel-outage scenario toggle · persistence nowcast ("this train is already running late") |
| Cut (roadmap only) | LightGBM quantile model · PostGIS · live disruption detection · multi-access mcRAPTOR with cost labels · bus data |

Do not build cut items unless explicitly asked.

---

## 2. Architecture

Two tiers. **Everything heavy happens offline. The request path reads only local files.** The demo must work with no network.

```
OFFLINE (one rerunnable build command)
  EC2 poller (raw rail JSON) ──► build pipeline ──► local artifacts (Parquet, JSON caches)
                                                         ▲
                                  reference data ────────┘
                                  (GTFS, prices, transfers, infra map)

REQUEST TIME (pure functions, local files only)
  access legs ──► router ──► pricer + exposure tagger ──► Pareto filter ──► scorer ──► Streamlit UI
  (cached drive)  (RAPTOR per station)                                     (Monte Carlo)
                                                                              ▲
                                                           delay pools, alerts (artifacts)
```

### Principles

1. **Raw data is never modified.** All processing is reproducible from the raw archive by one build command.
2. **Deterministic routing, probabilistic scoring — kept separate.** The router never sees delays. The scorer never routes (except the outage toggle, which calls the router).
3. **Contracts are the module boundaries.** The router and the hard-coded safety-net options emit the *same* `Candidate` contract, so the scorer, UI and evaluation work unchanged whichever produced them.
4. **Every price and external fact is dated and sourced** (`source_url`, `checked_date`).
5. **Network-agnostic routing core.** GTFS in, journeys out. No NJ-specific logic inside the RAPTOR module itself (NJ specifics live in reference data and the pricer). The same engine must run on a Japanese ODPT network later.

---

## 3. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Data | pandas + Parquet (pyarrow) | No database |
| Routing | Own RAPTOR, ported from Tetsudle | numpy arrays; GTFS read with pandas |
| Reliability | numpy empirical samples + Monte Carlo | |
| Drive legs | Google Maps Routes API (traffic-aware, toll info) | Called at **build time**, cached to JSON |
| Alerts | Gemini API, structured JSON output | Run offline over archived messages, cached |
| UI | Streamlit + pydeck | |
| Tests | pytest | |
| Collection | Existing EC2 poller (rail feeds) | Not part of this repo's request path |

Do not add new dependencies without a clear reason. Prefer the standard library, pandas and numpy.

---

## 4. Component details

### 4.1 Raw archive and cleaning

- Source: NJ Transit rail `getVehicleData` polled every ~30s since Thu 2026-10-08 evening, plus `getStationMSG` every 5 min.
- Archive quality: ~98% of expected polls present; empty snapshots between ~02:00–04:30 are normal (no service), not outages.
- **Snapshot filters (apply both):**
  - Drop snapshots whose train count is below half the rolling median.
  - Drop snapshots whose newest record is more than ~120s old (stale API responses, e.g. Fri 16:03–16:07 ET).
- Timestamps inside records are **New York local time with no zone marker**. Localize with `America/New_York`, then convert to UTC.
- Delay field is `SEC_LATE`. Positions are **snapped to stations**: model stop by stop, never between stations.

### 4.2 Joining the feed to GTFS

- Join key: train number + service date.
- **Strip leading zeros on both sides.** GTFS `trip_short_name` is zero-padded ("0400"); the API is not ("400"). A plain string join silently drops nearly all Gladstone and most Morris & Essex trains. With zeros stripped the join is ~99.9%.
- Trains first seen after midnight: if no match, try the adjacent service day(s).
- Port Jervis trains: ignore for this project (they run to Hoboken, not Penn).
- **After the join, GTFS is canonical** for line (route) and direction (`direction_id`). Discard the feed's line names and direction labels. (The feed mislabels some trains and the Atlantic City Line's direction convention is reversed.)

### 4.3 Delay pools (reliability v0)

- Pool key: **line × toward_ny × period**, not hour and not train number.
  - `toward_ny` (bool, per trip) = the trip's GTFS terminal is NY or HB (`config.TOWARD_NY_TERMINALS`). Do **not** key on GTFS `direction_id`: NJT direction 0 also covers trips ending at Newark Penn, Long Branch, Atlantic City, etc.
  - Periods: early (05–06), AM peak (06–09:30), shoulder (09:30–11); extend similarly for the rest of the day.
  - Do not pool by train number: the 10-12 timetable change moves start times by up to ±40 min.
- **Fallback hierarchy** when a cell has fewer than ~8 runs: line × toward_ny × period → toward_ny × period (all lines) → toward_ny only. Record which level was used and the run count; the UI shows it as a confidence indicator.
- **Store whole runs, not independent stop delays.** Each sample is one historical run with its delay at every stop. Sample size = number of runs, never number of stop observations.
- **Delay measure: `delay_vs_gtfs_s` only** (estimated time = feed schedule + SEC_LATE, minus the GTFS time). Never pool raw `SEC_LATE`: for some NY arrivals the feed's own schedule is 13 min later than the published GTFS time, so SEC_LATE understates lateness against the timetable riders use.
- **One delay per stop = the approach delay:** the last record seen while that stop was `NEXT_STOP`. It also stands in for the departure delay at that stop (stated limitation; dwell is not modeled).

### 4.4 Access legs

- Drive time and tolls from the Google Routes API, origin → each access station and origin → Manhattan garage.
- Called during the build, cached to JSON keyed by (origin, station, departure time). Request time reads the cache; a live call is optional and must fall back to the cache on any error.

### 4.5 Router

- Standard RAPTOR (criteria: **arrival time, number of transfers**), run **once per access station**, seeded with that station's drive arrival time.
- Network: NJ Transit rail GTFS (new timetable) + PATH GTFS, plus a hand-built transfers file.
- Required transfer links (hand-built, with minimum transfer times):
  - Hoboken: drive/park → PATH
  - PATH 33rd St → Penn Station (walk)
  - Secaucus: same-system transfer minimum
  - Newark Penn: NJ Transit ↔ PATH
- **Cost is not a RAPTOR criterion.** NJ Transit fares are origin–destination, not additive per leg. Cost is computed after routing (see 4.6). Results from all stations are merged, then filtered to the Pareto set on (arrival, cost, transfers).
- GTFS times can exceed 24:00:00. Handle as seconds since service-day midnight.

### 4.6 Pricer

- `base_cost = drive fuel + tolls + parking + NJ Transit O–D fare (+ PATH flat fare if PATH is used)`.
- Fuel = miles ÷ mpg × gas price (AAA NJ average). mpg is a user setting.
- Price tables are CSVs under `data/reference/`, each row with `source_url` and `checked_date`. Parking table has `daily_rate`, `permit_only`, `operator`; permit-only lots are excluded.
- Drive-all-the-way row: drive cost + tunnel toll + Manhattan garage + congestion charge.

### 4.7 Exposure tagger

- Static map: route segment → infrastructure asset (Portal Bridge, North River tunnels, Penn Station, PATH tunnels).
- A journey's exposure = the set of assets its legs traverse. Hoboken + PATH is the tunnel-avoiding option.

### 4.8 Scorer (Monte Carlo)

For each Candidate, N ≈ 2,000 samples:
1. Sample drive time (cached duration × a stated variance assumption; there is no observed drive variance).
2. For each rail leg, sample **one whole historical run** from the appropriate pool, so boarding-stop and Penn delays stay correlated. Sample **only runs observed at both the leg's boarding and alighting stops**; that effective count is what `PoolUsage.run_count` reports (and what the fallback threshold applies to).
3. If the sampled drive misses the planned train, move to the next departure from the same station toward Penn.
4. For transfers, compare sampled arrival of leg 1 with sampled departure of leg 2; a miss cascades to the next train.
5. PATH legs: **assumed on schedule**, with a missed-connection penalty of one headway wait. Label PATH legs "assumed, not measured" in the UI.

Outputs: P(on time), arrival p50 / p90, expected late minutes, and
`risk_adjusted_cost = base_cost + Σ P(late in bucket) × cost_of_lateness(bucket)`,
where `cost_of_lateness` comes from the user (value of time, extra parking, or a fixed hard-deadline penalty).

### 4.9 Alerts (Gemini)

- Offline: parse archived `getStationMSG` messages into structured records: lines, stations, trains, delay minutes, cause, severity, `affects_penn`.
- Validate on ~20 hand-labeled messages and report extraction accuracy. Never assume it.
- Request time: flag Candidates whose lines have an active alert. Alerts never silently change scores.

### 4.10 UI (Streamlit)

- Inputs: origin, departure time / arrive-by deadline, mpg, fare type, cost of being late.
- Output: ranked options with P(on time), p50/p90 arrival, itemized base cost, risk-adjusted cost, exposure tags, alerts, and the pool fallback level + run count.
- Map via pydeck.

---

## 5. Data contracts

Implement as frozen dataclasses (or equivalent typed structures) in one module. Do not pass loose dicts between modules.

**Leg:** mode (drive / rail / PATH / walk) · from stop · to stop · scheduled departure · scheduled arrival · train number (rail only) · GTFS line · direction.

**Stop identifiers in legs** (same keys as `data/reference/infra.csv`):
- NJ Transit stops: the 2-char station code (GTFS `stop_code`, e.g. `CM`, `NP`, `NY`). Secaucus = **`SE`** (upper level; the level NY-bound NEC/NJCL/M&E trains use, verified against the observations: all 148 Secaucus rows on NY-bound runs are SE). `TS` (lower level) serves only Hoboken-bound Main/Bergen/Pascack trains.
- PATH stops: `"PATH:<stop_name>"`, e.g. `PATH:Hoboken`, `PATH:33rd Street`.
- Non-station endpoints: `ORIGIN` for the trip origin.
- **Cross-system transfers require an explicit WALK leg**, e.g. `HB` → `PATH:Hoboken`, `PATH:33rd Street` → `NY`. `Candidate` rejects legs that do not connect stop to stop.

**Candidate** (router or safety net → scorer): access station · ordered legs · itemized costs (fuel, tolls, parking, fare, PATH fare) · base cost · exposure tags · source (`"router"` or `"safety_net"`).

**DelaySamples** (model → scorer): array of whole-run delay samples · pool key used · fallback level · run count.

**ScoredOption** (scorer → UI): the Candidate · P(on time) · arrival p50 · arrival p90 · expected late minutes · risk-adjusted cost · active alerts · assumption flags (e.g. PATH unmeasured).

---

## 6. Evaluation

- **Task 1, pre-trip:** predict the delay distribution at NY Penn before departure. Train: Thu evening + Fri before 12:00 ET. Test: Fri 12:00 onward (move the split later if the AM-peak pool is thin). Report MAE vs. the schedule baseline and the pooled baseline, plus calibration of P(on time) in 3–4 bins.
- **Task 2, nowcast:** given a train's current delay at an upstream stop, predict its delay at Penn. Same split. Compare against persistence and the schedule.
- **Weekend robustness:** weekday-trained model on Sat/Sun data, reported separately as a service-change result, never as headline accuracy.
- Always use **time-block splits**. Never random-split runs from the same day (one disruption affects many trains).

---

## 7. Proposed repo layout

Adjust to the actual repo; keep the separation.

```
data/raw/            raw archive (read-only, never modified)
data/reference/      GTFS zips, price CSVs, transfers, infrastructure map
data/reference/gtfs  GTFS zip
data/artifacts/      build outputs (Parquet, JSON caches) — regenerable, git-ignored
src/contracts   data contracts
src/build/      clean, join, pools, drive cache, alerts
src/routing/    RAPTOR (network-agnostic)
src/pricing/    pricer, exposure tagger
src/scoring/    Monte Carlo scorer, risk-adjusted cost
src/safety_net  hard-coded candidates
app/                 Streamlit UI
eval/                evaluation scripts and results table
tests/
```

---

## 8. Coding conventions

- **Time:** store and compute in UTC internally; convert to `America/New_York` only for display. GTFS schedule times are seconds since service-day midnight (may exceed 86,400). Never mix naive and aware datetimes.
- **Identifiers:** train numbers are strings with leading zeros stripped. Stop and route IDs are strings, never ints.
- **Purity:** request-time functions take inputs and artifacts and return values. No hidden I/O, no global state, no network calls except the optional, guarded live drive call.
- **Determinism:** Monte Carlo takes an explicit seed. Same inputs + seed → same output.
- **Configuration:** paths, N samples, period boundaries, fallback threshold, transfer minimums and the drive-variance assumption live in one config module. No magic numbers in logic.
- **Secrets:** API keys from environment variables only. Never commit keys or `.env`.
- **Data formats:** Parquet for tables, JSON for API caches, CSV only for hand-edited reference data.
- **Types and style:** type hints on public functions; short docstrings stating units (seconds, dollars, minutes). Units go in names where ambiguous (`delay_s`, `cost_usd`).
- **Tests:** add golden tests for the router (a known Chatham → Penn train must appear) and for the join (zero-padded IDs must match). Run tests before declaring work done.
- **Logging:** log counts at each build stage (snapshots dropped, join rate per line, runs per pool cell).
- **Scope discipline:** small, focused changes. Finish the end-to-end path before polishing any one part.

---

## 9. What not to do

- **Do not modify or delete anything in `data/raw/`.**
- **Do not call external APIs at request time** without a cache fallback. The demo must run offline.
- **Do not join train numbers as raw strings.** Strip leading zeros first.
- **Do not trust the feed's line names or direction labels** after the join; use GTFS.
- **Do not pool delays by train number or by single hour.**
- **Do not sample stop delays independently** within a run; sample whole runs.
- **Do not count stop observations as sample size.**
- **Do not put cost or reliability inside RAPTOR dominance.** Cost is post-routing; reliability is post-routing.
- **Do not add NJ-specific logic to the routing module.**
- **Do not present PATH reliability as measured.** It is an assumption.
- **Do not random-split the evaluation data.** Use time blocks.
- **Do not evaluate on weekend data as if it were the headline result.**
- **Do not build cut-scope features** (LightGBM, PostGIS, bus data, live disruption detection) unless asked.
- **Do not hard-code prices without `source_url` and `checked_date`.**
- **Do not commit API keys, `.env` files or large regenerable artifacts.**
- **Do not break the Candidate contract.** The safety net and router must stay interchangeable.

---

## 10. Known limitations (state them, don't hide them)

- About 1.5 weekdays of rail history at build time; most pool cells fall back to coarser levels.
- The 10-12 timetable change: pools are timetable-independent by design, but this is untested on new-timetable data.
- Station-snapped positions; no between-station modeling.
- PATH delays are not measured.
- Drive-time uncertainty is an assumption, not observed.
- Prices are dated snapshots and will drift.

---

## 11. Timeline checkpoints (Sat 10-10 → Sun 10-11)

| Hours | Goal |
|---|---|
| 0–1.5 | Lock scenario and contracts; price tables; GTFS + archive join |
| 1.5–4 | **Safety net:** 4 hard-coded options + drive-all-the-way, scored end to end |
| 4–8 | RAPTOR per access station over rail + PATH with transfers |
| 8 | **Go / no-go:** if Pareto sets are wrong, demo the safety net |
| 8–11 | Streamlit UI, exposure labels |
| 11–12.5 | Gemini alerts (timeboxed) |
| 17–19 | Stretch: outage toggle, persistence nowcast |
| 19 | **Feature freeze**, evaluation table |
| 19–24 | README, backup demo video, pitch |