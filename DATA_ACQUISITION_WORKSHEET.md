# Data Acquisition Worksheet — Friday night before HackRU

*Window: 7:15 – 10:00 PM. Hard stop at 10:00. Companion to `PROJECT_BRIEF.md`.*

**Rule for tonight:** collect and verify data, set up the environment. **No project logic** (no routing, no model code) until the event starts.

**Rule for every dataset:** record `source_url` and `checked_date` for every number. If a value can't be confirmed, leave it blank and mark it, never guess.

---

## 0. Station scope (5 min) — do this first

Everything else keys off this list. Use NJ Transit `STATION_2CHAR` codes (from the rail API station list / Appendix V) as the ID everywhere.

**Priority A — must have tonight (drives the demo)**

| Code | Station | Why |
|---|---|---|
| CM | Chatham | Home station |
| ST | Summit | Larger station, more service |
| TS / SE | Secaucus (lower / upper level) | The alternative being tested |
| NY | New York Penn Station | Destination |
| HB | Hoboken | PATH alternative |
| NP | Newark Penn Station | Hub / PATH transfer |

**Priority B — if time allows:** Madison (MA), Convent Station (CN), Short Hills (RT), Millburn (MB), Maplewood (MW), Morristown (MR), Basking Ridge (BI), Bernardsville (BV), Murray Hill (MH), New Providence (NV), Newark Broad Street (ND).

**Priority C — after the hackathon:** everything else.

☐ Station list saved as `data/reference/stations_scope.csv` (`code, name, priority`)

---

## 1. Rail fares (40 min)

**Why:** NJ Transit's rail GTFS has no fare files. Fares are priced origin–destination, so they must be looked up per pair.

**Pairs needed:** every Priority A/B origin × {NY Penn, Hoboken, Secaucus, Newark Penn}. Do Priority A first (~12 pairs).

**Method**
1. Use NJ Transit's official fare information on njtransit.com (trip planner or fare lookup). If NJ Transit publishes a rail fare chart (PDF tariff) for the Morris & Essex / Gladstone lines, prefer it: one document covers many pairs.
2. For each pair, record every fare type shown.
3. Collect by hand. With a list this short, manual is faster than scraping and avoids terms-of-use issues.
4. **PATH:** one flat fare from PATH's official site. Record it once.

**Schema** → `data/reference/fares_rail.csv`
```
origin_code, dest_code, one_way, offpeak_roundtrip, weekly, monthly, source_url, checked_date, notes
```
PATH → `data/reference/fares_path.csv`: `fare_type, amount, source_url, checked_date`

