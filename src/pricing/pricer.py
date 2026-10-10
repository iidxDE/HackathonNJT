"""Pricer: base cost of one commuting day (round trip) per Candidate, USD.

Phase 1 rules (one-way peak fares only; passes deferred to Phase 3):
  fare      2 × NJT one-way adult fare (board → alight of the planned train)
  path_fare 2 × PATH single ride, when the journey uses PATH
  parking   cheapest daily lot at the parking station with permit_only and
            resident_only both False (non-resident rate by default)
  tolls     2 × Routes API toll at the planned leave_home (BEST_GUESS slot)
  fuel      2 × distance (BEST_GUESS) / 1609.34 / mpg × NJ regular gas price
Every unknown is None with a note; every known value carries its source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pandas as pd

from src import config
from src.contracts import Candidate, CostBreakdown, CostItem, LegMode
from src.scoring.drive import lookup

METERS_PER_MILE = 1609.34
ROUND_TRIP = 2


@dataclass(frozen=True)
class PriceTables:
    fares: pd.DataFrame  # njt_fares.csv
    path_fares: pd.DataFrame  # path_fares.csv
    parking: pd.DataFrame  # parking.csv
    driving_costs: pd.DataFrame  # driving_costs.csv
    drive_dests: pd.DataFrame  # drive_destinations.csv


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


def fare_item(tables: PriceTables, origin: str, dest: str) -> CostItem:
    rows = tables.fares[(tables.fares["origin_code"] == origin) & (tables.fares["dest_code"] == dest)]
    direct = rows[rows["via"] == ""]
    if direct.empty and len(rows) > 1:
        return CostItem(None, note=f"{origin}→{dest} fare depends on routing ({len(rows)} via rows)")
    rows = direct if len(direct) else rows
    if rows.empty:
        return CostItem(None, note=f"no {origin}→{dest} fare in njt_fares.csv")
    row = rows.iloc[0]
    url, checked = _source(row)
    note = f"2 × one-way {row['one_way_adult']}"
    if not checked:
        note += f"; fare source not dated ({url})"
    return CostItem(ROUND_TRIP * float(row["one_way_adult"]), url, checked, note)


def path_fare_item(tables: PriceTables) -> CostItem:
    row = tables.path_fares[tables.path_fares["fare_type"] == "single_ride"].iloc[0]
    url, checked = _source(row)
    return CostItem(ROUND_TRIP * float(row["amount"]), url, checked,
                    f"2 × single ride {row['amount']}; cross-honoring for Midtown Direct riders "
                    "diverted to Hoboken may make this $0 (unverified)")


def parking_item(tables: PriceTables, station: str, resident: bool = False) -> tuple[CostItem, set[str]]:
    rate_col = "resident_rate_daily" if resident else "non_resident_rate_daily"
    lots = tables.parking[tables.parking["station_code"] == station]
    def is_true(col: str) -> pd.Series:
        return lots[col].str.strip().str.lower() == "true"

    open_lots = lots[~is_true("permit_only") & ~is_true("resident_only")]
    if open_lots.empty:
        return CostItem(None, note=f"no daily (non-permit) lot for {station} in parking.csv "
                                   "(not collected)"), {"no_daily_parking"}
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


def _gas(tables: PriceTables) -> pd.Series:
    return tables.driving_costs[tables.driving_costs["item"] == "nj_gas_regular"].iloc[0]


def drive_items(tables: PriceTables, drive_cache: pd.DataFrame, c: Candidate, mpg: float,
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


def price_candidate(c: Candidate, tables: PriceTables, drive_cache: pd.DataFrame,
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
    origin, dest = (rail.from_stop, rail.to_stop) if rail else (c.access_station, config.DESTINATION_STATION)
    uses_path = any(leg.mode is LegMode.PATH for leg in c.legs)
    parking, flags = parking_item(tables, c.access_station, resident)
    tolls, fuel = drive_items(tables, drive_cache, c, mpg)
    costs = CostBreakdown(fare=fare_item(tables, origin, dest), parking=parking, tolls=tolls, fuel=fuel,
                          path_fare=path_fare_item(tables) if uses_path else None, flags=flags)
    return replace(c, costs=costs)
