# Crossing: Phase 2 handoff

*Sat 2026-10-10, evening ET. Read this with `CLAUDE.md`, `IMPLEMENTATION_PLAN.md` (decision log at the
end), `README.md` and `docs/PHASE1_HANDOFF.md` before starting Phase 3. **Start with §0.***

---

## 0. Read first

**Phase 2 is complete and tagged `router-v1` (`7502e55`).** The router (range RAPTOR per access station
over NJ Transit rail + PATH) passed the 2.4 gate (**GO**), and **the app now uses router options by
default** (`config.DEFAULT_OPTION_SOURCE = "router"`). The hard-coded `station_options()` stays as the
regression reference: `--source safety_net`, the app's "Options from" radio, and `tests/test_adapter.py`.

**Gate result** (`eval/router_validation.md`):
- Every router option is **dataclass-equal** (walk legs included) to a hard-coded option, and the router
  adds no options of its own.
- Hard-coded options the router does not return: 3 profile-dominated Summit trains (802, 312, 6320) plus
  trains arriving after deadline + 60 min. None unexplained.
- Top option (rank and sched_rank) agrees with the safety net in **12 / 12** sweep settings; demo preset
  rank / train / P(on time) / gen_cost **identical**.

**The core claim and pitch are unchanged** (`eval/core_claim_router.md` = `eval/core_claim.md`):
demo preset (Tue 10-13, arrive NY Penn by 09:00, VOT $30/h, penalty $75, margin 5 min) → **NP → NY on
3224, leave 07:40, on time 99 / 100**, vs the timetable plan 3926 (leave 07:56, on time 30 / 100). The
train flips in 9 / 12 sweep settings, the station in 0 / 12.

**Check A is closed (H1 confirmed).** NJT's own station schedule lists `DWELL_TIME = 780` at NY on exactly
the runs whose feed NY time is +780 s vs GTFS (21 / 21 on Fri 10-09, 0 of 304 others). The feed's NY time
is arrival + terminal dwell; scheduled arrival = GTFS; SEC_LATE is the arrival delay. The H2 "quote ranges"
from the Phase 1 handoff are retired.

