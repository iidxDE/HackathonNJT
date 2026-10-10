import pandas as pd

from src.build import alerts_sample as al


def test_bucket_first_match_wins():
    assert al.bucket("Delays due to Portal Bridge, Penn Station congestion") == "portal_bridge"
    assert al.bucket("NEC train #3896 to New York Penn Station is late") == "penn_station"
    assert al.bucket("Elevator out of service") == "other"


def test_build_sample_dedupes_and_caps_per_bucket():
    texts = ["train #1 is late"] * 3 + [f"train #{i} cancelled" for i in range(10)]
    msgs = pd.DataFrame({"msg_text": texts, "msg_type": "banner", "msg_source": "", "msg_agency": "NJT",
                         "station_scope": "", "line_scope": "", "pubdate_utc": "",
                         "first_seen_file": "2026-10-09/000000Z.json.gz"})
    s = al.build_sample(msgs, n_per_bucket=4, seed=1)
    assert s["bucket"].value_counts().to_dict() == {"cancelled": 4, "delay_other": 1}
    assert list(s.columns) == list(al.COLUMNS)
    assert s["alert_id"].tolist() == ["A000", "A001", "A002", "A003", "A004"]
    assert (s["source"] == "archive").all() and (s["lbl_cause"] == "").all()
