from datetime import datetime, timezone

import numpy as np
import pytest

from src import config
from src.contracts import (
    Candidate,
    CostBreakdown,
    DelaySamples,
    FallbackLevel,
    InfraAsset,
    Leg,
    LegMode,
    PoolKey,
    ScoredOption,
    normalize_train_number,
)


def utc(h: int, m: int) -> datetime:
    """2026-10-12 at h:m UTC (EDT is UTC-4, so 11:00 UTC = 07:00 local)."""
    return datetime(2026, 10, 12, h, m, tzinfo=timezone.utc)


def chatham_candidate() -> Candidate:
    drive = Leg(LegMode.DRIVE, "ORIGIN", "CM", utc(10, 40), utc(10, 50))
    rail = Leg(LegMode.RAIL, "CM", "NY", utc(10, 58), utc(11, 45),
               train_number="0626", gtfs_line="7", direction=1)
    return Candidate(
        access_station="CM",
        legs=(drive, rail),
        costs=CostBreakdown(fuel_usd=0.61, parking_usd=5.50, fare_usd=13.05),
        exposure={InfraAsset.PORTAL_BRIDGE, InfraAsset.NORTH_RIVER_TUNNELS,
                  InfraAsset.PENN_STATION},
        source="safety_net",
    )


def test_hand_built_candidate():
    c = chatham_candidate()
    assert c.legs[1].train_number == "626"
    assert c.base_cost_usd == pytest.approx(19.16)
    assert c.n_transfers == 0
    assert isinstance(c.exposure, frozenset)


@pytest.mark.parametrize("raw,expected", [("0400", "400"), ("400", "400"), ("0", "0"), ("6660", "6660")])
def test_normalize_train_number(raw, expected):
    assert normalize_train_number(raw) == expected


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        Leg(LegMode.WALK, "A", "B", datetime(2026, 10, 12, 7), datetime(2026, 10, 12, 8))


def test_non_contiguous_legs_rejected():
    a = Leg(LegMode.DRIVE, "ORIGIN", "CM", utc(10, 40), utc(10, 50))
    b = Leg(LegMode.WALK, "ST", "NY", utc(11, 0), utc(11, 5))
    with pytest.raises(ValueError):
        Candidate("CM", (a, b), CostBreakdown(), frozenset(), "router")


def test_cross_system_transfer_needs_explicit_walk_leg():
    rail = Leg(LegMode.RAIL, "CM", "HB", utc(11, 0), utc(11, 50),
               train_number="0412", gtfs_line="7", direction=0)
    path = Leg(LegMode.PATH, "PATH:Hoboken", "PATH:33rd Street", utc(12, 0), utc(12, 15))
    with pytest.raises(ValueError):
        Candidate("CM", (rail, path), CostBreakdown(), frozenset(), "safety_net")
    walk = Leg(LegMode.WALK, "HB", "PATH:Hoboken", utc(11, 50), utc(11, 55))
    c = Candidate("CM", (rail, walk, path), CostBreakdown(), {InfraAsset.PATH_TUNNELS}, "safety_net")
    assert c.n_transfers == 1


def test_delay_samples_read_only_and_counts_runs():
    ds = DelaySamples(np.zeros((12, 3)), ("CM", "ST", "NY"),
                      PoolKey("7", True, "am_peak"), FallbackLevel.LINE_TOWARD_NY_PERIOD)
    assert ds.run_count == 12
    with pytest.raises(ValueError):
        ds.delays_s[0, 0] = 1.0


def test_scored_option_validates_probability():
    c = chatham_candidate()
    with pytest.raises(ValueError):
        ScoredOption(c, 1.2, utc(11, 45), utc(11, 55), 0.0, 20.0)


def test_periods_cover_day_without_gaps():
    bounds = [(s, e) for _, s, e in config.PERIODS]
    assert bounds[0][0] == 0 and bounds[-1][1] == config.SECONDS_PER_DAY
    assert all(prev[1] == nxt[0] for prev, nxt in zip(bounds, bounds[1:]))
