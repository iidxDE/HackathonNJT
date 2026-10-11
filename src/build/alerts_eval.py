"""Gemini extraction vs the human answer key → eval/alert_extraction.md (3.2a.3).

    python -m src.build.alerts_eval [--versions v1,v2]

Only rows with lbl_reviewed = Y are scored; replay reads the cache only (a
reviewed row missing from the cache is an error). One message can yield several
alerts; the message-level prediction is: kind, cause, delay_min, cut_at_station,
direction from the first alert that sets them; lines and trains = union over
alerts; penn_disruption = any. Several prompt versions → one block each
(before / after a prompt change).
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass

import pandas as pd

from src import config
from src.build.alert_labels import normalize_lines, read_rows
from src.build.alerts_extract import Alert, cache_key, load_cache, replay

NONE = "∅"  # null category in confusion tables


@dataclass(frozen=True)
class Prediction:
    kind: str | None
    cause: str | None
    lines: frozenset[str]
    trains: frozenset[str]
    delay_min: int | None
    cut_at_station: str | None
    direction: str | None
    penn_disruption: bool


def _canon_lines(names) -> frozenset[str]:
    out = set()
    for n in names:
        try:
            out |= {x for x in normalize_lines(n).split(";") if x}
        except ValueError:
            out.add(n)  # unknown name: kept as written, so it counts as a miss
    return frozenset(out)


def message_prediction(alerts: list[Alert]) -> Prediction:
    def first(attr):
        return next((getattr(a, attr) for a in alerts if getattr(a, attr) is not None), None)

    return Prediction(alerts[0].kind if alerts else None, first("cause"),
                      _canon_lines(x for a in alerts for x in a.lines),
                      frozenset(t for a in alerts for t in a.trains), first("delay_min"),
                      first("cut_at_station"), first("direction"), any(a.penn_disruption for a in alerts))


# --- Metrics (pure) ---------------------------------------------------------------

def accuracy(pairs: list[tuple]) -> float | None:
    return None if not pairs else sum(a == b for a, b in pairs) / len(pairs)


def confusion(pairs: list[tuple]) -> pd.DataFrame:
    """Rows = label, columns = predicted (null shown as ∅)."""
    def show(v):
        return NONE if v in (None, "") else str(v)
    labels = sorted({show(a) for a, _ in pairs} | {show(b) for _, b in pairs})
    c = Counter((show(a), show(b)) for a, b in pairs)
    return pd.DataFrame([{"label \\ predicted": la, **{p: c[(la, p)] for p in labels}} for la in labels])


def set_scores(pairs: list[tuple[frozenset, frozenset]]) -> dict[str, float | None]:
    """Exact-set match rate and micro precision / recall."""
    tp = sum(len(a & b) for a, b in pairs)
    n_pred = sum(len(b) for _, b in pairs)
    n_true = sum(len(a) for a, _ in pairs)
    return {"exact": accuracy([(a, b) for a, b in pairs]),
            "precision": tp / n_pred if n_pred else None, "recall": tp / n_true if n_true else None}


def norm_station(s: str | None) -> str:
    return " ".join((s or "").replace(".", "").upper().split())


# --- Labels ----------------------------------------------------------------------

def _opt(v: str) -> str | None:
    return v.strip() or None


def label_of(r: dict[str, str]) -> dict:
    return {"kind": _opt(r["lbl_kind"]),
            # lbl_cause may list acceptable alternatives, ";"-separated (e.g. "operations;infrastructure")
            "cause": tuple(x for x in r["lbl_cause"].split(";") if x.strip()) or None,
            "lines": frozenset(x for x in r["lbl_lines"].split(";") if x),
            "trains": frozenset(x for x in r["lbl_trains"].split(";") if x),
            "delay_min": int(r["lbl_delay_min"]) if r["lbl_delay_min"].strip() else None,
            "cut_at_station": _opt(r["lbl_cut_at_station"]), "direction": _opt(r["lbl_direction"]),
            "penn_disruption": {"Y": True, "N": False}.get(r["lbl_penn_disruption"].strip()),
            "train_row": r.get("verify_status", "") != "no_train"}


def evaluate_rows(rows: list[dict[str, str]], preds: dict[str, Prediction]) -> tuple[list[str], list[dict]]:
    """(markdown lines, disagreements) for reviewed rows with a prediction each."""
    L = {r["alert_id"]: label_of(r) for r in rows}
    ids = [r["alert_id"] for r in rows]
    msg = {r["alert_id"]: r["msg_text"] for r in rows}
    out: list[str] = []
    dis: list[dict] = []

    def note(i, fld, lab, pred):
        dis.append({"alert_id": i, "field": fld, "label": lab, "predicted": pred, "msg_text": msg[i]})

    def pct(x):
        return "—" if x is None else f"{x:.0%}"

    for i in ids:  # cause alternatives: credit the one predicted, else score against the first
        alts, pred = L[i]["cause"], preds[i].cause
        L[i]["cause_alts"] = alts
        L[i]["cause"] = None if alts is None else (pred if pred in alts else alts[0])
    for fld in ("kind", "cause"):
        pairs = [(L[i][fld], getattr(preds[i], fld)) for i in ids if fld != "kind" or L[i]["kind"]]
        extra = (" Labels with alternatives count as correct when any alternative is predicted."
                 if fld == "cause" and any(len(L[i]["cause_alts"] or ()) > 1 for i in ids) else "")
        out += [f"### {fld}", "", f"Accuracy {pct(accuracy(pairs))} (n = {len(pairs)}).{extra}", "",
                _md(confusion(pairs)) if pairs else "No rows.", ""]
        for i in ids:
            if (fld != "kind" or L[i]["kind"]) and L[i][fld] != getattr(preds[i], fld):
                lab = ";".join(L[i]["cause_alts"]) if fld == "cause" and L[i]["cause_alts"] else L[i][fld]
                note(i, fld, lab, getattr(preds[i], fld))
    for fld in ("lines", "trains"):
        pairs = [(L[i][fld], getattr(preds[i], fld)) for i in ids]
        s = set_scores(pairs)
        out += [f"### {fld}", "", f"Exact set match {pct(s['exact'])}, precision {pct(s['precision'])}, "
                f"recall {pct(s['recall'])} (n = {len(pairs)}; names normalized to canonical lines, "
                "train numbers without leading zeros).", ""]
        for i in ids:
            if L[i][fld] != getattr(preds[i], fld):
                note(i, fld, ";".join(sorted(L[i][fld])), ";".join(sorted(getattr(preds[i], fld))))
    delay_ids = [i for i in ids if L[i]["kind"] == "delay"]
    exact = [(L[i]["delay_min"], preds[i].delay_min) for i in delay_ids]
    within = [L[i]["delay_min"] is not None and preds[i].delay_min is not None
              and abs(L[i]["delay_min"] - preds[i].delay_min) <= 5 for i in delay_ids]
    out += ["### delay_min (delay rows)", "", f"Exact {pct(accuracy(exact))}, within ±5 min "
            f"{pct(sum(within) / len(within) if within else None)} (n = {len(delay_ids)}).", ""]
    for i in delay_ids:
        if L[i]["delay_min"] != preds[i].delay_min:
            note(i, "delay_min", L[i]["delay_min"], preds[i].delay_min)
    cut_ids = [i for i in ids if L[i]["cut_at_station"]]
    cut = [(norm_station(L[i]["cut_at_station"]), norm_station(preds[i].cut_at_station)) for i in cut_ids]
    out += ["### cut_at_station (labeled rows)", "", f"Exact {pct(accuracy(cut))} (n = {len(cut)}; case and "
            "periods ignored).", ""]
    for i, (a, b) in zip(cut_ids, cut):
        if a != b:
            note(i, "cut_at_station", L[i]["cut_at_station"], preds[i].cut_at_station)
    penn_ids = [i for i in ids if L[i]["penn_disruption"] is not None]
    penn = [(L[i]["penn_disruption"], preds[i].penn_disruption) for i in penn_ids]
    n_pos = sum(a for a, _ in penn)
    out += ["### penn_disruption", "", f"Accuracy {pct(accuracy(penn))} (n = {len(penn)}, labeled positives "
            f"{n_pos}, predicted positives {sum(b for _, b in penn)})."
            + (" **No labeled positives: this measures only that Penn is not flagged falsely; recall on a real "
               "Penn disruption is untested.**" if not n_pos else ""), ""]
    for i, (a, b) in zip(penn_ids, penn):
        if a != b:
            note(i, "penn_disruption", a, b)
    dir_ids = [i for i in ids if not L[i]["train_row"]]
    dirs = [(L[i]["direction"], preds[i].direction) for i in dir_ids]
    out += ["### direction (non-train rows)", "", f"Accuracy {pct(accuracy(dirs))} (n = {len(dirs)}). Train rows "
            "are not scored: their direction is derived from GTFS (`derived_direction`), not extracted.", ""]
    for i, (a, b) in zip(dir_ids, dirs):
        if a != b:
            note(i, "direction", a, b)
    return out, dis


def _md(df: pd.DataFrame) -> str:
    if df is None or not len(df):
        return "None."
    out = ["| " + " | ".join(str(c) for c in df.columns) + " |", "|" + "---|" * len(df.columns)]
    out += ["| " + " | ".join(str(v).replace("|", "/").replace("\r\n", " ").replace("\n", " ") for v in r) + " |"
            for r in df.itertuples(index=False)]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    from src.scoring.pipeline import build_stamp

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--versions", default=config.ALERT_PROMPT_VERSION,
                        help="comma-separated prompt versions, oldest first (before / after)")
    args = parser.parse_args(argv)
    versions = args.versions.split(",")
    _, rows = read_rows()
    reviewed = [r for r in rows if r["lbl_reviewed"].strip() == "Y"]
    demo = [r for r in rows if r.get("on_demo_date") == "Y"]
    cache = load_cache()
    lines = [
        "# Alert extraction: Gemini vs the answer key",
        "",
        f"Generated by `python -m src.build.alerts_eval` at build `{build_stamp()}`. Model "
        f"`{config.GEMINI_MODEL_ID}`, structured output (JSON schema), temperature 0, replayed from "
        f"`data/alerts/gemini_cache.jsonl` (no live calls).",
        "",
        f"**n = {len(reviewed)} reviewed messages** (lbl_reviewed = Y) of {len(rows)} in "
        "`data/reference/alert_samples.csv`. Unreviewed rows are never scored.",
        "",
    ]
    cov = []
    for v in versions:
        have = lambda rs: sum(cache_key(r["msg_text"], v) in cache for r in rs)  # noqa: E731
        cov.append({"prompt": v, "all samples": f"{have(rows)} / {len(rows)}",
                    "reviewed": f"{have(reviewed)} / {len(reviewed)}",
                    "demo-date messages": f"{have(demo)} / {len(demo)}"})
    lines += ["## Cache coverage", "", _md(pd.DataFrame(cov)), ""]
    if not reviewed:
        lines += ["## Metrics", "", "**Not computed: no row has lbl_reviewed = Y yet.** Review the proposed labels "
                  "in `alert_samples.csv` (A000–A019 prefilled, A020–A033 empty), set lbl_reviewed = Y, and rerun. "
                  "No accuracy is claimed until then.", ""]
    for v in versions:
        if not reviewed:
            break
        preds = {r["alert_id"]: message_prediction(replay(r["msg_text"], cache, v)) for r in reviewed}
        body, dis = evaluate_rows(reviewed, preds)
        lines += [f"## Prompt {v}", "", *body, f"### Disagreements ({len(dis)})", "", _md(pd.DataFrame(dis)), ""]
    if len(versions) > 1:
        lines += [f"Prompt history: {' → '.join(versions)} (tuned at most once; both blocks above).", ""]
    config.ALERT_EXTRACTION_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {config.ALERT_EXTRACTION_MD} (n = {len(reviewed)})")


if __name__ == "__main__":
    main()
