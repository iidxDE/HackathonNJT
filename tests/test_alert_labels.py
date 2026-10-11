"""3.2a.0: alert label migration + train verification."""

from pathlib import Path

import pytest

from src import config
from src.build import alert_labels as al

FIXTURE = Path(__file__).parent / "fixtures" / "alert_samples_premigration.csv"
needs_gtfs = pytest.mark.skipif(not (config.GTFS_NJT_RAIL_DIR / "stop_times.txt").exists(),
                                reason="rail GTFS not available")


def test_premigration_fixture_round_trips_byte_identical_except_quoted_empties():
    cols, rows = al.read_rows(FIXTURE)
    raw = FIXTURE.read_bytes()
    # the only re-encoding: two empty fields stored as "" in A017 are written unquoted
    assert al.to_csv_text(cols, rows).encode("utf-8") == raw.replace(b',"","",', b",,,")
    assert "\r\n" in next(r["msg_text"] for r in rows if r["alert_id"] == "A017")


def test_migration_keeps_every_value_and_column_order():
    old_cols, old = al.read_rows(FIXTURE)
    cols, new = al.read_rows(config.ALERT_SAMPLES_CSV)
    assert len(new) == len(old) == 34
    renamed = ["lbl_affects_penn_legacy" if c == "lbl_affects_penn" else c for c in old_cols]
    assert cols == renamed + list(al.HUMAN_COLUMNS) + list(al.DERIVED_COLUMNS)
    for o, n in zip(old, new):
        assert n["msg_text"].encode("utf-8") == o["msg_text"].encode("utf-8")  # A017's CRLF included
        assert n["lbl_affects_penn_legacy"] == o["lbl_affects_penn"]
        for c in old_cols:  # lbl_* may change by human review after the migration
            if not c.startswith("lbl_"):
                assert n[c] == o[c], (n["alert_id"], c)
        assert n["lbl_reviewed"] in ("N", "Y")
        names = [x for x in n["lbl_lines"].split(";") if x]
        assert all(x in config.LINE_NAME_TO_ROUTES for x in names)
    a004 = next(r for r in new if r["alert_id"] == "A004")
    assert a004["lbl_lines"] == "Main Line;Bergen County Line"


def test_migrate_is_a_noop_on_the_new_schema():
    cols, rows = al.read_rows(config.ALERT_SAMPLES_CSV)
    assert al.migrate(cols, rows) == (cols, rows)


def test_migration_prefills_proposals_and_never_reviews():
    cols, rows = al.migrate(*al.read_rows(FIXTURE))
    by = {r["alert_id"]: r for r in rows}
    assert (by["A000"]["lbl_kind"], by["A000"]["lbl_cut_at_station"]) == ("cancellation", "Point Pleasant")
    assert (by["A001"]["lbl_kind"], by["A001"]["lbl_direction"]) == ("cancellation", "toward_ny")
    assert by["A017"]["lbl_kind"] == "suspension" and by["A018"]["lbl_direction"] == "toward_ny"
    assert all(by[f"A{i:03d}"]["lbl_kind"] == "" for i in range(20, 34))
    assert all(r["lbl_reviewed"] == "N" for r in rows)


def test_normalize_lines():
    assert al.normalize_lines("Main Line Bergen County Line") == "Main Line;Bergen County Line"
    assert al.normalize_lines("ME Line") == "Morris & Essex Line"
    assert al.normalize_lines("Northeast Corridor Line") == "Northeast Corridor Line"
    assert al.normalize_lines("*MontClair-Boonton Line") == "Montclair-Boonton Line"
    with pytest.raises(ValueError):
        al.normalize_lines("Hogwarts Express")


def test_parse_primary_train_ignores_suggested_train():
    text = ("NJCL train #4384, the 8:26 PM departure from Bay Head, scheduled to arrive in Long Branch at "
            "9:04 PM, was cancelled. Please take train #4392, the 10:48 PM departure from Bay Head.")
    train, seg = al.primary_train(text)
    assert train == "4384"
    assert {(m.station, m.kind, m.clock_s) for m in al.mentions(seg)} == {
        ("Bay Head", "departure", 20 * 3600 + 26 * 60), ("Long Branch", "arrival", 21 * 3600 + 4 * 60)}