**Open before presenting:**
1. **Merge the last commits.** PR [iidxDE/HackathonNJT#1](https://github.com/iidxDE/HackathonNJT/pull/1)
   was merged at `66ccec7` (2.4). The 2.5 fixes + `router-v1` (`b01f3f4` … `7502e55`, plus this handoff)
   need a new PR: https://github.com/iidxDE/HackathonNJT/compare/master...claude/phase-2-hackathon-start-8771df
   (merges cleanly).
2. **Demo backup** (carried from Phase 1): screenshots + a 60-second recording of the demo preset.

---

## 1. Status at a glance

| Step | Commit | Result |
|---|---|---|
| Check A resolved | `fac065b` | Feed NY time = arrival + 780 s terminal dwell; `delay_s` at NY confirmed |
| 2.1 Network build | `68d9289`, `858a1d7` | `network_20261013.npz` + `eval/network_report.md`; all build asserts pass |
| 2.2 Range RAPTOR | `4918510`, `cc8f916` | `route_profile` per station, 0.05–0.1 s each; PATH midnight-wrap bug found and fixed |
| 2.3 Adapter + scorer chain | `aa96f95` | `router_options()` → same dict as `station_options()`; rail → rail scoring; source switch |
| 2.4 Validation | `e6f787f`, `66ccec7` | **GO**; app default = router (PR #1 merged here) |
| 2.5 Router fixes | `b01f3f4` | Late cap on every draw, taken second leg recorded, one GTFS → UTC rule, `Leg.terminal`, `SAME_STOP_TRANSFER_S`, GO needs identical scores |
| Clean stamps | `353924d`, `7502e55` | Build stamps ignore `eval/`; reports at `353924d`; **tag `router-v1`** |

**Tests:** 127 pass (`python -m pytest tests -q`, about 75 s, with `CROSSING_DATA_DIR` set; data-backed
tests skip without data).

**2.5 regression check:** a before/after dump of 216 scored rows (both sources × 12 sweep settings × all
train variants, every field) showed **0 differences**: no demo draw exceeds the 60-min cap.

**Git:** branch `claude/phase-2-hackathon-start-8771df` = `origin` = `7502e55` (+ this handoff). Tag
`router-v1` pushed. `origin/master` = `b53a1c4` (merge of PR #1).

---

## 2. Environment

Same as Phase 1 (`docs/PHASE1_HANDOFF.md` §2), plus:
- Run from a worktree with the main checkout's interpreter and data:
  `CROSSING_DATA_DIR=C:/Users/anton/PycharmProjects/HackathonNJT/data PYTHONIOENCODING=utf-8 ../../../.venv/Scripts/python.exe …`
  (add `PYTHONPATH=.` for `-c` one-liners).
- `.claude/launch.json` in this worktree (git-ignored) starts the app in the preview pane as `crossing-app`
  on **port 8502**. `get_page_text` / `find` work; screenshots were not needed.
- Bash heredocs with nested quotes still break: write patch scripts to the scratchpad, or use
  `python - <<'PYEOF'` with no stray quotes.

---

## 3. Commands

```bash
python -m src.build.network                 # router network (NJT rail + PATH, 10-13) → eval/network_report.md
python -m src.routing.validate              # router vs hard-coded options + gate → eval/router_validation.md
python -m src.scoring --source router       # demo table + sweep from router options → eval/core_claim_router.md
python -m src.scoring --source safety_net   # same from hard-coded options → eval/core_claim.md (regression)
python -m streamlit run app/app.py          # the demo; sidebar "Options from" switches source
python -m pytest tests -q
```

Phase 1 commands (`src.build`, `src.build.pools`, `src.build.drive_cache --offline`, `src.safety_net`) are
unchanged. To regenerate reports with a clean stamp: commit code, run the four report commands, commit
`eval/` (the stamp now ignores `eval/` changes).

Inspect one station's raw profile:
```bash
python -c "from src import config; from src.routing.network import Network; from src.routing.raptor import route_profile, describe; n=Network.load(config.network_npz(config.DEMO_SERVICE_DATE)); [print(describe(j)) for j in route_profile(n, 'NJT:NP', config.DEMO_SERVICE_DATE, 9*3600)]"
```
(The trailing `walk NJT:NY→DEST:NYP 0m` is the hop into the virtual target; the adapter drops it.)

---

## 4. Request path (with the router)

`src/scoring/pipeline.py`: `load_demo_data()` now also loads the network (`DemoData.network`, None if the
npz is missing) and reads the same-stop minimum from transfers.csv. `simulate(data, settings)` calls
`data.options_for(settings)`:
- `source="safety_net"` → `data.options` (hard-coded `station_options`, deadline-independent);
- `source="router"` → `router_options(network, service_date, deadline_s)` (cached on the network per
  deadline), then the unchanged chain `build_candidates(..., source=...)` → `price_candidate` →
  `tag_candidate` → `score_all` → `reprice` / `sweep`.

**Timing:** the first router simulate for a new deadline takes about 0.5 s (5 RAPTOR runs + adapter),
later ones about 0.05 s; the safety net takes about 0.1 s warm. `Settings.source` is part of the app's
`simulate` cache key.

| Module | Role |
|---|---|
| `src/routing/network.py` | **Network-agnostic.** `FeedSpec(prefix, dir, stop_key)`, `load_timetable` (active services, parent / same-name stop collapse, `unwrap_midnight`, synthetic labels), `build_network` (patterns = route × stop sequence, FIFO split, stop → pattern index, footpaths), `Network.save/load` (npz), `trip_index()`, `terminal_of_pattern()`, `load_transfers` |
| `src/routing/raptor.py` | **Network-agnostic.** `route_profile(network, access_stop, service_date, deadline)` → `list[Journey]`; `Journey` / `JourneyLeg` (walk legs included); `ProfileStats`; `describe()` |
| `src/routing/adapter.py` | Router ↔ contracts boundary: `router_options()`, `journey_to_option()`, `signature()`, `label_for()`, `gtfs_utc()`, `AdapterReport` (drop counts, profile stats) |
| `src/routing/validate.py` | 2.4 report + gate |
| `src/build/network.py` | NJ-specific build: NJT / PATH specs, asserts, report |
| `src/scoring/scorer.py` | + `can_board()`, rail → rail nested catch-next, `_rail_draws` (intermediate alight), late cap on every draw |

### Router (`route_profile`)
- Range RAPTOR, K = 3 rounds; departures from the access stop (or a footpath neighbour) in
  **[deadline − 150 min, deadline + 60 min]**; arrivals at `DEST:NYP` ≤ **deadline + 60 min**; journeys with
  ≤ 2 rides returned, 3-ride journeys counted (`stats.dropped_rides`) and logged.
- Labels τ[k][stop] persist across departure iterations (latest → earliest); pruning keeps the best
  arrival per stop **per ride budget** (min over rounds ≤ k), so an earlier, fewer-ride journey with the same
  arrival is not lost.
- Footpaths and the same-stop minimum come from transfers.csv only. **No double transfer:** a stop reached
  on foot needs no extra same-stop minimum (HB → PATH = the 5-min footpath only, as in the scorer).
- Boards only where `pickup_type ≠ 1`, alights only where `drop_off_type ≠ 1`.

### Adapter (`router_options`)
- Stops: `NJT:<code>` → `<code>`, `PATH:<name>` unchanged, `DEST:NYP` → `NY`; the 0-min `NJT:NY → DEST:NYP`
  walk is dropped. Times: `gtfs.gtfs_to_utc` (local noon − 12 h + s).
- Rail legs: train_number = trip_short_name (normalized), gtfs_line = route_id, direction = direction_id,
  toward_ny and **terminal** from the pattern's last stop. PATH legs keep `gtfs_line` (as the hard-coded
  ones do).
- Key = signature (access, [(mode, board, alight) per ride]); hard-coded signatures keep their labels
  (`NP → NY`, `CM → HB + PATH`); every hard-coded key is present (empty → infeasible row kept).
- Dropped and counted: rail trip without trip_short_name or direction_id; shapes the scorer cannot simulate
  (anything other than rail, rail → rail, rail → HB + PATH Hoboken → 33rd St).
- Plans only `config.ROUTER_ACCESS_STATIONS` = CM / ST / RT / SE / NP (HB has no drive destination or
  station buffer).

### Scorer additions
- `can_board(ready, sched_dep, board_delay)`: the one catchability rule (router uses it with zero delay).
- Walk before the first rail leg is added to `ready` (e.g. SE → TS).
- **Rail → rail:** leg-1 actual arrival + transfer (walk legs between, else `inputs.same_stop_transfer_s`) →
  nested catch-next over the second rail legs of all catch-next options, earliest scheduled arrival first.
  `train_taken_share` labels `first+second`; the PoolUsage of the second-leg train actually taken is
  recorded.
- **Intermediate alight** (`leg.to_stop != leg.terminal`): pool anchored on that stop, period from the
  leg's own arrival (approximation), `PoolUsage.intermediate_alight` + flag `intermediate_alight`.
- **Late cap:** `late_min = min(max(0, arrival − deadline), LATE_CAP_MIN)` on every draw.
- Pricer: NJT fare is O–D from the first rail boarding to the last rail alighting.

**None of the rail → rail / intermediate paths occur at the demo** (no such signature at the plannable
stations for deadlines 07:00–10:00); they are tested on synthetic candidates only.

---

## 5. Contracts and config (changed in Phase 2)

- **`Leg.terminal`** (rail only, optional): the trip's last stop; set by both the safety net and the
  adapter (equality between them includes it). Non-rail legs with a terminal raise.
- **`PoolUsage.intermediate_alight`** (bool).
- **`Candidate.source`** is `"router"` for router-planned rows (drive-all-the-way stays `"safety_net"`).
- **`Settings.source`** (`"router"` | `"safety_net"`); **`ScoringInputs.same_stop_transfer_s`**.
- Config: `NETWORK_NPZ_TEMPLATE`, `network_npz()`, `NETWORK_REPORT_MD`, `ROUTER_DEST_STOP = "DEST:NYP"`,
  `ROUTER_VIRTUAL_PREFIXES`, `CUTOVER_HOBOKEN_ROUTE_IDS = (7, 8, 2, 3)`, `ROUTER_MAX_ROUNDS = 3`,
  `ROUTER_MAX_RIDES_OUT = 2`, `ROUTER_WINDOW_BEFORE_S = 150 min`, `ROUTER_ARRIVE_AFTER_S` =
  `ROUTER_DEPART_AFTER_S` = `LATE_CAP_MIN` (60 min), `ROUTER_MODE_BY_FEED`, `ROUTER_NJT_PREFIX`,
  `ROUTER_ACCESS_STATIONS`, `OPTION_SOURCES`, `DEFAULT_OPTION_SOURCE = "router"`,
  **`SAME_STOP_TRANSFER_S = 5 min`**; `TRANSFER_MIN_S["secaucus_same_system"]` 6 → 5 min (unused).
- `gtfs.gtfs_to_utc(service_date, seconds, tz)`: the single request-time GTFS → UTC rule
  (`candidates.to_utc`, `adapter.gtfs_utc`, `scorer.local_epoch` delegate to it).

---

## 6. Data (git-ignored, local to this machine)

| File | Phase 2 change |
|---|---|
| `data/reference/transfers.csv` | **New**, hand-built (no transfers.txt in either feed). Directional rows, `source = estimate`: NJT:HB ↔ PATH:Hoboken 5, NJT:NP ↔ PATH:Newark 5, NJT:SE ↔ NJT:TS 5, PATH:33rd Street → DEST:NYP 4, NJT:NY → DEST:NYP 0; `*,*` same-stop minimum 300 s. The build asserts it matches `TRANSFER_MIN_S` (HB → PATH, 33rd St → Penn) and `SAME_STOP_TRANSFER_S` |
| `data/artifacts/network_20261013.npz` | **New**, regenerable: `python -m src.build.network` |

The network (10-13): NJT 771 trips / 243 patterns / 0 FIFO splits; PATH 952 trips / 10 patterns / 0
splits, 5 midnight wraps fixed; 176 stops incl. `DEST:NYP`. No light-rail trips run on 10-13.

---

## 7. Findings to carry forward

- **PATH GTFS writes after-midnight times as `0:xx` instead of `24:xx`** (5 trips, one wrapping between
  an arrival and a departure at the same stop). Unfixed, a bogus 00:16 arrival pruned every real journey.
  `unwrap_midnight` fixes it per trip (arr / dep interleaved) and the build fails if any trip still runs
  backwards. The "5 FIFO splits" first reported for PATH in 2.1 were this artifact.
- **PATH GTFS has no `parent_station`:** each station is listed once per direction under the same
  stop_name; platforms collapse on stop_name → `PATH:<stop_name>` (matches the Leg contract).
- **Synthetic PATH labels** are `<route>@HH:MM <first stop>`: the same route leaves at the same minute in
  both directions, so route + time is not unique.
- **Router window:** the spec's departures ≤ deadline / arrivals ≤ +30 would have cut catch-next (SE
  3926: 4 → 1 trains). Both are now deadline + `LATE_CAP_MIN`; with the cap applied to every draw a later
  train cannot change any cost.
- **Catch-next can be shorter from the router** (RT 872: 2 vs 4 trains; ST drops dominated 6320, adds
  6624) with no score change at the preset.
- **Profile-dominated Summit trains:** 802 (beaten by 6406), 312 (by 412), 6320 (by 6620).
- **Check A mechanism:** the feed's NY `SCHED_DEP_TIME` is the departure after a 13-min terminal dwell
  (`DWELL_TIME = 780`); last-seen times cannot discriminate (trains stay in the feed through the dwell).
  23 NY runs have `DWELL_TIME = 1560` and no offset.

---

## 8. Known gaps / cleanup (not blocking)

- **Shapes not simulated:** PATH-only journeys and rail → PATH anywhere but Hoboken → 33rd St are dropped
  by the adapter (the scorer's PATH timetable is Hoboken → 33rd St only, and PATH delays are not measured).
  None occur at the plannable stations.
- **Intermediate-alight period** is approximated by the leg's own arrival, not the run's terminal arrival.
- **HB as an access station** is not planned (no drive destination, no station buffer).
- **The scorer still reads the HB → PATH and 33rd St walk times from `TRANSFER_MIN_S`**, not from
  transfers.csv; the build asserts they agree. Wire the scorer to transfers.csv if they ever diverge.
- **`src/build/join.py`** keeps its own build-time GTFS → UTC (midnight + s); same result except on DST
  days; changing it means rebuilding observations and pools.
- `train_taken_share` for one-rail options is keyed by the first train number only (unchanged from
  Phase 1).
- **Router validation covers the demo date and the 09:00 deadline only** (option shapes were surveyed
  for 07:00–10:00); `router_options` is cached per deadline, so many distinct deadlines grow the cache.
- Phase 1 leftovers still open: unused `PATH_ASSUMED_HEADWAY_S` / `LATENESS_BUCKETS_MIN`; undated fare
  sources; NP drive cache routes to Gateway One while the cheapest lot is 986 Raymond Blvd; evaluation
  tasks (CLAUDE.md §6) and weekend robustness not started.

---

## 9. Phase 3 (UI) starting points

Per `IMPLEMENTATION_PLAN.md` Phase 3 and CLAUDE.md §4.10:
- Ranked cards / table with itemized cost breakdown (fuel · tolls · parking · fare · PATH fare).
- Confidence indicator: pool fallback level + run count (`ScoredOption.pools`; now also
  `intermediate_alight`).
- Assumption flags: "PATH: assumed, not measured"; drive variance assumed.
- Exposure badges per option (`Candidate.exposure`).
- pydeck map: origin, six stations, rail / PATH lines, selected option highlighted.
- The "scheduled-time ranking vs risk-adjusted ranking" comparison view (the demo's key moment: 3926 vs
  3224 at NP).
- Decide whether to keep the "Options from" radio visible in the demo (useful for the "validated against
  the timetable" line) or move it to an advanced section.
- Done when: full demo walkthrough under 2 minutes, offline; `tests/test_app.py` covers the default
  (router) source and the switch.

After Phase 3: Phase 4 Gemini alerts (hard 1.5 h timebox, `GEMINI_AI_KEY`), then stretch items
(tunnel-outage toggle can now call the router with an asset removed).