**Verify**
- ☐ Fares increase with distance (Summit → NY should not exceed Chatham → NY if Summit is closer).
- ☐ Spot-check 2 pairs against a second source (the NJ Transit app, or a ticket you've bought).
- ☐ Note which fare types are peak vs off-peak.

**Done when:** all Priority A pairs filled.

---

## 2. Parking (45 min)

**Why:** parking is the cost no existing tool shows, and it varies most between stations.

**Method**
1. Start at NJ Transit's station page for each station (parking section). Many lots are run by the **town or a private operator**, not NJ Transit, so follow links to the municipality's parking page or the operator (e.g. ParkMobile-style operators).
2. **One row per lot**, not per station. Stations often have several lots with different rules.
3. Record whether the lot is **daily (pay-as-you-go)** or **permit-only / resident-only**. Permit-only lots must be excludable, or the tool will recommend parking you can't use.
4. If a rate differs by weekday/weekend or early-bird, record the weekday commuter rate and put the rest in `notes`.
5. If availability is known to be a problem (lot fills by 7 AM), note it. This is a real decision factor.

**Schema** → `data/reference/parking.csv`
```
station_code, lot_name, operator, daily_rate, permit_only, resident_only, payment_method, fills_early, source_url, checked_date, notes
```

**Verify**
- ☐ Every Priority A station has at least one non-permit row, or is explicitly marked "no daily parking".
- ☐ Secaucus is covered (it's the key comparison).
- ☐ No rate is a guess; unknowns are blank with a note.

**Done when:** all Priority A stations filled.

---

## 3. Tolls (15 min) — test Google first

**Why:** tolls apply to some driving legs (e.g. toward Secaucus or into Manhattan) and differ by route.

**Method**
1. Create a Google Cloud project, enable the **Routes API**, create an API key, and **restrict it** (Routes API only). Billing must be enabled; set a budget alert.
2. Make **one test request**: home area → Secaucus Junction, requesting toll information (the Routes API supports toll estimates as an extra computation; check the current docs for the exact field mask and toll-pass options).
3. If toll prices come back: tolls are solved; no manual table needed.
4. If not: build a small manual table only for toll points on your candidate driving routes.

**Fallback schema** → `data/reference/tolls_manual.csv`
```
route_desc, toll_point, ezpass_rate, cash_rate, time_band, source_url, checked_date
```

**Verify**
- ☐ API key works and is restricted; key is in `.env`, not in git.
- ☐ Test response saved to `data/reference/samples/routes_toll_test.json`.

**Done when:** you know whether Google returns tolls for your routes.

---

## 4. Gas price (5 min)

**Method:** take the current **New Jersey average regular gas price** from AAA's gas prices page. One number.

**Also record (optional):** the current IRS standard mileage rate, for a "true cost of driving" mode.

**Schema** → `data/reference/driving_costs.csv`
```
item, value, unit, source_url, checked_date
nj_gas_regular, ..., usd_per_gallon, ..., ...
irs_mileage_rate, ..., usd_per_mile, ..., ...
```
mpg stays a **user setting**, not data.

---

## 5. GTFS: NJ Transit rail + PATH (15 min)

**Method**
1. **NJ Transit rail GTFS:** you already have it (downloaded 2026-10-08, covers 2026-10-01 → 2027-03-29). Unzip to `data/gtfs/njt_rail/`.
2. **PATH GTFS:** get it from the Port Authority's official source if available, otherwise from Transitland's mirror (feed `f-dr5r-path~nj~us`; may need a free Transitland API key). Unzip to `data/gtfs/path/`.
3. Load both with pandas once to confirm they parse.

**Verify**
- ☐ Both feeds have `stops.txt`, `trips.txt`, `stop_times.txt`, `calendar*.txt`.
- ☐ Both feeds' service dates cover **this weekend and next week**.
- ☐ PATH's Hoboken and Newark stops can be located (for transfer links to NJ Transit stations; note the stop IDs).

**Record** → `data/gtfs/README.md`: source, download date, date range covered.

---

## 6. Rail archive snapshot from EC2 (20 min, can run in background)

**Why:** work offline at the venue; venue Wi-Fi is unreliable.

**Method**
1. Copy **only the rail feeds** (skip the bus trip updates at ~1.2 GB/day):
   ```
   rsync -avz ec2:~/njt-poller/data/prod/rail_vehicle_data   ./data/archive/
   rsync -avz ec2:~/njt-poller/data/prod/rail_station_msg    ./data/archive/
   rsync -avz ec2:~/njt-poller/data/prod/rail_station_schedule ./data/archive/
   ```
2. Note the snapshot cut-off time. **Everything before it is training data; everything collected during the hackathon is the test set.**
3. Confirm the poller is **still running** on EC2 after the copy.

**Verify**
- ☐ File counts locally match EC2.
- ☐ A random file opens and decodes locally.
- ☐ Poller still writing new files.
- ☐ Snapshot cut-off time recorded in `data/archive/SNAPSHOT.md`.

---

## 7. Alert sample for Gemini validation (15 min)

**Why:** extraction accuracy must be measured, not assumed. Pulling the sample tonight saves time at the event (this is data, not project code).

**Method**
1. From the `rail_station_msg` archive, extract ~30–40 **unique** messages (dedupe on text). Include a mix: delays, cancellations, Portal Bridge, track work, cross-honoring.
2. Save to `data/reference/alerts_sample.csv` with an empty label column.
3. **Label at the event** (or tonight if time allows): `lines, trains, delay_min, cause, affects_penn (Y/N)`.

---

## 8. Infrastructure exposure map (15 min)

**Why:** feature F6 needs to know which trips depend on fragile infrastructure.

**Method:** a small hand-built table describing which **service patterns** pass through which assets. Build it from NJ Transit's system map and line descriptions, and **verify each row** rather than relying on memory. Working assumptions to check:

- Midtown Direct (Morris & Essex) trains to NY Penn use the Kearny Connection, the **Portal Bridge**, the **North River tunnels**, and **Penn Station**.
- Morris & Essex trains to **Hoboken** avoid the Portal Bridge, the North River tunnels, and Penn Station; the last leg uses **PATH**.
- Boarding at **Secaucus Upper Level** toward NY Penn: confirm whether the Portal Bridge is before or after Secaucus on that route.
- Northeast Corridor / North Jersey Coast trains to NY Penn: Portal Bridge + North River tunnels + Penn Station.

**Schema** → `data/reference/infrastructure.csv`
```
service_pattern, from_station, to_station, portal_bridge, north_river_tunnels, penn_station, path_tunnels, source_url, notes
```

---

## 9. Pitch statistic (10 min)

**Method:** from the Census Bureau's ACS **county-to-county commuting flows** tables (census.gov), sum NJ counties → New York County (Manhattan). One number, one citation, for the opening slide.

→ `data/reference/pitch_stats.md`: the number, the table, the vintage, the URL.

---

## 10. Environment setup (25 min) — no project logic

- ☐ Repo created; `PROJECT_BRIEF.md` and this worksheet committed.
- ☐ `.gitignore`: `.env`, `data/archive/`, `.venv/`, `__pycache__/`
- ☐ Virtual environment: `pandas`, `pyarrow`, `streamlit`, `folium` or `pydeck`, `lightgbm`, `requests`, `python-dotenv`, Gemini SDK
- ☐ `.env` holds: Google Maps key, Gemini key, NJ Transit credentials (if calling live)
- ☐ `streamlit hello` runs
- ☐ One Gemini test call returns a response

---

## Folder layout at the end of tonight

```
crossing/
├── PROJECT_BRIEF.md
├── DATA_ACQUISITION_WORKSHEET.md
├── .env                      (not in git)
└── data/
    ├── reference/
    │   ├── stations_scope.csv
    │   ├── fares_rail.csv
    │   ├── fares_path.csv
    │   ├── parking.csv
    │   ├── driving_costs.csv
    │   ├── tolls_manual.csv  (only if Google doesn't return tolls)
    │   ├── infrastructure.csv
    │   ├── alerts_sample.csv
    │   ├── pitch_stats.md
    │   └── samples/routes_toll_test.json
    ├── gtfs/
    │   ├── njt_rail/
    │   ├── path/
    │   └── README.md
    └── archive/              (rsync snapshot, not in git)
        └── SNAPSHOT.md
```

---

## Timeline

| Time | Task | Section |
|---|---|---|
| 7:15 | Station scope | 0 |
| 7:20 | Start rsync in background | 6 |
| 7:20 | Fares | 1 |
| 8:00 | Parking | 2 |
| 8:45 | Tolls test + gas | 3, 4 |
| 9:05 | GTFS check + PATH download | 5 |
| 9:20 | Infrastructure map + alert sample | 8, 7 |
| 9:35 | Environment setup | 10 |
| 9:55 | Pitch stat if time remains, otherwise skip | 9 |
| **10:00** | **Stop. Pack. Sleep.** | |

**If running behind, cut in this order:** pitch stat (9) → alert labeling (7, do at event) → Priority B stations → infrastructure map (8, do at event). **Never cut:** Priority A fares and parking, the archive snapshot, environment setup.
