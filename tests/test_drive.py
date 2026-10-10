import numpy as np
import pandas as pd
import pytest

from src.build import drive_cache as dc
from src.contracts import AssumptionFlag
from src.scoring.drive import lookup, sample_drive_s

ORIGIN = dc.Point("demo", 40.74, -74.38)
DEST = dc.Point("SE", 40.76, -74.07)


def cache(durations: dict[str, list[float]], slots=("07:00", "07:15")) -> pd.DataFrame:
    rows = []
    for model, values in durations.items():
        for hhmm, d in zip(slots, values):
            t = pd.Timestamp(f"2026-10-13 {hhmm}", tz="America/New_York").tz_convert("UTC")
            rows.append({"dest": "SE", "slot_utc": t, "model": model, "duration_s": d,
                         "distance_m": 30000.0, "toll_usd": 2.81})
    return pd.DataFrame(rows)


def local(hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"2026-10-13 {hhmm}", tz="America/New_York").tz_convert("UTC")


def test_fourteen_slots_and_252_calls():
    slots = dc.departure_slots()
    assert len(slots) == 14
    assert slots[0].tz_convert("America/New_York").strftime("%H:%M") == "05:30"
    assert slots[-1].tz_convert("America/New_York").strftime("%H:%M") == "08:45"
    assert len(dc.plan_calls(ORIGIN, [DEST] * 6, slots)) == 252


def test_request_body_traffic_and_tolls():
    call = dc.RouteCall(ORIGIN, DEST, local("07:00"), "PESSIMISTIC")
    body = dc.request_body(call)
    assert body["routingPreference"] == "TRAFFIC_AWARE_OPTIMAL"
    assert body["trafficModel"] == "PESSIMISTIC"
    assert body["departureTime"] == "2026-10-13T11:00:00Z"
    assert body["extraComputations"] == ["TOLLS"]
    assert call.raw_name == "SE_20261013T0700_PESSIMISTIC.json"


@pytest.mark.parametrize("toll_info,expected_usd,status", [
    (None, 0.0, "none"),
    ({"estimatedPrice": [{"currencyCode": "USD", "units": "2", "nanos": 810000000}]}, 2.81, "priced"),
    ({}, float("nan"), "unpriced"),
])
def test_parse_raw_toll_status(toll_info, expected_usd, status):
    route = {"duration": "1830s", "distanceMeters": 30000}
    if toll_info is not None:
        route["travelAdvisory"] = {"tollInfo": toll_info}
    rec = {"response": {"routes": [route]}, "fetched_at": "2026-10-10T19:00:00Z"}
    row = dc.parse_raw(rec, dc.RouteCall(ORIGIN, DEST, local("07:00"), "BEST_GUESS"))
    assert row["duration_s"] == 1830 and row["toll_status"] == status
    assert row["toll_usd"] == pytest.approx(expected_usd, nan_ok=True)


def test_linear_interpolation_between_slots():
    c = cache({"OPTIMISTIC": [1000, 1200], "BEST_GUESS": [1200, 1600], "PESSIMISTIC": [1500, 2100]})
    est = lookup(c, "SE", local("07:07:30"))
    assert (est.low_s, est.mode_s, est.high_s) == (1100, 1400, 1800)
    assert AssumptionFlag.DRIVE_SLOT_EXTRAPOLATED not in est.flags


def test_outside_range_uses_nearest_slot_and_flags():
    c = cache({"OPTIMISTIC": [1000, 1200], "BEST_GUESS": [1200, 1600], "PESSIMISTIC": [1500, 2100]})
    est = lookup(c, "SE", local("09:30"))
    assert est.mode_s == 1600 and AssumptionFlag.DRIVE_SLOT_EXTRAPOLATED in est.flags


def test_crossed_models_are_sorted():
    # Seen live: BEST_GUESS above PESSIMISTIC on a 1-minute drive.
    c = cache({"OPTIMISTIC": [48, 48], "BEST_GUESS": [54, 54], "PESSIMISTIC": [50, 50]})
    est = lookup(c, "SE", local("07:00"))
    assert (est.low_s, est.mode_s, est.high_s) == (48, 50, 54)
    s = sample_drive_s(est, 1000, np.random.default_rng(0))
    assert s.min() >= 48 and s.max() <= 54


def test_degenerate_variance_flagged_and_constant():
    c = cache({"OPTIMISTIC": [600, 600], "BEST_GUESS": [600, 600], "PESSIMISTIC": [600, 600]})
    est = lookup(c, "SE", local("07:05"))
    assert AssumptionFlag.DRIVE_VARIANCE_DEGENERATE in est.flags
    assert np.all(sample_drive_s(est, 50, np.random.default_rng(0)) == 600)


def test_sampling_deterministic_with_seed():
    c = cache({"OPTIMISTIC": [1000, 1000], "BEST_GUESS": [1200, 1200], "PESSIMISTIC": [1500, 1500]})
    est = lookup(c, "SE", local("07:00"))
    a = sample_drive_s(est, 100, np.random.default_rng(3))
    b = sample_drive_s(est, 100, np.random.default_rng(3))
    assert np.array_equal(a, b)
