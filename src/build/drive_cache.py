"""Drive cache: Google Routes API → data/cache/routes_raw/ → drive_cache.parquet.

python -m src.build.drive_cache            fetch missing calls, then rebuild the parquet
python -m src.build.drive_cache --offline  rebuild the parquet from raw responses only
python -m src.build.drive_cache --dry-run  list the calls that would be made

One computeRoutes call per (destination, departure slot, traffic model):
DRIVE, TRAFFIC_AWARE_OPTIMAL, tolls requested with an NJ E-ZPass. Each raw
response is written to its own JSON file; a call whose file already exists
is skipped, so re-runs are safe and only fill gaps. Build time only: the
request path reads drive_cache.parquet and never calls the API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Point:
    id: str
    lat: float
    lon: float


@dataclass(frozen=True)
class RouteCall:
    origin: Point
    dest: Point
    slot_utc: pd.Timestamp
    model: str

    @property
    def raw_name(self) -> str:
        local = self.slot_utc.tz_convert(config.LOCAL_TZ)
        return f"{self.dest.id}_{local:%Y%m%dT%H%M}_{self.model}.json"


def load_origin(path: Path = config.DEMO_ORIGIN_CSV) -> Point:
    row = pd.read_csv(path, dtype={"origin_id": str}).iloc[0]
    return Point(row["origin_id"], float(row["lat"]), float(row["lon"]))


def load_destinations(path: Path = config.DRIVE_DESTINATIONS_CSV) -> list[Point]:
    df = pd.read_csv(path, dtype={"dest": str})
    return [Point(r.dest, float(r.lat), float(r.lon)) for r in df.itertuples()]


def departure_slots(service_date=config.DEMO_SERVICE_DATE, start_s: int = config.DRIVE_SLOT_START_S,
                    end_s: int = config.DRIVE_SLOT_END_S,
                    step_s: int = config.DRIVE_SLOT_STEP_S) -> list[pd.Timestamp]:
    """Slot departure times in UTC; start and end inclusive, local time of day."""
    midnight = pd.Timestamp(service_date).tz_localize(config.LOCAL_TZ)
    return [(midnight + pd.Timedelta(seconds=s)).tz_convert("UTC")
            for s in range(start_s, end_s + 1, step_s)]


def origin_dir(origin: Point, raw_root: Path = config.ROUTES_RAW_DIR) -> Path:
    """Raw responses per origin, so moving the origin never reuses stale calls."""
    tag = hashlib.sha1(f"{origin.lat:.6f},{origin.lon:.6f}".encode()).hexdigest()[:8]
    return raw_root / f"{origin.id}_{tag}"


def plan_calls(origin: Point, dests: list[Point], slots: list[pd.Timestamp],
               models: tuple[str, ...] = config.ROUTES_TRAFFIC_MODELS) -> list[RouteCall]:
    return [RouteCall(origin, d, s, m) for d in dests for s in slots for m in models]


def request_body(call: RouteCall) -> dict:
    def waypoint(p: Point) -> dict:
        return {"location": {"latLng": {"latitude": p.lat, "longitude": p.lon}}}

    return {
        "origin": waypoint(call.origin),
        "destination": waypoint(call.dest),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "trafficModel": call.model,
        "departureTime": call.slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extraComputations": ["TOLLS"],
        "routeModifiers": {"tollPasses": list(config.ROUTES_TOLL_PASSES)},
    }


def fetch_missing(calls: list[RouteCall], out_dir: Path, api_key: str) -> int:
    """Call the API for every call without a raw file. Returns the number fetched."""
    import requests  # build-time only

    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [c for c in calls if not (out_dir / c.raw_name).exists()]
    log.info("routes: %d calls planned, %d cached, %d to fetch",
             len(calls), len(calls) - len(todo), len(todo))
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": api_key,
               "X-Goog-FieldMask": config.ROUTES_FIELD_MASK}
    for i, call in enumerate(todo, 1):
        body = request_body(call)
        for attempt in range(config.ROUTES_RETRIES):
            resp = requests.post(config.ROUTES_API_URL, json=body, headers=headers,
                                 timeout=config.ROUTES_TIMEOUT_S)
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            log.warning("routes: %s on %s, retry %d", resp.status_code, call.raw_name, attempt + 1)
            time.sleep(2 ** attempt)
        if resp.status_code != 200:
            raise RuntimeError(f"Routes API {resp.status_code} for {call.raw_name}: {resp.text[:500]}")
        record = {"request": body, "response": resp.json(),
                  "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        (out_dir / call.raw_name).write_text(json.dumps(record, indent=1), encoding="utf-8")
        if i % 25 == 0 or i == len(todo):
            log.info("routes: fetched %d/%d", i, len(todo))
        time.sleep(0.05)
    return len(todo)


def _money_usd(prices: list[dict]) -> float:
    usd = [p for p in prices if p.get("currencyCode") == "USD"]
    if not usd:
        raise ValueError(f"No USD toll price in {prices}")
    return float(usd[0].get("units", 0)) + float(usd[0].get("nanos", 0)) / 1e9


def parse_raw(record: dict, call: RouteCall) -> dict:
    """One raw response → one drive_cache row.

    toll_status: 'none' (no tollInfo: Google expects no toll), 'priced'
    (estimatedPrice present), 'unpriced' (tolls expected, price unknown → NaN).
    """
    routes = record["response"].get("routes") or []
    if not routes:
        raise ValueError(f"No route in response for {call.raw_name}")
    r = routes[0]
    toll = r.get("travelAdvisory", {}).get("tollInfo")
    if toll is None:
        toll_usd, status = 0.0, "none"
    elif toll.get("estimatedPrice"):
        toll_usd, status = _money_usd(toll["estimatedPrice"]), "priced"
    else:
        toll_usd, status = float("nan"), "unpriced"
    return {
        "origin_id": call.origin.id,
        "dest": call.dest.id,
        "slot_utc": call.slot_utc,
        "slot_local": call.slot_utc.tz_convert(config.LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        "model": call.model,
        "duration_s": float(r["duration"].rstrip("s")),
        "distance_m": float(r.get("distanceMeters", float("nan"))),
        "toll_usd": toll_usd,
        "toll_status": status,
        "polyline": r.get("polyline", {}).get("encodedPolyline"),
        "fetched_at": pd.Timestamp(record["fetched_at"]),
    }


def build_cache_table(calls: list[RouteCall], out_dir: Path) -> pd.DataFrame:
    rows, missing = [], []
    for call in calls:
        path = out_dir / call.raw_name
        if not path.exists():
            missing.append(call.raw_name)
            continue
        rows.append(parse_raw(json.loads(path.read_text(encoding="utf-8")), call))
    if missing:
        log.warning("drive cache: %d planned calls have no raw response, e.g. %s",
                    len(missing), missing[:3])
    table = pd.DataFrame(rows)
    log.info("drive cache: %d rows; toll status by dest:\n%s", len(table),
             pd.crosstab(table["dest"], table["toll_status"]).to_string() if len(table) else "-")
    return table


def summary_at(table: pd.DataFrame, local_hhmm: str = "07:00") -> pd.DataFrame:
    """Per destination at one slot: BEST_GUESS (p50 proxy) and OPTIMISTIC–PESSIMISTIC spread, minutes."""
    at = table[table["slot_local"].str.endswith(local_hhmm)]
    wide = at.pivot_table(index="dest", columns="model", values="duration_s") / 60
    out = pd.DataFrame({
        "best_guess_min": wide["BEST_GUESS"],
        "optimistic_min": wide["OPTIMISTIC"],
        "pessimistic_min": wide["PESSIMISTIC"],
        "spread_min": wide["PESSIMISTIC"] - wide["OPTIMISTIC"],
    }).round(1)
    tolls = at[at["model"] == "BEST_GUESS"].set_index("dest")[["toll_usd", "toll_status"]]
    return out.join(tolls)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="no API calls; parse raw files only")
    parser.add_argument("--dry-run", action="store_true", help="print planned calls and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    origin = load_origin()
    calls = plan_calls(origin, load_destinations(), departure_slots())
    out_dir = origin_dir(origin)
    if args.dry_run:
        todo = [c for c in calls if not (out_dir / c.raw_name).exists()]
        print(f"{len(calls)} planned, {len(todo)} to fetch; e.g. {[c.raw_name for c in todo[:3]]}")
        return
    if not args.offline:
        from dotenv import load_dotenv

        for env in (config.REPO_ROOT / ".env", config.DATA_DIR.parent / ".env"):
            load_dotenv(env)
        key = os.environ.get(config.ROUTES_API_KEY_ENV)
        if not key:
            raise SystemExit(f"{config.ROUTES_API_KEY_ENV} is not set (env or .env)")
        fetch_missing(calls, out_dir, key)

    table = build_cache_table(calls, out_dir)
    config.ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    table.to_parquet(config.DRIVE_CACHE_PARQUET, index=False)
    log.info("wrote %s", config.DRIVE_CACHE_PARQUET)
    if len(table):
        log.info("at 07:00 local:\n%s", summary_at(table).to_string())


if __name__ == "__main__":
    main()
