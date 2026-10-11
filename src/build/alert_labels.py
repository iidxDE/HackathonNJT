"""Alert answer key: label migration + train verification (3.2a.0).

    python -m src.build.alert_labels      # migrate (once) + derive + eval/alert_label_check.md

data/reference/alert_samples.csv is edited in place:
- Label migration, applied only while the old schema is present (never re-run
  over human edits): lbl_lines normalized to ";"-separated canonical names
  (config.LINE_NAME_TO_ROUTES), lbl_trains ";"-separated, lbl_affects_penn renamed
  lbl_affects_penn_legacy, new human columns lbl_kind / lbl_direction /
  lbl_penn_disruption / lbl_cut_at_station (proposals for A000–A019) and
  lbl_reviewed = N.
- Derived columns (recomputed on every run, never written into lbl_*): the
  message's train matched against rail GTFS on its service day.

The CSV is read and written with the csv module in the file's own dialect (CRLF
records, minimal quoting), so every untouched value round-trips byte-identical,
including the embedded CRLF in A017's msg_text.

Verification: service day = pubdate_utc in America/New_York; trip_short_name ==
train (leading zeros stripped); every station + clock time the message gives for
that train must match the trip's stop_times within config.ALERT_TIME_TOL_S. No
match → previous service day with times ≥ 24:00. derived_direction: toward_ny
iff the trip's direction_id equals that of its route's trips terminating at NY
or HB (route's line group if the route has none); parity: even → toward_ny.
ok = found + time match + derived == parity.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from src import config, gtfs

log = logging.getLogger(__name__)

HUMAN_COLUMNS = ("lbl_kind", "lbl_direction", "lbl_penn_disruption", "lbl_cut_at_station", "lbl_reviewed")
DERIVED_COLUMNS = ("gtfs_service_date", "gtfs_trip_id", "gtfs_route_id", "gtfs_direction_id", "gtfs_terminal",
                   "gtfs_serves_ny", "gtfs_time_at_named_stop", "derived_direction", "parity_direction",
                   "verify_status", "on_demo_date")
KINDS = ("suspension", "cancellation", "delay", "advisory", "cross_honor")
DIRECTIONS = ("toward_ny", "away_from_ny", "both")

# Proposed human labels (3.2a.0 spec); lbl_reviewed stays N until a person checks them.
_CANCEL_AWAY = ("cancellation", "away_from_ny", "N")
PROPOSED: dict[str, tuple[str, str, str, str]] = {  # kind, direction, penn_disruption, cut_at_station
    "A000": (*_CANCEL_AWAY, "Point Pleasant"),
    "A004": (*_CANCEL_AWAY, "Glen Rock"),
    "A001": ("cancellation", "toward_ny", "N", ""),
    "A002": (*_CANCEL_AWAY, ""),
    "A003": (*_CANCEL_AWAY, ""),
    **{f"A{i:03d}": ("delay", "away_from_ny", "N", "") for i in range(5, 17)},
    "A017": ("suspension", "away_from_ny", "N", ""),
    "A018": ("delay", "toward_ny", "N", ""),
    "A019": ("delay", "toward_ny", "N", ""),
}


# --- CSV I/O in the file's own dialect ------------------------------------------

def read_rows(path: Path = config.ALERT_SAMPLES_CSV) -> tuple[list[str], list[dict[str, str]]]:
    """(columns, rows) with every value a str, exactly as stored."""
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), [dict(r) for r in reader]


def to_csv_text(columns: list[str], rows: list[dict[str, str]]) -> str:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def write_rows(columns: list[str], rows: list[dict[str, str]], path: Path = config.ALERT_SAMPLES_CSV) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(to_csv_text(columns, rows))


# --- Label migration ----------------------------------------------------------

def _line_patterns() -> list[tuple[str, tuple[str, ...]]]:
    pats = [(name, (name,)) for name in config.LINE_NAME_TO_ROUTES]
    pats += list(config.LINE_ALIASES.items())
    return sorted(pats, key=lambda kv: -len(kv[0]))  # longest first


def normalize_lines(text: str) -> str:
    """'Main Line Bergen County Line' → 'Main Line;Bergen County Line'. Unknown text raises."""
    found: list[tuple[int, str]] = []
    rest = text
    for pat, names in _line_patterns():
        for m in re.finditer(rf"(?<![\w&]){re.escape(pat)}(?![\w&])", rest, flags=re.IGNORECASE):
            found += [(m.start(), n) for n in names]
            rest = rest[:m.start()] + " " * (m.end() - m.start()) + rest[m.end():]
    leftover = re.sub(r"[\s;,*/]+|\band\b", "", rest, flags=re.IGNORECASE)
    if leftover:
        raise ValueError(f"unrecognized line name text {leftover!r} in {text!r}")
    out: list[str] = []
    for _, n in sorted(found, key=lambda x: x[0]):
        if n not in out:
            out.append(n)
    return ";".join(out)


def normalize_trains(text: str) -> str:
    return ";".join(t.lstrip("0") or "0" for t in re.findall(r"\d+", text))


def migrate(columns: list[str], rows: list[dict[str, str]]) -> tuple[list[str], list[dict[str, str]]]:
    """Old label schema → new; a no-op once lbl_affects_penn_legacy exists (never clobbers reviews)."""
    if "lbl_affects_penn_legacy" in columns:
        return columns, rows
    if "lbl_affects_penn" not in columns:
        raise ValueError("unknown alert_samples schema: neither lbl_affects_penn nor lbl_affects_penn_legacy")
    cols = ["lbl_affects_penn_legacy" if c == "lbl_affects_penn" else c for c in columns]
    cols += [c for c in HUMAN_COLUMNS if c not in cols]
    out = []
    for r in rows:
        n = {("lbl_affects_penn_legacy" if k == "lbl_affects_penn" else k): v for k, v in r.items()}
        n["lbl_lines"] = normalize_lines(n["lbl_lines"]) if n["lbl_lines"].strip() else ""
        n["lbl_trains"] = normalize_trains(n["lbl_trains"])
        kind, direction, penn, cut = PROPOSED.get(n["alert_id"], ("", "", "", ""))
        n.update(lbl_kind=kind, lbl_direction=direction, lbl_penn_disruption=penn,
                 lbl_cut_at_station=cut, lbl_reviewed="N")
        out.append(n)
    return cols, out


# --- Message parsing ----------------------------------------------------------

_TIME = r"(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?"
_STOP_END = r"(?=,|\.\s|\.$|\s+is\b|\s+was\b|\s+scheduled\b|\s+will\b|\s+has\b|$)"
_P_EVENT = re.compile(_TIME + r"\s+(arrival|departure)\s+(?:into|in|to|at|from)\s+(.+?)" + _STOP_END)
_P_SCHED = re.compile(r"scheduled to (arrive|depart)\s+(?:in|into|at|from)\s+(.+?)\s+at\s+" + _TIME)


@dataclass(frozen=True)
class Mention:
    station: str  # as written
    kind: str  # "arrival" | "departure"
    clock_s: int  # seconds after local midnight (0..86399)


def _clock(h: str, m: str, ap: str) -> int:
    hh = int(h) % 12 + (12 if ap.lower() == "p" else 0)
    return hh * 3600 + int(m) * 60


def primary_train(text: str) -> tuple[str | None, str]:
    """(first train number, the text about it): from '#N' to the next train number or 'Please take'."""
    m = re.search(r"#\s*0*(\d+)", text)
    if not m:
        return None, ""
    seg = text[m.end():]
    cut = re.search(r"#\s*\d+|please take", seg, flags=re.IGNORECASE)
    return m.group(1), seg[:cut.start()] if cut else seg


def mentions(segment: str) -> list[Mention]:
    out = []
    for m in _P_EVENT.finditer(segment):
        out.append(Mention(m.group(5).strip(), m.group(4).lower(), _clock(m.group(1), m.group(2), m.group(3))))
    for m in _P_SCHED.finditer(segment):
        kind = "arrival" if m.group(1).lower() == "arrive" else "departure"
        out.append(Mention(m.group(2).strip(), kind, _clock(m.group(3), m.group(4), m.group(5))))
    return out


# --- GTFS index -------------------------------------------------------------------

@dataclass
class RailIndex:
    feed: gtfs.GtfsFeed
    stop_code: dict[str, str]  # stop_id → stop_code
    name_to_code: dict[str, str]  # upper stop_name → stop_code (heavy-rail stations)
    route_toward_dir: dict[str, int | None]  # route_id → direction_id toward NY/HB (None = unknown)
    trips_by_date: dict[date, pd.DataFrame] = field(default_factory=dict)
    st_by_trip: dict[str, pd.DataFrame] = field(default_factory=dict)

    def trips_on(self, d: date) -> pd.DataFrame:
        if d not in self.trips_by_date:
            t = gtfs.active_trips(self.feed, d).copy()
            t["train"] = t["trip_short_name"].astype(str).str.lstrip("0")
            self.trips_by_date[d] = t
        return self.trips_by_date[d]

    def stop_times(self, trip_id: str) -> pd.DataFrame:
        if trip_id not in self.st_by_trip:
            st = self.feed.stop_times[self.feed.stop_times["trip_id"] == trip_id].sort_values("stop_sequence").copy()
            st["code"] = st["stop_id"].map(self.stop_code)
            self.st_by_trip[trip_id] = st
        return self.st_by_trip[trip_id]

    def resolve_station(self, name: str) -> str | None:
        key = re.sub(r"\s+", " ", name.upper().replace(".", "")).strip()
        if key in config.ALERT_STATION_ALIASES:
            return config.ALERT_STATION_ALIASES[key]
        names = {re.sub(r"\s+", " ", n.replace(".", "")).strip(): c for n, c in self.name_to_code.items()}
        if key in names:
            return names[key]
        starts = {c for n, c in names.items() if n.startswith(key + " ") or n == key}
        return starts.pop() if len(starts) == 1 else None


def load_rail_index(feed: gtfs.GtfsFeed | None = None) -> RailIndex:
    feed = feed or gtfs.load_feed(config.GTFS_NJT_RAIL_DIR)
    stops = feed.stops.astype({"stop_id": str, "stop_code": str})
    stop_code = dict(zip(stops["stop_id"], stops["stop_code"]))
    heavy = stops[~stops["stop_code"].str.fullmatch(r"\d{5}")]  # light-rail stops carry 5-digit codes
    name_to_code = dict(zip(heavy["stop_name"].str.upper(), heavy["stop_code"]))
    # Terminal of every trip → direction_id of trips ending at NY / HB, per route.
    st = feed.stop_times.sort_values(["trip_id", "stop_sequence"])
    last = st.groupby("trip_id", sort=False)["stop_id"].last().map(stop_code)
    trips = feed.trips.astype({"trip_id": str, "route_id": str})
    trips["terminal"] = trips["trip_id"].map(last)
    toward = trips[trips["terminal"].isin(config.TOWARD_NY_TERMINALS)]
    by_route = toward.groupby("route_id")["direction_id"].agg(lambda s: set(int(x) for x in s))
    route_dir: dict[str, int | None] = {}
    for route in trips["route_id"].unique():
        dirs = by_route.get(route)
        if not dirs:  # route never reaches NY / HB (e.g. a shuttle): use its line group
            group = {r for rs in config.LINE_NAME_TO_ROUTES.values() if route in rs for r in rs}
            dirs = set().union(*(by_route.get(r, set()) for r in group)) if group else set()
        route_dir[route] = next(iter(dirs)) if len(dirs) == 1 else None
    return RailIndex(feed, stop_code, name_to_code, route_dir)


# --- Verification ---------------------------------------------------------------

def _hms(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def service_day(pubdate_utc: str) -> date | None:
    if not pubdate_utc:
        return None
    ts = pd.Timestamp(pubdate_utc)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return ts.tz_convert(config.LOCAL_TZ).date()


def _time_matches(idx: RailIndex, trip_id: str, ms: list[tuple[Mention, str]], previous_day: bool
                  ) -> tuple[bool, str, str]:
    """(all mentions match, GTFS time at the first named stop, reason)."""
    st = idx.stop_times(trip_id)
    first_time, reasons = "", []
    for k, (m, code) in enumerate(ms):
        rows = st[st["code"] == code]
        if rows.empty:
            reasons.append(f"trip does not stop at {m.station} ({code})")
            continue
        col = "arrival_s" if m.kind == "arrival" else "departure_s"
        s = int(rows.iloc[0][col])
        if k == 0:
            first_time = _hms(s)
        targets = [m.clock_s + 86400] if previous_day else [m.clock_s, m.clock_s + 86400]
        if min(abs(s - t) for t in targets) > config.ALERT_TIME_TOL_S:
            reasons.append(f"{m.kind} {m.station} {_hms(m.clock_s)[:5]} vs GTFS {_hms(s)[:5]}")
    return not reasons, first_time, "; ".join(reasons)


def verify(idx: RailIndex, row: dict[str, str], demo_date: date = config.DEMO_SERVICE_DATE
           ) -> tuple[dict[str, str], str]:
    """Derived columns for one message, plus a reason for any non-ok status."""
    out = {c: "" for c in DERIVED_COLUMNS}
    train, segment = primary_train(row["msg_text"])
    if train is None:
        out["verify_status"] = "no_train"
        return out, "no train number in the message"
    out["parity_direction"] = "toward_ny" if int(train) % 2 == 0 else "away_from_ny"
    out["on_demo_date"] = "Y" if (idx.trips_on(demo_date)["train"] == train).any() else "N"
    ms = [(m, code) for m in mentions(segment) if (code := idx.resolve_station(m.station))]
    unresolved = [m.station for m in mentions(segment) if not idx.resolve_station(m.station)]
    day = service_day(row["pubdate_utc"])
    if day is None:
        out["verify_status"] = "not_found"
        return out, "no pubdate_utc"
    attempts = []  # (date, previous_day, matching trips, all trips)
    for d, prev in ((day, False), (day - timedelta(days=1), True)):
        trips = idx.trips_on(d)
        trips = trips[trips["train"] == train]
        ok = [(t, *_time_matches(idx, str(t.trip_id), ms, prev)) for t in trips.itertuples()] if ms else []
        attempts.append((d, prev, [x for x in ok if x[1]], trips, ok))
        if any(x[1] for x in ok):
            break
    d, prev, matched, trips, tried = attempts[-1]
    if not matched:
        found = [a for a in attempts if len(a[3])]
        if not found:
            out["verify_status"] = "not_found"
            return out, f"train {train} not in GTFS on {day} or {day - timedelta(days=1)}"
        d, prev, _, trips, tried = found[0]
        t = trips.iloc[0]
        _fill(idx, out, t, d, tried[0][2] if tried else "")
        out["verify_status"] = "time_mismatch"
        why = ("no station + clock time parsed for the train" if not ms else
               "; ".join(x[3] for x in tried if x[3]) or "no time match")
        if unresolved:
            why += f" (unresolved station names: {unresolved})"
        return out, why
    if len(matched) > 1:
        _fill(idx, out, matched[0][0], d, matched[0][2])
        out["verify_status"] = "ambiguous"
        return out, f"{len(matched)} trips numbered {train} match on {d}"
    t, _, first_time, _ = matched[0]
    _fill(idx, out, t, d, first_time)
    if not out["derived_direction"]:
        out["verify_status"] = "ambiguous"
        return out, f"route {out['gtfs_route_id']}: no single direction_id toward NY / HB"
    if out["derived_direction"] != out["parity_direction"]:
        out["verify_status"] = "parity_mismatch"
        return out, f"derived {out['derived_direction']} vs parity {out['parity_direction']}"
    out["verify_status"] = "ok"
    notes = ["previous service day, after midnight"] if prev else []
    if row.get("lbl_trains") and train not in row["lbl_trains"].split(";"):
        notes.append("lbl_trains does not list the message's train")
    return out, "; ".join(notes)


def _fill(idx: RailIndex, out: dict[str, str], t, d: date, first_time: str) -> None:
    st = idx.stop_times(str(t.trip_id))
    route = str(t.route_id)
    toward = idx.route_toward_dir.get(route)
    out.update(gtfs_service_date=d.isoformat(), gtfs_trip_id=str(t.trip_id), gtfs_route_id=route,
               gtfs_direction_id=str(int(t.direction_id)), gtfs_terminal=str(st["code"].iloc[-1]),
               gtfs_serves_ny="Y" if (st["code"] == "NY").any() else "N",
               gtfs_time_at_named_stop=first_time,
               derived_direction="" if toward is None else
               ("toward_ny" if int(t.direction_id) == toward else "away_from_ny"))


def derive(idx: RailIndex, columns: list[str], rows: list[dict[str, str]]
           ) -> tuple[list[str], list[dict[str, str]], dict[str, str]]:
    """Recompute the derived columns (appended after the label columns). Returns reasons by alert_id."""
    cols = [c for c in columns if c not in DERIVED_COLUMNS] + list(DERIVED_COLUMNS)
    reasons, out = {}, []
    for r in rows:
        d, why = verify(idx, r)
        out.append({**r, **d})
        reasons[r["alert_id"]] = why
    return cols, out, reasons


# --- Report -----------------------------------------------------------------------

def _md(df: pd.DataFrame) -> str:
    if not len(df):
        return "None."
    out = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    out += ["| " + " | ".join(str(v).replace("|", "/").replace("\r\n", " ").replace("\n", " ") for v in r) + " |"
            for r in df.itertuples(index=False)]
    return "\n".join(out)


def report(rows: list[dict[str, str]], reasons: dict[str, str]) -> str:
    from src.scoring.pipeline import build_stamp

    df = pd.DataFrame(rows)
    counts = df["verify_status"].value_counts().rename_axis("verify_status").reset_index(name="rows")
    non_ok = df[df["verify_status"] != "ok"].assign(reason=lambda x: x["alert_id"].map(reasons))
    non_ok = non_ok[["alert_id", "verify_status", "gtfs_service_date", "gtfs_trip_id", "reason", "msg_text"]]
    ok_notes = df[(df["verify_status"] == "ok") & df["alert_id"].map(reasons).astype(bool)]
    labeled = df[(df["lbl_direction"] != "") & (df["derived_direction"] != "")]
    dir_diff = labeled[labeled["lbl_direction"] != labeled["derived_direction"]][
        ["alert_id", "lbl_direction", "derived_direction", "parity_direction", "gtfs_terminal", "msg_text"]]
    trains = df[df["gtfs_trip_id"] != ""]
    penn = trains[["alert_id", "gtfs_terminal", "gtfs_serves_ny", "lbl_affects_penn_legacy", "lbl_penn_disruption"]]
    penn = penn.assign(agree=[("—" if not legacy else "yes" if legacy == serves else "**no**")
                              for serves, legacy in zip(penn["gtfs_serves_ny"], penn["lbl_affects_penn_legacy"])])
    demo = df[df["on_demo_date"] == "Y"][["alert_id", "lbl_kind", "gtfs_route_id", "gtfs_terminal",
                                          "derived_direction", "verify_status", "msg_text"]]
    n_rev = int((df["lbl_reviewed"] == "Y").sum())
    return "\n".join([
        "# Alert answer key: label check",
        "",
        f"Generated by `python -m src.build.alert_labels` at build `{build_stamp()}` from "
        f"`data/reference/alert_samples.csv` ({len(df)} messages, {n_rev} reviewed) and the rail GTFS. "
        "Derived columns come from GTFS; they never overwrite a `lbl_*` value. Service day = pubdate in New York "
        f"local time (retry: previous service day, times ≥ 24:00); clock times must match within "
        f"{config.ALERT_TIME_TOL_S // 60} min.",
        "",
        "## verify_status",
        "",
        _md(counts),
        "",
        "## Non-ok rows",
        "",
        _md(non_ok),
        "",
        "Notes on ok rows: " + ("; ".join(f"{a}: {reasons[a]}" for a in ok_notes["alert_id"]) or "none") + ".",
        "",
        "## lbl_direction vs derived_direction (human decides)",
        "",
        "Rows with both values set that disagree. derived = GTFS direction_id against the route's NY / HB-bound "
        "trips; parity = even → toward NY.",
        "",
        _md(dir_diff),
        "",
        "## gtfs_serves_ny vs lbl_affects_penn_legacy",
        "",
        "Why the old field was split: `lbl_affects_penn` mixed \"this train runs to / through Penn\" with "
        "\"Penn itself is impaired\". The first is now derived (`gtfs_serves_ny`); the second is the human label "
        "`lbl_penn_disruption`.",
        "",
        _md(penn),
        "",
        "## Replay candidates (train also runs on the demo date "
        f"{config.DEMO_SERVICE_DATE})",
        "",
        _md(demo),
        "",
    ])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = config.ALERT_SAMPLES_CSV
    columns, rows = read_rows(path)
    columns, rows = migrate(columns, rows)
    columns, rows, reasons = derive(load_rail_index(), columns, rows)
    write_rows(columns, rows, path)
    config.ALERT_LABEL_CHECK_MD.write_text(report(rows, reasons), encoding="utf-8")
    counts = pd.Series([r["verify_status"] for r in rows]).value_counts().to_dict()
    print(f"wrote {path} ({len(rows)} rows) and {config.ALERT_LABEL_CHECK_MD}: {counts}")


if __name__ == "__main__":
    main()
