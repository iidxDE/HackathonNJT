"""Generate data/reference/infra.csv: which fragile assets each rail leg uses.

python -m src.build.infrastructure [--refresh-osm]

One row per (service pattern, boarding station, downstream alighting station).
service_pattern is "{route_long_name} → {terminal stop_name}" from GTFS.

Asset geometry comes from OpenStreetMap (Overpass API; the same query runs in
overpass-turbo.eu), cached to data/reference/osm/infra_assets.json:
  portal_bridge        ways named Portal Bridge / Portal North Bridge (NEC over
                       the Hackensack; Portal North carries trains since 2026-03)
  north_river_tunnels  ways with tunnel:name = North River Tunnels
  path_tunnels         ways with tunnel:name = Uptown / Downtown Hudson Tubes
  penn_station         leg starts or ends at NY Penn (GTFS stop_code NY)
For each OSM asset a gate is drawn perpendicular to its main axis through the
middle of its length. A leg uses the asset if its GTFS shape, between the two
stops, crosses that gate (segment intersection, independent of point density).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src import config
from src.gtfs import GtfsFeed, load_feed

log = logging.getLogger(__name__)

ASSETS = ("portal_bridge", "north_river_tunnels", "penn_station", "path_tunnels")
PENN_STOP_CODE = "NY"
PENN_SOURCE = "GTFS stop_code NY (New York Penn Station)"

# OSM asset name → (CSV column, feed whose shapes it applies to)
OSM_ASSET_COLUMNS: dict[str, tuple[str, str]] = {
    "Portal Bridge": ("portal_bridge", "njt"),
    "Portal North Bridge": ("portal_bridge", "njt"),
    "North River Tunnels": ("north_river_tunnels", "njt"),
    "Uptown Hudson Tubes": ("path_tunnels", "path"),
    "Downtown Hudson Tubes": ("path_tunnels", "path"),
}
OVERPASS_QUERY = """[out:json][timeout:60];
(
  way["railway"]["bridge"]["bridge:name"~"^Portal( North)? Bridge$"](40.74,-74.12,40.77,-74.07);
  way["railway"]["bridge"]["name"~"^Portal( North)? Bridge$"](40.74,-74.12,40.77,-74.07);
  way["railway"]["tunnel"]["tunnel:name"~"^(North River Tunnels|Uptown Hudson Tubes|Downtown Hudson Tubes)$"](40.70,-74.06,40.78,-73.99);
);
out tags geom;"""

_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LON_EQ = 111_320.0


@dataclass(frozen=True)
class Gate:
    asset_name: str  # OSM name, e.g. "Portal North Bridge"
    column: str  # CSV column
    agency: str  # "njt" or "path"
    lat_lon: tuple[tuple[float, float], tuple[float, float]]
    source_url: str  # representative OSM way


# --- OSM ---------------------------------------------------------------------

def fetch_osm_assets(cache_path: Path = config.OSM_ASSETS_JSON, refresh: bool = False) -> dict:
    """Overpass response for the asset ways; cached on disk, fetched only if missing or refresh."""
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode()
    last_error: Exception | None = None
    for url in config.OVERPASS_URLS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body,
                                             headers={"User-Agent": "Crossing-hackathon/0.1"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                payload["_query"] = OVERPASS_QUERY
                payload["_source"] = url
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
                log.info("osm: fetched %d ways from %s (OSM data as of %s)",
                         len(payload["elements"]), url, payload["osm3s"]["timestamp_osm_base"])
                return payload
            except (OSError, ValueError, KeyError) as exc:  # network, bad JSON, error page
                last_error = exc
                log.info("osm: %s attempt %d failed: %s", url, attempt + 1, exc)
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Overpass fetch failed on all endpoints: {last_error}")


def _asset_name(tags: dict) -> str | None:
    for key in ("tunnel:name", "bridge:name", "name"):
        if tags.get(key) in OSM_ASSET_COLUMNS:
            return tags[key]
    return None


def gates_from_osm(osm: dict, half_width_m: float = config.OSM_GATE_HALF_WIDTH_M) -> list[Gate]:
    """One gate per OSM asset name, through the middle of the asset, across its main axis."""
    ways: dict[str, list[dict]] = {}
    for el in osm["elements"]:
        name = _asset_name(el.get("tags", {}))
        if name and el.get("geometry"):
            ways.setdefault(name, []).append(el)
    missing = set(OSM_ASSET_COLUMNS) - set(ways)
    if missing:
        raise ValueError(f"OSM response lacks assets: {sorted(missing)}")

    gates = []
    for name, els in sorted(ways.items()):
        lat0 = np.mean([p["lat"] for el in els for p in el["geometry"]])
        kx = _M_PER_DEG_LON_EQ * np.cos(np.radians(lat0))
        # Densify every way to ~10 m so the middle is by length, not by node count.
        pts = []
        for el in els:
            xy = np.array([[p["lon"] * kx, p["lat"] * _M_PER_DEG_LAT] for p in el["geometry"]])
            for a, b in zip(xy[:-1], xy[1:]):
                n = max(int(np.linalg.norm(b - a) // 10), 1)
                pts.append(a + np.outer(np.arange(n) / n, b - a))
        pts = np.vstack(pts)
        centroid = pts.mean(axis=0)
        axis = np.linalg.svd(pts - centroid, full_matrices=False)[2][0]
        middle = centroid + axis * np.median((pts - centroid) @ axis)
        normal = np.array([-axis[1], axis[0]])
        ends = [middle + s * half_width_m * normal for s in (-1, 1)]
        lat_lon = tuple((float(y / _M_PER_DEG_LAT), float(x / kx)) for x, y in ends)
        column, agency = OSM_ASSET_COLUMNS[name]
        longest = max(els, key=lambda el: len(el["geometry"]))
        gate = Gate(name, column, agency, lat_lon, f"https://www.openstreetmap.org/way/{longest['id']}")
        log.info("gate %-22s mid (%.5f, %.5f) from %d OSM ways",
                 name, middle[1] / _M_PER_DEG_LAT, middle[0] / kx, len(els))
        gates.append(gate)
    return gates


# --- Geometry on GTFS shapes -------------------------------------------------

def crosses(poly_lat: np.ndarray, poly_lon: np.ndarray,
            gate: tuple[tuple[float, float], tuple[float, float]]) -> np.ndarray:
    """For each polyline segment k (points k → k+1), does it intersect the gate line?"""
    (a_lat, a_lon), (b_lat, b_lon) = gate
    p = np.column_stack([poly_lon, poly_lat])
    p1, p2 = p[:-1], p[1:]
    a, b = np.array([a_lon, a_lat]), np.array([b_lon, b_lat])

    def orient(u, v, w):
        return np.sign((v[..., 0] - u[..., 0]) * (w[..., 1] - u[..., 1])
                       - (v[..., 1] - u[..., 1]) * (w[..., 0] - u[..., 0]))

    return ((orient(p1, p2, a) != orient(p1, p2, b))
            & (orient(a, b, p1) != orient(a, b, p2)))


def _stop_vertex_index(shape: pd.DataFrame, stops: pd.DataFrame) -> np.ndarray:
    """Shape vertex index for each stop, in visit order.

    Uses shape_dist_traveled when both files have it; otherwise the nearest
    vertex at or after the previous stop's vertex.
    """
    if "shape_dist_traveled" in shape and "shape_dist_traveled" in stops \
            and stops["shape_dist_traveled"].notna().all():
        dist = shape["shape_dist_traveled"].to_numpy(float)
        idx = np.searchsorted(dist, stops["shape_dist_traveled"].to_numpy(float), side="left")
        return np.clip(idx, 0, len(shape) - 1)
    lat, lon = shape["shape_pt_lat"].to_numpy(float), shape["shape_pt_lon"].to_numpy(float)
    out, start = [], 0
    for s_lat, s_lon in zip(stops["stop_lat"].astype(float), stops["stop_lon"].astype(float)):
        d = (lat[start:] - s_lat) ** 2 + ((lon[start:] - s_lon) * np.cos(np.radians(s_lat))) ** 2
        start += int(np.argmin(d))
        out.append(start)
    return np.array(out)


def trip_legs(feed: GtfsFeed, gates: list[Gate], station_key: pd.Series,
              excluded_route_ids: tuple[str, ...] = ()) -> pd.DataFrame:
    """All (pattern, from, to) legs along each trip with the asset columns they cross.

    station_key: stop_id → station identifier used in the CSV.
    """
    if feed.shapes is None:
        raise ValueError("Feed has no shapes.txt; exposure needs shape geometry")
    trips = feed.trips[~feed.trips["route_id"].isin(excluded_route_ids)]
    routes = feed.routes.set_index("route_id")["route_long_name"]
    stops = feed.stops.set_index("stop_id")
    stop_names = stops["stop_name"].str.strip()

    st = feed.stop_times[feed.stop_times["trip_id"].isin(trips["trip_id"])].copy()
    if "shape_dist_traveled" in st:
        st["shape_dist_traveled"] = pd.to_numeric(st["shape_dist_traveled"], errors="coerce")
    st = st.join(stops[["stop_lat", "stop_lon"]], on="stop_id").sort_values(["trip_id", "stop_sequence"])
    shapes = feed.shapes.copy()
    shapes["shape_pt_sequence"] = shapes["shape_pt_sequence"].astype(int)
    if "shape_dist_traveled" in shapes:
        shapes["shape_dist_traveled"] = pd.to_numeric(shapes["shape_dist_traveled"], errors="coerce")
    shapes = {k: v.sort_values("shape_pt_sequence") for k, v in shapes.groupby("shape_id")}
    st_by_trip = dict(tuple(st.groupby("trip_id")))
    columns = sorted({g.column for g in gates})

    rows = []
    for trip in trips.itertuples(index=False):
        ts = st_by_trip.get(trip.trip_id)
        shape = shapes.get(trip.shape_id)
        if ts is None or shape is None or len(ts) < 2:
            continue
        vidx = _stop_vertex_index(shape, ts)
        lat = shape["shape_pt_lat"].to_numpy(float)
        lon = shape["shape_pt_lon"].to_numpy(float)
        # cum[col][k] = gate crossings for that column on shape segments before vertex k
        cum = {c: np.zeros(len(lat), dtype=int) for c in columns}
        for g in gates:
            cum[g.column][1:] += np.cumsum(crosses(lat, lon, g.lat_lon))
        keys = station_key.reindex(ts["stop_id"]).to_numpy()
        i, j = np.triu_indices(len(ts), k=1)
        leg = {
            "service_pattern": f"{routes[trip.route_id]} → {stop_names[ts['stop_id'].iloc[-1]]}",
            "from_station": keys[i],
            "to_station": keys[j],
            "trip_id": trip.trip_id,
        }
        for c in columns:
            leg[c] = (cum[c][vidx[j]] - cum[c][vidx[i]]) > 0
        rows.append(pd.DataFrame(leg))
    legs = pd.concat(rows, ignore_index=True)
    for asset in ASSETS:
        if asset not in legs:
            legs[asset] = False
    return legs


# --- Table -------------------------------------------------------------------

def build_infra_table(njt: GtfsFeed, path: GtfsFeed, gates: list[Gate],
                      osm_date: str) -> pd.DataFrame:
    njt_key = njt.stops.set_index("stop_id")["stop_code"].str.strip()
    path_key = "PATH:" + path.stops.set_index("stop_id")["stop_name"].str.strip()
    legs = pd.concat([
        trip_legs(njt, [g for g in gates if g.agency == "njt"], njt_key,
                  config.EXCLUDED_ROUTE_IDS).assign(agency="NJT"),
        trip_legs(path, [g for g in gates if g.agency == "path"], path_key).assign(agency="PATH"),
    ], ignore_index=True)
    legs["penn_station"] = (legs["agency"] == "NJT") & (
        (legs["from_station"] == PENN_STOP_CODE) | (legs["to_station"] == PENN_STOP_CODE))

    keys = ["service_pattern", "from_station", "to_station"]
    agg = legs.groupby(keys).agg(
        **{a: (a, "max") for a in ASSETS},
        **{f"{a}_min": (a, "min") for a in ASSETS},
        n_trips=("trip_id", "size"), agency=("agency", "first"))
    inconsistent = np.column_stack([agg[a] != agg[f"{a}_min"] for a in ASSETS]).any(axis=1)
    if inconsistent.any():
        log.warning("%d legs differ between trips of the same pattern (flagged if any trip crosses)",
                    inconsistent.sum())

    sources: dict[str, list[str]] = {"penn_station": [PENN_SOURCE]}
    for g in gates:
        sources.setdefault(g.column, []).append(g.source_url)
    out = agg.reset_index()
    out["source_url"] = [
        " ".join(u for a in ASSETS if row[a] for u in sources.get(a, [])) or "none crossed"
        for _, row in out.iterrows()
    ]
    out["notes"] = [
        f"{ag} GTFS shapes ({n} trips) vs OSM asset gates (OSM data {osm_date}); "
        f"checked {pd.Timestamp.now(tz=config.LOCAL_TZ).date()}"
        + ("; differs across trips, flagged if any trip crosses" if bad else "")
        for ag, n, bad in zip(out["agency"], out["n_trips"], inconsistent)
    ]
    for a in ASSETS:
        out[a] = out[a].astype(int)
    return out[["service_pattern", *keys[1:], *ASSETS, "source_url", "notes"]]


def log_summary(table: pd.DataFrame) -> None:
    by_pattern = table.groupby("service_pattern")[list(ASSETS)].max()
    log.info("infra: %d legs, %d patterns", len(table), len(by_pattern))
    log.info("assets touched anywhere in each pattern:\n%s", by_pattern.to_string())
    to_ny = table[table["to_station"] == PENN_STOP_CODE]
    for code in config.ACCESS_STATIONS:
        sub = to_ny[to_ny["from_station"] == code][["service_pattern", *ASSETS]]
        log.info("legs %s → NY:\n%s", code, sub.to_string(index=False) if len(sub) else "(none)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh-osm", action="store_true", help="re-query Overpass")
    parser.add_argument("--out", type=Path, default=config.INFRA_CSV)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    osm = fetch_osm_assets(refresh=args.refresh_osm)
    gates = gates_from_osm(osm)
    osm_date = osm["osm3s"]["timestamp_osm_base"][:10]
    table = build_infra_table(load_feed(config.GTFS_NJT_RAIL_DIR),
                              load_feed(config.GTFS_PATH_DIR), gates, osm_date)
    log_summary(table)
    table.to_csv(args.out, index=False, encoding="utf-8")
    log.info("wrote %s", args.out)


if __name__ == "__main__":
    main()
