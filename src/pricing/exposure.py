"""Exposure tagger: which fragile assets a Candidate's legs traverse (infra.csv).

RAIL legs: infra.csv rows for (from_station, to_station) whose service
pattern belongs to the leg's GTFS route ("{route_long_name} → ..."). If the
matching patterns disagree, the union is used and a note is added.
PATH legs: infra.csv when it covers the pair, else the static rule
{PATH_TUNNELS}. DRIVE / WALK legs carry no modeled exposure.
Candidate exposure = union over the planned legs; if any catch-next option's
exposure differs, the Candidate gets the `exposure_varies` flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pandas as pd

from src import config
from src.contracts import Candidate, InfraAsset, Leg, LegMode

INFRA_COLUMNS: dict[str, InfraAsset] = {
    "portal_bridge": InfraAsset.HACKENSACK_PORTAL,
    "north_river_tunnels": InfraAsset.NORTH_RIVER_TUNNELS,
    "penn_station": InfraAsset.NY_PENN,
    "path_tunnels": InfraAsset.PATH_TUNNELS,
}


def load_infra(path: Path = config.INFRA_CSV) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _assets(rows: pd.DataFrame) -> list[frozenset[InfraAsset]]:
    return [frozenset(a for col, a in INFRA_COLUMNS.items() if r[col] == "1") for _, r in rows.iterrows()]


def leg_exposure(leg: Leg, infra: pd.DataFrame,
                 route_names: Mapping[str, str]) -> tuple[frozenset[InfraAsset], list[str]]:
    """Assets one leg traverses, plus notes on how they were determined."""
    if leg.mode in (LegMode.DRIVE, LegMode.WALK):
        return frozenset(), []
    rows = infra[(infra["from_station"] == leg.from_stop) & (infra["to_station"] == leg.to_stop)]
    if leg.mode is LegMode.PATH:
        if rows.empty:
            return frozenset({InfraAsset.PATH_TUNNELS}), [
                f"{leg.from_stop}→{leg.to_stop}: not in infra.csv; static rule PATH_TUNNELS"]
        found = frozenset().union(*_assets(rows))
        return found, [] if found == {InfraAsset.PATH_TUNNELS} else [
            f"{leg.from_stop}→{leg.to_stop}: infra.csv gives {sorted(a.value for a in found)}"]

    name = route_names.get(leg.gtfs_line or "", "")
    own = rows[rows["service_pattern"].str.startswith(f"{name} →")] if name else rows.iloc[0:0]
    notes = []
    if own.empty:
        if rows.empty:
            raise KeyError(f"No infra.csv row for rail leg {leg.from_stop}→{leg.to_stop}")
        notes.append(f"{leg.from_stop}→{leg.to_stop}: no pattern for route {leg.gtfs_line}; any line used")
        own = rows
    sets = _assets(own)
    if len(set(sets)) > 1:
        notes.append(f"{leg.from_stop}→{leg.to_stop}: patterns disagree; union used")
    return frozenset().union(*sets), notes


def legs_exposure(legs, infra, route_names) -> tuple[frozenset[InfraAsset], list[str]]:
    assets, notes = frozenset(), []
    for leg in legs:
        a, n = leg_exposure(leg, infra, route_names)
        assets |= a
        notes += n
    return assets, notes


def tag_candidate(c: Candidate, infra: pd.DataFrame, route_names: Mapping[str, str]) -> Candidate:
    """Return the Candidate with exposure, exposure_varies flag and notes filled in."""
    exposure, notes = legs_exposure(c.legs, infra, route_names)
    flags = set(c.flags)
    if c.access_station == "DRIVE":
        notes.append("road Hudson crossing (Lincoln / Holland tunnel) not modeled")
    if not c.legs:
        notes.append("no eligible train: no exposure to tag")
    planned_onward = legs_exposure(c.catch_next[0], infra, route_names)[0] if c.catch_next else None
    if any(legs_exposure(o, infra, route_names)[0] != planned_onward for o in c.catch_next[1:]):
        flags.add("exposure_varies")
    return replace(c, exposure=exposure, flags=flags, notes=(*c.notes, *dict.fromkeys(notes)))
