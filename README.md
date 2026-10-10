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

`data/` is git-ignored: raw archive, GTFS, reference CSVs and the personal demo origin. A clean clone
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
python -m pytest tests -q
```

## Known limitations

About one weekday of observed delays (Fri 10-09 AM peak); Chatham → Hoboken has 5 effective runs.
PATH delays are not measured (scheduled times). Drive-time spread comes from Google's traffic
models, not observation. Prices are dated snapshots. The NY delay measure uses SEC_LATE (check A: the feed's NY time includes a 13-min terminal dwell).
