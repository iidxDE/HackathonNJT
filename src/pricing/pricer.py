"""Pricer: base cost of one commuting day (round trip) per Candidate, USD.

Phase 1 rules (one-way peak fares only; passes deferred to Phase 3):
  fare      2 × NJT one-way adult fare (board → alight of the planned train)
  path_fare 2 × PATH single ride when the journey uses PATH, unless a dated
            path_fares.csv rule covers the travel date and rail route
            (Portal cutover cross-honoring: $0)
  parking   cheapest daily lot at the parking station with permit_only and
            resident_only both False (non-resident rate by default)
  tolls     2 × Routes API toll at the planned leave_home (BEST_GUESS slot)
  fuel      2 × distance (BEST_GUESS) / 1609.34 / mpg × NJ regular gas price
Every unknown is None with a note; every known value carries its source.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import wraps
from datetime import date
from pathlib import Path

import pandas as pd

from src import config
from src.contracts import Candidate, CostBreakdown, CostItem, LegMode
from src.scoring.drive import DestDrive, lookup

DriveCache = pd.DataFrame | dict[str, DestDrive]  # raw table or index_drive_cache()

METERS_PER_MILE = 1609.34
ROUND_TRIP = 2


@dataclass(frozen=True, eq=False)
class PriceTables:
    fares: pd.DataFrame  # njt_fares.csv
    path_fares: pd.DataFrame  # path_fares.csv
    parking: pd.DataFrame  # parking.csv
    driving_costs: pd.DataFrame  # driving_costs.csv
    drive_dests: pd.DataFrame  # drive_destinations.csv
    memo: dict = field(default_factory=dict, repr=False)  # lookups below; tables are read-only


def _memoized(fn):
    """Cache a pure lookup on the PriceTables it reads (results are immutable)."""
    @wraps(fn)
    def inner(tables: PriceTables, *args, **kwargs):
        key = (fn.__name__, args, tuple(sorted(kwargs.items())))
        if key not in tables.memo:
            tables.memo[key] = fn(tables, *args, **kwargs)
        return tables.memo[key]
    return inner


def load_price_tables(fares: Path = config.FARES_CSV, path_fares: Path = config.PATH_FARES_CSV,
                      parking: Path = config.PARKING_CSV, driving: Path = config.DRIVING_COSTS_CSV,
                      dests: Path = config.DRIVE_DESTINATIONS_CSV) -> PriceTables:
    def read(p: Path) -> pd.DataFrame:
        return pd.read_csv(p, dtype=str, keep_default_na=False)

    return PriceTables(read(fares), read(path_fares), read(parking), read(driving), read(dests))


def _flag(value: str) -> bool:
    return str(value).strip().lower() == "true"


def _source(row: pd.Series) -> tuple[str, str | None]:
    return row["source_url"], (row["checked_date"] or None)


@_memoized
def fare_item(tables: PriceTables, origin: str, dest: str) -> CostItem:
    rows = tables.fares[(tables.fares["origin_code"] == origin) & (tables.fares["dest_code"] == dest)]
    if rows.empty:
        return CostItem(None, note=f"no {origin}→{dest} fare in njt_fares.csv")
    direct = rows[rows["via"] == ""]
    via_note = ""
    if direct.empty:  # only routed variants: the one-seat ride is the cheapest routing
        row = rows.loc[rows["one_way_adult"].astype(float).idxmin()]
        via_note = f" (via {row['via']}, cheapest of {len(rows)} routings)"
    else:
        row = direct.iloc[0]
    url, checked = _source(row)
    note = f"2 × one-way {row['one_way_adult']}{via_note}"
    if not checked:
        note += f"; fare source not dated ({url})"
    return CostItem(ROUND_TRIP * float(row["one_way_adult"]), url, checked, note)


@_memoized
def path_fare_item(tables: PriceTables, travel_date: date | None = None,
                   rail_route: str | None = None, rail_alight: str | None = None) -> CostItem:
    """2 × PATH fare. A dated rule (valid_from..valid_to, weekdays_only, `lines` = GTFS
    route ids) overrides it when the rail leg ends at HB on one of those lines
    (Portal cutover cross-honoring)."""
    fares = tables.path_fares
    not_covered = ""
    if travel_date is not None and rail_alight == "HB" and "valid_from" in fares:
        ymd = travel_date.isoformat()
        for _, rule in fares[fares["valid_from"] != ""].iterrows():
            routes = set(filter(None, rule["lines"].split(";")))
            in_force = (rule["valid_from"] <= ymd <= rule["valid_to"]
                        and (rule["weekdays_only"].lower() != "true" or travel_date.weekday() < 5))
            if in_force and (not routes or rail_route in routes):
                url, checked = _source(rule)
                return CostItem(ROUND_TRIP * float(rule["amount"]), url, checked,
                                f"{rule['fare_type']} {rule['valid_from']}–{rule['valid_to']}: "
                                f"{rule['notes']}")
            if in_force:  # e.g. Main/Bergen to HB: not a diverted Midtown Direct rider
                not_covered += (f"; not cross-honored: route {rail_route} is not in {rule['fare_type']} "
                                f"lines {rule['lines'].replace(';', '/')} ({rule['source_url']})")
    row = fares[fares["fare_type"] == "single_ride"].iloc[0]
    url, checked = _source(row)
    return CostItem(ROUND_TRIP * float(row["amount"]), url, checked,
                    f"2 × single ride {row['amount']}{not_covered}")


@_memoized
def parking_item(tables: PriceTables, station: str, resident: bool = False,
                 park_hhmm: str | None = None) -> tuple[CostItem, set[str]]:
    """Cheapest usable daily lot. park_hhmm (local "HH:MM") applies non_resident_from limits.

    Flags: no_daily_parking (station not collected), parking_unavailable (lots known,
    none usable by this rider at this time), parking_rate_unknown (usable, no rate).
    """
    rate_col = "resident_rate_daily" if resident else "non_resident_rate_daily"
    lots = tables.parking[tables.parking["station_code"] == station]
    if lots.empty:
        return CostItem(None, note=f"no parking data for {station} in parking.csv (not collected)"),             {"no_daily_parking"}

    def is_true(col: str) -> pd.Series:
        return lots[col].str.strip().str.lower() == "true"

    usable = ~is_true("permit_only") & ~is_true("resident_only")
    if not resident and park_hhmm and "non_resident_from" in lots:
        start = lots["non_resident_from"]
        usable &= (start == "") | (start <= park_hhmm)
    open_lots = lots[usable]
    if open_lots.empty:
        why = "; ".join(f"{r.lot_name}: " + ("permit/residents only" if _flag(r.permit_only) or _flag(r.resident_only)
                                             else f"non-residents from {r.non_resident_from}")
                        for r in lots.itertuples())
        return CostItem(None, note=f"no {'resident' if resident else 'non-resident'} daily parking at {station}"
                                   f"{f' when parking at {park_hhmm}' if park_hhmm else ''} ({why})"),             {"parking_unavailable"}
    rated = open_lots[open_lots[rate_col] != ""]
    if rated.empty:
        ops = ", ".join(sorted(set(open_lots["operator"])))
        return CostItem(None, note=f"no published daily rate at {station} ({ops}; variable pricing)"), \
            {"parking_rate_unknown"}
    row = rated.loc[rated[rate_col].astype(float).idxmin()]
    url, checked = _source(row)
    flags = {"fills_early"} if _flag(row["fills_early"]) else set()
    note = f"{row['lot_name']}, {'resident' if resident else 'non-resident'} daily rate"
    driven_to = tables.drive_dests.loc[tables.drive_dests["station_code"] == station, "lot_name"]
    if len(driven_to) and driven_to.iloc[0] != row["lot_name"]:
        note += f"; drive cache routes to {driven_to.iloc[0]}"
    if flags:
        note += "; lot fills early (no fill time modeled)"
    return CostItem(float(row[rate_col]), url, checked, note), flags


def _local_date(leg) -> date | None:
    return None if leg is None else pd.Timestamp(leg.sched_dep).tz_convert(config.LOCAL_TZ).date()


def _gas(tables: PriceTables) -> pd.Series:
    return tables.driving_costs[tables.driving_costs["item"] == "nj_gas_regular"].iloc[0]


def drive_items(tables: PriceTables, drive_cache: DriveCache, c: Candidate, mpg: float,
                inbound_toll_only: bool = False) -> tuple[CostItem, CostItem]:
    """(tolls, fuel) from the access drive at leave_home."""
    drive = c.legs[0] if c.legs and c.legs[0].mode is LegMode.DRIVE else None
    if drive is None or c.leave_home is None:
        note = "no planned drive (no eligible train)"
        return CostItem(None, note=note), CostItem(None, note=note)
    est = lookup(drive_cache, drive.to_stop.removeprefix("LOT:"), c.leave_home)
    trips = 1 if inbound_toll_only else ROUND_TRIP
    if pd.isna(est.toll_usd):
        tolls = CostItem(None, note="Google expects tolls but returned no price")
    elif est.toll_usd == 0:
        tolls = CostItem(0.0, config.ROUTES_API_URL, est.fetched_at or None, "no toll returned")
    else:
        why = "inbound only" if inbound_toll_only else "2 × inbound; return toll assumed equal"
        tolls = CostItem(trips * est.toll_usd, config.ROUTES_API_URL, est.fetched_at or None,
                         f"{why} ({est.toll_usd:.2f}, NJ E-ZPass, BEST_GUESS slot)")
    gas = _gas(tables)
    miles = ROUND_TRIP * est.distance_m / METERS_PER_MILE
    fuel = CostItem(miles / mpg * float(gas["value"]), gas["source_url"], gas["checked_date"] or None,
                    f"{miles:.1f} mi round trip / {mpg:g} mpg (assumed) × {gas['value']} USD/gal")
    return tolls, fuel


def price_candidate(c: Candidate, tables: PriceTables, drive_cache: DriveCache,
                    mpg: float = config.DEFAULT_MPG, resident: bool = False) -> Candidate:
    """Return the Candidate with costs filled in."""
    if c.access_station == "DRIVE":
        tolls, fuel = drive_items(tables, drive_cache, c, mpg, inbound_toll_only=True)
        costs = CostBreakdown(
            fare=CostItem(None, note="no transit fare (drive)"),
            parking=CostItem(None, note="Midtown garage rate not collected"),
            tolls=replace(tolls, note=tolls.note + "; may include the Manhattan congestion charge"),
            fuel=fuel, priced=False, flags={"not_priced"})
        return replace(c, costs=costs)

    rail = c.planned_train
    rails = [leg for leg in c.legs if leg.mode is LegMode.RAIL]
    last = rails[-1] if rails else None  # fare is O–D: first rail boarding → last rail alighting
    origin, dest = (rail.from_stop, last.to_stop) if rail else (c.access_station, config.DESTINATION_STATION)
    alias = config.FARE_STATION_ALIAS
    fare = fare_item(tables, alias.get(origin, origin), alias.get(dest, dest))
    if origin in alias or dest in alias:
        fare = replace(fare, note=f"{fare.note}; {'/'.join(s for s in (origin, dest) if s in alias)} "
                                  f"priced as {'/'.join(alias[s] for s in (origin, dest) if s in alias)} "
                                  "(same fare station)")
    uses_path = any(leg.mode is LegMode.PATH for leg in c.legs)
    park_hhmm = (pd.Timestamp(c.legs[0].sched_arr).tz_convert(config.LOCAL_TZ).strftime("%H:%M")
                 if c.legs and c.legs[0].mode is LegMode.DRIVE else None)
    parking, flags = parking_item(tables, c.access_station, resident, park_hhmm)
    tolls, fuel = drive_items(tables, drive_cache, c, mpg)
    costs = CostBreakdown(fare=fare, parking=parking, tolls=tolls, fuel=fuel,
                          path_fare=path_fare_item(tables, _local_date(rail), last.gtfs_line if last else None,
                                                   last.to_stop if last else None)
                          if uses_path else None, flags=flags)
    return replace(c, costs=costs)
