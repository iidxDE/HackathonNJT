"""Build data/reference/alert_samples.csv: a hand-labeling sample of station messages.

python -m src.build.alerts_sample [--n-per-bucket 6] [--seed 42]

Reads every archived getStationMSG snapshot (read only), dedupes on message
text (the same alert repeats every 5 minutes), tags a rough keyword bucket so
the sample covers different kinds of messages, samples up to N per bucket and
writes a CSV with empty lbl_* columns: the answer key for scoring Gemini's
extraction (Phase 4). bucket is only for spreading the sample, not a label.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
from pathlib import Path

import pandas as pd

from src import config

log = logging.getLogger(__name__)

BUCKETS = (  # first match wins; order matters
    ("portal_bridge", r"portal"),
    ("penn_station", r"penn station|psny|new york penn|\bny penn"),
    ("cross_honor", r"cross[- ]?hono"),
    ("cancelled", r"cancel"),
    ("signal_switch", r"signal|switch"),
    ("track_work", r"track (work|maintenance)|maintenance"),
    ("police_medical", r"police|medical|trespass|emergency"),
    ("equipment", r"equipment|mechanical|disabled train"),
    ("weather", r"weather|snow|storm|heat|flood|wind"),
    ("delay_other", r"late|delay"),
)
LABEL_COLUMNS = ("lbl_lines", "lbl_trains", "lbl_delay_min", "lbl_cause",
                 "lbl_affects_penn", "lbl_scope", "lbl_notes")
COLUMNS = ("alert_id", "source", "bucket", "msg_text", "msg_type", "msg_source", "msg_agency",
           "station_scope", "line_scope", "pubdate_utc", "first_seen_file", *LABEL_COLUMNS)


def bucket(text: str) -> str:
    t = text.lower()
    for name, pattern in BUCKETS:
        if re.search(pattern, t):
            return name
    return "other"


def load_messages(archive_dir: Path) -> pd.DataFrame:
    """One row per message per snapshot, oldest snapshot first."""
    rows = []
    for path in sorted(archive_dir.glob("*/*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            body = json.load(fh)
        for m in body or []:
            rows.append({
                "msg_text": (m.get("MSG_TEXT") or "").strip(),
                "msg_type": m.get("MSG_TYPE"),
                "msg_source": (m.get("MSG_SOURCE") or "").strip(),
                "msg_agency": (m.get("MSG_AGENCY") or "").strip(),
                "station_scope": (m.get("MSG_STATION_SCOPE") or "").strip(),
                "line_scope": (m.get("MSG_LINE_SCOPE") or "").strip(),
                "pubdate_utc": m.get("MSG_PUBDATE_UTC"),
                "first_seen_file": f"{path.parent.name}/{path.name}",
            })
    df = pd.DataFrame(rows)
    pub = pd.to_datetime(df["pubdate_utc"], format="%m/%d/%Y %I:%M:%S %p", errors="coerce")
    df["pubdate_utc"] = pub.dt.strftime("%Y-%m-%dT%H:%M:%SZ").fillna("")
    return df[df["msg_text"] != ""]


def build_sample(messages: pd.DataFrame, n_per_bucket: int, seed: int) -> pd.DataFrame:
    unique = messages.drop_duplicates("msg_text", keep="first").copy()
    unique["bucket"] = unique["msg_text"].map(bucket)
    log.info("%d message rows, %d unique texts; per bucket:\n%s",
             len(messages), len(unique), unique["bucket"].value_counts().to_string())
    sample = (unique.sample(frac=1, random_state=seed)
                    .groupby("bucket", sort=True).head(n_per_bucket)
                    .sort_values(["bucket", "pubdate_utc"])
                    .reset_index(drop=True))
    sample.insert(0, "alert_id", [f"A{i:03d}" for i in range(len(sample))])
    sample["source"] = "archive"
    for col in LABEL_COLUMNS:
        sample[col] = ""
    return sample[list(COLUMNS)]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, default=config.RAW_STATION_MSG_DIR)
    parser.add_argument("--out", type=Path, default=config.ALERT_SAMPLES_CSV)
    parser.add_argument("--n-per-bucket", type=int, default=6)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force", action="store_true", help="overwrite an existing (possibly labeled) file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if args.out.exists() and not args.force:
        raise SystemExit(f"{args.out} exists and may hold hand labels; pass --force to overwrite")
    sample = build_sample(load_messages(args.archive), args.n_per_bucket, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sample.to_csv(args.out, index=False, encoding="utf-8")
    log.info("wrote %d alerts to %s", len(sample), args.out)


if __name__ == "__main__":
    main()
