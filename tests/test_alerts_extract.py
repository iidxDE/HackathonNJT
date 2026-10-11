"""3.2a.1–3.2a.3: schema validation, replay cache, evaluation metrics (no network)."""

import json

import pytest

from src import config
from src.build import alerts_eval as ev
from src.build import alerts_extract as ax

A = {"kind": "delay", "lines": ["NEC"], "trains": ["03713"], "stations": ["Jersey Ave"], "cut_at_station": None,
     "delay_min": 15, "cause": "operations", "direction": None, "penn_disruption": False}


def test_parse_alerts_validates_schema():
    (a,) = ax.parse_alerts({"alerts": [A]})
    assert a.trains == ("3713",) and a.delay_min == 15
    for bad in ({"kind": "storm"}, {"cause": "weather"}, {"direction": "north"}, {"delay_min": "15"},
                {"penn_disruption": None}):
        with pytest.raises(ValueError):
            ax.parse_alerts({"alerts": [{**A, **bad}]})
    with pytest.raises(ValueError):
        ax.parse_alerts({"alert": []})


def test_cache_key_and_replay(tmp_path):
    k = ax.cache_key("msg", "v1", "m")
    assert k.endswith("|v1|m") and k != ax.cache_key("msg", "v2", "m") != ax.cache_key("msg2", "v1", "m")
    path = tmp_path / "c.jsonl"
    ax.append_cache({"key": ax.cache_key("hello"), "msg_text": "hello", "parsed": {"alerts": [A]}}, path)
    cache = ax.load_cache(path)
    assert ax.replay("hello", cache)[0].kind == "delay"
    with pytest.raises(KeyError):
        ax.replay("not cached", cache)
    with pytest.raises(KeyError):
        ax.replay("hello", cache, prompt_version="v999")


def test_schema_and_prompt_agree_with_config():
    item = ax.RESPONSE_SCHEMA["properties"]["alerts"]["items"]
    assert set(item["required"]) == set(item["properties"]) == set(ax.Alert.__dataclass_fields__)
    p = ax.system_prompt("v1")
    assert all(name in p for name in config.LINE_NAME_TO_ROUTES) and "{line_names}" not in p
    json.dumps(ax.RESPONSE_SCHEMA)


@pytest.mark.skipif(not config.GEMINI_CACHE_JSONL.exists(), reason="no extraction cache")
def test_cache_rows_validate_and_carry_no_key():
    text = config.GEMINI_CACHE_JSONL.read_text(encoding="utf-8")
    for row in ax.load_cache().values():
        ax.parse_alerts(row["parsed"])
        assert {"key", "msg_text", "pubdate_utc", "alert_id", "raw_response", "parsed", "extracted_at"} <= set(row)
    assert "x-goog-api-key" not in text and "AIza" not in text


def test_message_prediction_merges_alerts():
    a1, a2 = ax.parse_alerts({"alerts": [A, {**A, "kind": "cancellation", "trains": ["3715"], "lines": ["ME Line"],
                                              "cause": "equipment", "penn_disruption": True}]})
    p = ev.message_prediction([a1, a2])
    assert p.kind == "delay" and p.cause == "operations" and p.penn_disruption
    assert p.trains == {"3713", "3715"} and p.lines == {"Northeast Corridor Line", "Morris & Essex Line"}


def test_metrics():
    assert ev.accuracy([("a", "a"), ("a", "b")]) == 0.5 and ev.accuracy([]) is None
    s = ev.set_scores([(frozenset({"1", "2"}), frozenset({"1"})), (frozenset({"3"}), frozenset({"3", "4"}))])
    assert s == {"exact": 0.0, "precision": 2 / 3, "recall": 2 / 3}
    c = ev.confusion([("delay", "delay"), ("delay", None)])
    assert c.set_index("label \\ predicted").loc["delay", ev.NONE] == 1


def _row(i, **kw):
    base = {"alert_id": i, "msg_text": f"text {i}", "lbl_kind": "delay", "lbl_cause": "operations",
            "lbl_lines": "Northeast Corridor Line", "lbl_trains": "3713", "lbl_delay_min": "15",
            "lbl_cut_at_station": "", "lbl_direction": "", "lbl_penn_disruption": "N", "verify_status": "ok"}
    return {**base, **kw}


def test_evaluate_rows_lists_every_disagreement():
    rows = [_row("A1"), _row("A2", lbl_delay_min="25", lbl_cut_at_station="Point Pleasant", lbl_kind="cancellation"),
            _row("A3", verify_status="no_train", lbl_direction="both", lbl_trains="")]
    good = ev.message_prediction(ax.parse_alerts({"alerts": [A]}))
    preds = {"A1": good,
             "A2": ev.Prediction("cancellation", "operations", good.lines, good.trains, 20, "Point Pleasant Beach",
                                 None, False),
             "A3": ev.Prediction("delay", "operations", good.lines, frozenset(), 15, None, None, True)}
    md, dis = ev.evaluate_rows(rows, preds)
    got = {(d["alert_id"], d["field"]) for d in dis}
    # A2 is a cancellation: its delay_min is not scored (delay rows only)
    assert got == {("A2", "cut_at_station"), ("A3", "penn_disruption"), ("A3", "direction")}
    text = "\n".join(md)
    assert "No labeled positives" in text  # penn: no Y label among the rows
    assert "Accuracy 100% (n = 3)" in text  # kind


def test_cause_alternatives_count_as_correct():
    rows = [_row("A1", lbl_cause="operations;infrastructure"), _row("A2", lbl_cause="equipment;operations")]
    good = ev.message_prediction(ax.parse_alerts({"alerts": [A]}))
    infra = ev.Prediction("delay", "infrastructure", good.lines, good.trains, 15, None, None, False)
    congestion = ev.Prediction("delay", "congestion", good.lines, good.trains, 15, None, None, False)
    md, dis = ev.evaluate_rows(rows, {"A1": infra, "A2": congestion})
    assert [(d["alert_id"], d["field"], d["label"]) for d in dis] == [("A2", "cause", "equipment;operations")]
    assert "Accuracy 50% (n = 2). Labels with alternatives" in chr(10).join(md)