@pytest.fixture(scope="module")
def idx():
    return al.load_rail_index()


def _row(alert_id: str) -> dict[str, str]:
    return next(r for r in al.read_rows(FIXTURE)[1] if r["alert_id"] == alert_id)


@needs_gtfs
def test_verify_after_midnight_a003(idx):
    """4393, 12:08 AM from Long Branch, published 00:17 local 10-10 → service day 10-09 at 24:08
    (absolute 10-10 00:08, 9 min before the pubdate: inside [−6 h, +18 h])."""
    d, _ = al.verify(idx, _row("A003"))
    assert d["verify_status"] == "ok"
    assert d["gtfs_service_date"] == "2026-10-09" and d["gtfs_time_at_named_stop"] == "24:08:00"
    assert d["derived_direction"] == d["parity_direction"] == "away_from_ny"


@needs_gtfs
def test_verify_ny_bound_shuttle_a001(idx):
    """4384 Bay Head → Long Branch: toward NY although its terminal is neither NY nor HB."""
    d, _ = al.verify(idx, _row("A001"))
    assert d["verify_status"] == "ok"
    assert d["gtfs_terminal"] == "LB" and d["gtfs_serves_ny"] == "N"
    assert d["derived_direction"] == d["parity_direction"] == "toward_ny"


@needs_gtfs
def test_verify_statuses(idx):
    assert al.verify(idx, _row("A017"))[0]["verify_status"] == "no_train"
    late = dict(_row("A005"), msg_text=_row("A005")["msg_text"].replace("9:08 AM", "9:30 AM"))
    d, why = al.verify(idx, late)
    assert d["verify_status"] == "time_mismatch" and "09:30" in why


@needs_gtfs
def test_verify_picks_trip_by_absolute_time_window(idx):
    """A032: 3201's 1:46 AM arrival, published 01:17 on 10-09 → the 10-08 service day (25:46),
    not 10-09's trip, which runs a day later."""
    d, _ = al.verify(idx, _row("A032"))
    assert d["verify_status"] == "ok" and d["gtfs_service_date"] == "2026-10-08"
    assert d["gtfs_time_at_named_stop"] == "25:46:00"


@needs_gtfs
def test_next_night_duplicate_does_not_match(idx):
    """A synthetic copy of 4393 on 10-10 (24:08 = 10-11 00:08) is 24 h past A003's pubdate: never chosen."""
    from datetime import date

    import pandas as pd

    real = idx.trips_on(date(2026, 10, 9))
    dup = real[real["train"] == "4393"]
    saved = idx.trips_on(date(2026, 10, 10))
    idx.trips_by_date[date(2026, 10, 10)] = pd.concat([saved, dup])
    try:
        d, _ = al.verify(idx, _row("A003"))
    finally:
        idx.trips_by_date[date(2026, 10, 10)] = saved
    assert d["verify_status"] == "ok" and d["gtfs_service_date"] == "2026-10-09"


@needs_gtfs
def test_on_demo_date_requires_same_route_and_time(idx):
    # M&E 6654 runs to PSNY on 10-09; on 10-13 (cutover) the number runs to Hoboken
    assert al.verify(idx, _row("A020"))[0]["on_demo_date"] == "N (number reused)"
    assert al.verify(idx, _row("A002"))[0]["on_demo_date"] == "Y"
    assert al.verify(idx, _row("A026"))[0]["on_demo_date"] == "N (unverified)"


def test_read_rows_accepts_bom_and_rejects_other_files(tmp_path):
    bom = tmp_path / "bom.csv"
    bom.write_bytes(bytes([0xEF, 0xBB, 0xBF]) + FIXTURE.read_bytes())
    cols, rows = al.read_rows(bom)
    assert cols[0] == "alert_id" and len(rows) == 34
    other = tmp_path / "other.csv"
    other.write_text("a,b" + chr(13) + chr(10) + "1,2" + chr(13) + chr(10), encoding="utf-8")
    with pytest.raises(ValueError):
        al.read_rows(other)
