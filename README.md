# Crossing

Reliability-aware, total-cost trip chooser for New Jersey commuters driving + riding into NY Penn
Station. It ranks options by **risk-adjusted cost** (fare + parking + tolls + fuel + time + the
expected cost of arriving late) and shows when that picks a different option than the schedule does.
See `CLAUDE.md` for the design and `IMPLEMENTATION_PLAN.md` for the decision log.

## Run the demo (offline)

The app reads only local files: GTFS, `data/reference/` price tables, and the built artifacts
`data/artifacts/pools.parquet` and `data/artifacts/drive_cache.parquet`. It makes no network calls.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt
.venv/Scripts/python -m streamlit run app/app.py
```

(On macOS / Linux use `.venv/bin/python`.) Open http://localhost:8501. The demo preset is
Tue 2026-10-13, arrive at NY Penn by 09:00, VOT $30/h, hard-deadline penalty $75, margin 5 min.

### Data (not in git)

`data/` is git-ignored (except the alert answer key and the Gemini cache): raw archive, GTFS, reference CSVs
and the personal demo origin. A clean clone
needs a copy of `data/` from the project owner; or point `CROSSING_DATA_DIR` at an existing one
(worktrees do this):

```bash
export CROSSING_DATA_DIR=/path/to/HackathonNJT/data
```

Expected under `data/`:

| Path | What |
|---|---|
| `reference/gtfs/njtransit_gtfs_rail/`, `reference/gtfs/PATH GTFS/` | GTFS feeds |
| `reference/njt_fares.csv`, `path_fares.csv`, `parking.csv`, `driving_costs.csv` | Price tables (sourced, dated) |
| `reference/infra.csv` | Leg → infrastructure exposure (`python -m src.build.infrastructure`) |
| `reference/drive_destinations.csv`, `demo_origin.csv` | Drive endpoints (origin is personal) |
| `artifacts/pools.parquet`, `observations.parquet` | Delay pools (`python -m src.build`) |
| `artifacts/drive_cache.parquet`, `cache/routes_raw/` | Drive cache (`python -m src.build.drive_cache --offline` rebuilds from raw) |

### Rebuild (offline unless noted)

```bash
python -m src.build                       # raw archive + GTFS → observations, pools
python -m src.build.pools                 # pools + eval/pool_evidence.md only
python -m src.build.drive_cache --offline # drive_cache.parquet from cached raw responses
python -m src.build.drive_cache           # network: fills missing Routes API calls (GOOGLE_MAPS_KEY)
```

## Command-line checks

```bash
python -m src.safety_net --deadline 09:00   # candidates, costs, exposure
python -m src.build.network                 # router network (NJT rail + PATH, 10-13) → eval/network_report.md
python -m src.scoring --source router       # ranked table + sweep from router options → eval/core_claim_router.md
python -m src.scoring --source safety_net   # same from hard-coded options → eval/core_claim.md (regression)
python -m src.routing.validate              # router vs hard-coded options + gate → eval/router_validation.md
python -m src.routing.alternatives          # exclusion runs (avoid NY Penn + North River tunnels) → eval/alternatives.md
python -m src.build.alert_labels            # alert answer key: GTFS train verification → eval/alert_label_check.md
python -m src.build.alerts_eval             # Gemini extraction vs reviewed labels (cache replay) → eval/alert_extraction.md
python -m pytest tests -q
```

`src.routing.alternatives` exits non-zero if an exclusion run touches NY Penn, the exposure tagger finds the
North River tunnels or NY Penn on a flagged row, or `exclusion=None` differs from router-v1.

### Alerts (Gemini, offline replay)

`data/reference/alert_samples.csv` (hand-labeled answer key) and `data/alerts/gemini_cache.jsonl` are tracked in
git. Extraction replays the cache; a missing message is an error. Live calls only on request:

```bash
python -m src.build.alerts_extract          # replay check: every sample cached?
python -m src.build.alerts_extract --live   # calls Gemini for uncached messages (GEMINI_AI_KEY in .env)
```

## Known limitations

**Data and model**
- About one weekday of observed delays (Fri 10-09 AM peak); Chatham → Hoboken has 5 effective runs.
  The delay history predates the Portal cutover.
- PATH delays are not measured (scheduled times). Drive-time spread comes from Google's traffic models, not
  observation. Prices are dated snapshots. The NY delay measure uses SEC_LATE (check A: the feed's NY time
  includes a 13-min terminal dwell).
- Transfer times (`transfers.csv`) are estimates.

**Scoring**
- PATH fall-through: if the earliest catchable rail option has no PATH connection (NaN arrival), the draw is
  counted censored instead of trying the next option.
- SE → TS alternative: the SE station buffer (8 min, park → upper level) and the SE → TS walk (5 min) are both
  added; part of the platform walk may be counted twice.
- SE parking: the pricer charges the cheapest SE lot (Lot 1), while the drive cache routes to ParkFast Secaucus
  Junction (noted in the cost breakdown).
- The SE → HB fare is not in `njt_fares.csv` (no sourced value yet): the SE → TS → HB + PATH row is marked
  partial and is **not ranked**. The app shows its $/day as a lower bound ("≥ $X (missing: fare)") and its
  risk-adjusted cost as "—"; partial rows never outrank complete ones.
- On-time grace (`ON_TIME_GRACE_S`) is applied by the scorer only; the planner and router use the bare
  deadline. No effect at the current 0 s.
- A rail leg built without `terminal` falls back to "alights at NY / HB on a toward-NY trip" to decide
  intermediate alighting.

**Alerts**
- Gemini free tier: 20 requests / day per model, so each message was extracted with one call (no repeats,
  no self-consistency check). 4 sample messages (A021, A022, A024, A025) are not yet extracted.
- The requested model `gemini-3.5-flash` is served as `gemini-3.6-flash` (`modelVersion` in each cached
  response).
- Prompt v1 only: not tuned against the labels. 20 reviewed messages; none is a real Penn / North River tunnel
  disruption, so `penn_disruption` recall is untested.
- Evaluation is per message: kind, cause, delay, cut and direction come from the first alert that sets them,
  lines and trains are the union. A message that yields several alerts is not scored alert by alert.
- Alerts in the app (sidebar "Disruption"): two inputs only. "Penn suspended" is a hand-built scenario
  (label *scenario*, no Gemini) mapped to the NY Penn / North River tunnel exclusion and re-routed via
  `route_all(base=...)`; A017 is an archived message replayed from the Gemini cache (label *replayed*).
  Only `penn_disruption` changes the options. Every other alert kind is display only, with a direction /
  scope gate ("not affecting your trip"); delay shifts and train bans from alerts are not applied yet.
- The scope gate compares the stations an alert names with the stations the options board or alight at
  (Hoboken and NY Penn ignored as reference points); intermediate stops are not checked.
