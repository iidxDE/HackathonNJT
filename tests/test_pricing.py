from datetime import date, timedelta

import pandas as pd
import pytest

from src.contracts import Candidate, InfraAsset, Leg, LegMode
from src.pricing.exposure import leg_exposure, tag_candidate
from src.pricing.pricer import PriceTables, parking_item, price_candidate
from src.safety_net.candidates import to_utc

TUE = date(2026, 10, 13)
URL = "https://example.test"


def t(hhmm: str):
    h, m = map(int, hhmm.split(":"))
    return to_utc(TUE, h * 3600 + m * 60)


def tables() -> PriceTables:
    fares = pd.DataFrame([
        {"origin_code": "SE", "dest_code": "NY", "one_way_adult": "5.05", "via": "",
         "source_url": "njtransitapp", "checked_date": ""},
        {"origin_code": "CM", "dest_code": "HB", "one_way_adult": "13.05", "via": "",
         "source_url": URL, "checked_date": "2026-10-10"},
    ])
    path = pd.DataFrame([{"fare_type": "single_ride", "amount": "3.25", "source_url": URL,
                          "checked_date": "2026-10-10"}])

    def lot(station, name, rate, permit="False", resident="False", fills=""):
        return {"station_code": station, "lot_name": name, "operator": "Op",
                "resident_rate_daily": rate, "non_resident_rate_daily": rate, "permit_only": permit,
                "resident_only": resident, "fills_early": fills, "source_url": URL,
                "checked_date": "2026-10-09"}

    parking = pd.DataFrame([
        lot("CM", "Lot 1", "8.00", fills="True"),
        lot("ST", "Lot 6", "14.95"), lot("ST", "Lot 14", "20.00"), lot("ST", "Permit", "5.00", permit="True"),
        lot("SE", "ParkFast", ""),
    ])
    driving = pd.DataFrame([{"item": "nj_gas_regular", "value": "4.00", "source_url": URL,
                             "checked_date": "2026-10-09"}])
    dests = pd.DataFrame([{"dest": "ST", "station_code": "ST", "lot_name": "Lot 14"}])
    return PriceTables(fares, path, parking, driving, dests)


def drive_cache(dest: str, distance_m: float, toll: float) -> pd.DataFrame:
    return pd.DataFrame([{"dest": dest, "slot_utc": pd.Timestamp(t(hhmm)), "model": m, "duration_s": 1800.0,
                          "distance_m": distance_m, "toll_usd": toll,
                          "fetched_at": pd.Timestamp("2026-10-10T19:00Z")}
                         for hhmm in ("07:00", "07:15") for m in ("OPTIMISTIC", "BEST_GUESS", "PESSIMISTIC")])


def rail(frm, to, dep, arr, train="3926", line="10"):
    return Leg(LegMode.RAIL, frm, to, t(dep), t(arr), train_number=train, gtfs_line=line,
               direction=0, toward_ny=True)


def se_candidate(catch_next_extra=()) -> Candidate:
    r = rail("SE", "NY", "08:00", "08:13")
    legs = (Leg(LegMode.DRIVE, "ORIGIN", "LOT:SE", t("07:00"), t("07:30")),
            Leg(LegMode.WALK, "LOT:SE", "SE", t("07:52"), t("08:00")), r)
    return Candidate("SE", legs, None, None, "safety_net", catch_next=((r,), *catch_next_extra),
                     leave_home=t("07:00"), label="SE → NY")


def test_round_trip_costs_and_hand_arithmetic():
    c = price_candidate(se_candidate(), tables(), drive_cache("SE", 35_405.0, 2.81), mpg=25)
    k = c.costs
    assert k.fare.usd == pytest.approx(10.10)  # 2 × 5.05
    assert k.tolls.usd == pytest.approx(5.62)  # 2 × 2.81
    miles = 2 * 35_405.0 / 1609.34
    assert k.fuel.usd == pytest.approx(miles / 25 * 4.00)
    assert k.parking.usd is None and "parking_rate_unknown" in k.flags  # SE: no published rate
    assert k.partial and k.total_usd == pytest.approx(10.10 + 5.62 + k.fuel.usd)
    assert "not dated" in k.fare.note  # njtransitapp row has no checked_date
    assert k.fuel.source_url == URL and k.tolls.checked_date == "2026-10-10"


def test_parking_cheapest_open_lot_and_flags():
    item, flags = parking_item(tables(), "ST")
    assert item.usd == 14.95 and "Lot 14" in item.note  # cheaper than the lot driven to
    item, flags = parking_item(tables(), "CM")
    assert item.usd == 8.0 and flags == {"fills_early"}
    item, flags = parking_item(tables(), "RT")
    assert item.usd is None and flags == {"no_daily_parking"} and item.note


