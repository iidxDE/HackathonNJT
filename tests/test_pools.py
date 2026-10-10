from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.build.pools import add_delay_used, build_pools
from src.contracts import DelayField, FallbackLevel, PoolKey
from src.scoring.pools import delay_field_for, period_of, sample

FRI = date(2026, 10, 9)
SAT = date(2026, 10, 10)


def obs_rows(train: str, stops: dict[str, tuple[int, int]], route: str = "7",
             service_date: date = FRI, terminal_local: str = "07:50") -> list[dict]:
    """One run; stops maps stop_code → (delay_s, delay_vs_gtfs_s)."""
    term = pd.Timestamp(f"{service_date} {terminal_local}", tz="America/New_York").tz_convert("UTC")
    return [{"service_date": service_date, "train_number": train, "trip_id": f"t{train}",
             "route_id": route, "toward_ny": True, "terminal_arr_utc": term,
             "stop_code": code, "delay_s": d, "delay_vs_gtfs_s": g}
            for code, (d, g) in stops.items()]


def observations(*runs: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([row for run in runs for row in run])


def ny_local(hh_mm: str) -> datetime:
    return pd.Timestamp(f"2026-10-12 {hh_mm}", tz="America/New_York").tz_convert("UTC").to_pydatetime()


@pytest.mark.parametrize("hh_mm,expected", [
    ("05:59", "early"), ("06:00", "am_peak"), ("09:59", "am_peak"), ("10:00", "midday"),
    ("16:00", "pm_peak"), ("19:29", "pm_peak"), ("19:30", "evening"), ("00:30", "early"),
])
def test_period_boundaries_in_local_time(hh_mm, expected):
    assert period_of(ny_local(hh_mm)) == expected


def test_feed_delay_only_at_configured_stops():
    assert delay_field_for("NY", "feed", ("NY",)) is DelayField.FEED
    assert delay_field_for("HB", "feed", ("NY",)) is DelayField.GTFS
    assert delay_field_for("NY", "gtfs", ("NY",)) is DelayField.GTFS
    obs = observations(obs_rows("1", {"SE": (100, 130), "NY": (110, 890)}))
    used = add_delay_used(obs, "feed", ("NY",)).set_index("stop_code")["delay_used_s"]
    assert used.to_dict() == {"SE": 130.0, "NY": 110.0}


def test_weekend_runs_excluded_and_period_from_terminal():
    obs = observations(obs_rows("1", {"CM": (0, 60), "HB": (0, 120)}),
                       obs_rows("2", {"CM": (0, 60), "HB": (0, 120)}, service_date=SAT),
                       obs_rows("3", {"CM": (0, 60), "HB": (0, 120)}, terminal_local="10:05"))
    pools = build_pools(obs, weekdays_only=True)
    l1 = pools[pools["level"] == FallbackLevel.LINE_TOWARD_NY_PERIOD.value]
    assert set(l1["train_number"]) == {"1", "3"}
    assert l1.groupby("train_number")["period"].first().to_dict() == {"1": "am_peak", "3": "midday"}
    assert len(pools) == 3 * len(l1)  # each run stored once per level


def test_l1_draws_board_and_alight_from_same_run():
    # Run i: board delay i, alight delay 1000 + i. Run 9 lacks CM: not effective at L1.
    runs = [obs_rows(str(i), {"CM": (0, i), "HB": (0, 1000 + i)}) for i in range(8)]
    runs.append(obs_rows("9", {"HB": (0, 5000)}))
    pools = build_pools(observations(*runs))
    samples, usage = sample(pools, "CM", "HB", "7", True, "am_peak", 500, np.random.default_rng(1))
    assert usage.fallback_level is FallbackLevel.LINE_TOWARD_NY_PERIOD
    assert usage.run_count == samples.run_count == 8
    assert usage.pool_key == PoolKey("7", True, "am_peak")
    d = samples.delays_s
    assert d.shape == (500, 2) and not np.isnan(d).any()
    assert np.all(d[:, 1] - d[:, 0] == 1000)
    assert usage.delay_field_at_stop == {"CM": DelayField.GTFS, "HB": DelayField.GTFS}


def test_same_seed_same_samples():
    runs = [obs_rows(str(i), {"SE": (i, i), "NY": (10 * i, 9 * i)}, route="10") for i in range(6)]
    pools = build_pools(observations(*runs))
    a, _ = sample(pools, "SE", "NY", "10", True, "am_peak", 200, np.random.default_rng(7))
    b, _ = sample(pools, "SE", "NY", "10", True, "am_peak", 200, np.random.default_rng(7))
    assert np.array_equal(a.delays_s, b.delays_s)


def test_empty_cell_falls_back_to_l2_with_zero_board_delay():
    # Line 8 has two runs (too few); other lines into HB in the same period make L2.
    runs = [obs_rows(f"8{i}", {"ST": (0, 50), "HB": (0, 300)}, route="8") for i in range(2)]
    runs += [obs_rows(f"5{i}", {"SF": (0, 10), "HB": (0, 60 * i)}, route="5") for i in range(4)]
    pools = build_pools(observations(*runs))
    samples, usage = sample(pools, "ST", "HB", "8", True, "am_peak", 100, np.random.default_rng(0))
    assert usage.fallback_level is FallbackLevel.TOWARD_NY_PERIOD
    assert usage.pool_key == PoolKey(None, True, "am_peak")
    assert usage.run_count == 6
    assert np.all(samples.delays_s[:, 0] == 0)
    assert usage.delay_field_at_stop["ST"] is DelayField.ASSUMED_ZERO


def test_l3_then_no_data():
    runs = [obs_rows(str(i), {"HB": (0, 60)}, route="5", terminal_local="12:00") for i in range(5)]
    pools = build_pools(observations(*runs))
    _, usage = sample(pools, "CM", "HB", "7", True, "am_peak", 10, np.random.default_rng(0))
    assert usage.fallback_level is FallbackLevel.TOWARD_NY and usage.run_count == 5
    samples, usage = sample(pools, "SE", "NY", "10", True, "am_peak", 10, np.random.default_rng(0))
    assert usage.fallback_level is FallbackLevel.NO_DATA and usage.run_count == 0
    assert samples.delays_s.shape == (10, 2) and not samples.delays_s.any()


def test_negative_delays_kept():
    runs = [obs_rows(str(i), {"CM": (0, -120), "HB": (0, -60)}) for i in range(5)]
    samples, _ = sample(build_pools(observations(*runs)), "CM", "HB", "7", True, "am_peak",
                        5, np.random.default_rng(0))
    assert np.all(samples.delays_s == [-120, -60])


def test_l1b_keeps_the_line_before_mixing_lines():
    # Line 7: only 2 runs stop at RT, 6 runs reach HB. Line 5 (another line into HB) is faster.
    runs = [obs_rows(f"7{i}", {"RT": (0, 30), "HB": (0, 240)}) for i in range(2)]
    runs += [obs_rows(f"7x{i}", {"ST": (0, 30), "HB": (0, 240)}) for i in range(4)]
    runs += [obs_rows(f"5{i}", {"SF": (0, 0), "HB": (0, 0)}, route="5") for i in range(20)]
    pools = build_pools(observations(*runs))
    samples, usage = sample(pools, "RT", "HB", "7", True, "am_peak", 200, np.random.default_rng(0))
    assert usage.fallback_level is FallbackLevel.LINE_TOWARD_NY_PERIOD_ALIGHT
    assert usage.pool_key == PoolKey("7", True, "am_peak") and usage.run_count == 6
    assert np.all(samples.delays_s[:, 0] == 0) and np.all(samples.delays_s[:, 1] == 240)
    assert usage.delay_field_at_stop["RT"] is DelayField.ASSUMED_ZERO


def test_stop_coverage_diagnostic_classifies_missing_runs():
    from src.build.pools import stop_coverage_diagnostic

    t = lambda m: pd.Timestamp("2026-10-09T12:00Z") + pd.Timedelta(minutes=m)  # noqa: E731
    obs = pd.DataFrame([
        {"service_date": FRI, "train_number": n, "trip_id": f"t{n}", "stop_code": c, "observed_utc": t(m)}
        for n in ("1", "2", "3", "4") for c, m in (("ST", 0), ("HB", 40))] +
        [{"service_date": FRI, "train_number": "1", "trip_id": "t1", "stop_code": "RT", "observed_utc": t(5)}])
    stop_codes = pd.DataFrame({"trip_id": ["t1", "t2", "t3", "t4"], "stop_code": "RT"})
    raw = pd.DataFrame({"train_number": ["3", "4", "4", "2"], "next_stop_name": "Short Hills",
                        "poll_utc": [t(5), t(6), t(7), t(500)]})  # train 2's record is another day
    cleaned = raw[raw["train_number"] == "4"]
    d = stop_coverage_diagnostic(obs, stop_codes, raw, cleaned, {"Short Hills"})
    assert (d["population"], d["observed"], d["missing"]) == (4, 1, 3)
    assert (d["no_next_stop_record"], d["dropped_by_cleaning"], d["stop_code_mismatch"]) == (1, 1, 1)