def test_hoboken_path_adds_path_fare():
    r = rail("CM", "HB", "07:50", "08:31", train="0872", line="7")
    legs = (Leg(LegMode.DRIVE, "ORIGIN", "LOT:CM", t("07:00"), t("07:01")),
            Leg(LegMode.WALK, "LOT:CM", "CM", t("07:46"), t("07:50")), r,
            Leg(LegMode.WALK, "HB", "PATH:Hoboken", t("08:31"), t("08:36")),
            Leg(LegMode.PATH, "PATH:Hoboken", "PATH:33rd Street", t("08:41"), t("08:56"), gtfs_line="BLU"),
            Leg(LegMode.WALK, "PATH:33rd Street", "NY", t("08:56"), t("09:00")))
    c = Candidate("CM", legs, None, None, "safety_net", leave_home=t("07:00"))
    k = price_candidate(c, tables(), drive_cache("CM", 300.0, 0.0)).costs
    assert k.fare.usd == pytest.approx(26.10) and k.path_fare.usd == pytest.approx(6.50)
    assert k.tolls.usd == 0 and k.tolls.note == "no toll returned"
    assert not k.partial


def test_drive_all_the_way_not_priced_with_inbound_toll():
    legs = (Leg(LegMode.DRIVE, "ORIGIN", "LOT:NYG", t("07:00"), t("07:50")),
            Leg(LegMode.WALK, "LOT:NYG", "NY", t("07:55"), t("08:00")))
    c = Candidate("DRIVE", legs, None, None, "safety_net", leave_home=t("07:00"))
    k = price_candidate(c, tables(), drive_cache("NYG", 43_000.0, 26.17)).costs
    assert not k.priced and k.total_usd is None
    assert k.tolls.usd == pytest.approx(26.17)  # inbound only


INFRA = pd.DataFrame([
    {"service_pattern": "Northeast Corridor → NEW YORK PENN STATION", "from_station": "NP", "to_station": "NY",
     "portal_bridge": "1", "north_river_tunnels": "1", "penn_station": "1", "path_tunnels": "0"},
    {"service_pattern": "Northeast Corridor → NEW YORK PENN STATION", "from_station": "SE", "to_station": "NY",
     "portal_bridge": "0", "north_river_tunnels": "1", "penn_station": "1", "path_tunnels": "0"},
    {"service_pattern": "North Jersey Coast Line → NEW YORK PENN STATION", "from_station": "SE",
     "to_station": "NY", "portal_bridge": "1", "north_river_tunnels": "1", "penn_station": "1",
     "path_tunnels": "0"},  # fictional disagreement, to test exposure_varies
])
ROUTES = {"10": "Northeast Corridor", "11": "North Jersey Coast Line"}


def test_rail_exposure_uses_the_train_route_pattern():
    assets, notes = leg_exposure(rail("SE", "NY", "08:00", "08:13"), INFRA, ROUTES)
    assert assets == {InfraAsset.NORTH_RIVER_TUNNELS, InfraAsset.NY_PENN} and not notes


def test_path_leg_static_rule_when_not_in_infra():
    leg = Leg(LegMode.PATH, "PATH:Hoboken", "PATH:33rd Street", t("08:41"), t("08:56"))
    assets, notes = leg_exposure(leg, INFRA, ROUTES)
    assert assets == {InfraAsset.PATH_TUNNELS} and notes


def test_exposure_varies_when_a_catch_next_train_differs():
    njcl = rail("SE", "NY", "08:20", "08:33", train="3226", line="11")
    c = tag_candidate(se_candidate(catch_next_extra=((njcl,),)), INFRA, ROUTES)
    assert c.exposure == {InfraAsset.NORTH_RIVER_TUNNELS, InfraAsset.NY_PENN}
    assert "exposure_varies" in c.flags
    assert "exposure_varies" not in tag_candidate(se_candidate(), INFRA, ROUTES).flags


@pytest.mark.skipif(not __import__("src.config", fromlist=["x"]).INFRA_CSV.exists(), reason="infra.csv not available")
def test_golden_demo_exposure():
    """Real 10-13 candidates. SE → NY has no Portal (SE is east of the bridges; infra.csv)."""
    from src import config
    from src.gtfs import load_feed
    from src.pricing.exposure import load_infra
    from src.safety_net.candidates import build_candidates

    njt = load_feed(config.GTFS_NJT_RAIL_DIR)
    cands = build_candidates(njt, load_feed(config.GTFS_PATH_DIR), lambda d, x: 1200.0, TUE, 9 * 3600)
    routes = dict(zip(njt.routes["route_id"], njt.routes["route_long_name"]))
    got = {c.label: tag_candidate(c, load_infra(), routes).exposure for c in cands}
    core = {InfraAsset.NORTH_RIVER_TUNNELS, InfraAsset.NY_PENN}
    assert got["SE → NY"] == core
    assert got["NP → NY"] == core | {InfraAsset.HACKENSACK_PORTAL}
    assert got["CM → HB + PATH"] == {InfraAsset.PATH_TUNNELS}
    assert got["Drive all the way (not priced)"] == frozenset()
    assert got["CM → NY"] == frozenset()  # no train on 10-13
