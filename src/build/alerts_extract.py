"""Gemini alert extraction with a replay cache (3.2a.1–3.2a.2).

    python -m src.build.alerts_extract            # replay: every sample must be cached (else error)
    python -m src.build.alerts_extract --live     # call Gemini for messages missing from the cache

One message → {"alerts": [alert, ...]} under a JSON schema (structured output,
temperature 0). Cache: data/alerts/gemini_cache.jsonl, one row per call, keyed
by sha256(msg_text) | prompt_version | model_id. Replay reads the cache only; a
missing key raises. Live calls need config.ALERTS_LIVE = True or --live, and
the key in the environment / .env (config.GEMINI_API_KEY_ENV). The key is sent
as a request header, never logged or cached.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src import config

log = logging.getLogger(__name__)

KINDS = ("suspension", "cancellation", "delay", "advisory", "cross_honor")
CAUSES = ("equipment", "operations", "congestion", "infrastructure")
DIRECTIONS = ("toward_ny", "away_from_ny", "both")

# Gemini responseSchema (OpenAPI subset). Mirrors the 3.2a.1 alert schema.
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "alerts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {"type": "STRING", "enum": list(KINDS)},
                    "lines": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "trains": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "stations": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "cut_at_station": {"type": "STRING", "nullable": True},
                    "delay_min": {"type": "INTEGER", "nullable": True},
                    "cause": {"type": "STRING", "enum": list(CAUSES), "nullable": True},
                    "direction": {"type": "STRING", "enum": list(DIRECTIONS), "nullable": True},
                    "penn_disruption": {"type": "BOOLEAN"},
                },
                "required": ["kind", "lines", "trains", "stations", "cut_at_station", "delay_min", "cause",
                             "direction", "penn_disruption"],
                "propertyOrdering": ["kind", "lines", "trains", "stations", "cut_at_station", "delay_min",
                                     "cause", "direction", "penn_disruption"],
            },
        },
    },
    "required": ["alerts"],
}

PROMPTS: dict[str, str] = {
    "v1": """You extract structured service alerts from NJ TRANSIT rail customer messages.
Return {"alerts": [...]}: one alert per distinct disruption in the message (usually exactly one).
Do not create an alert for a train that is only suggested as an alternative ("Please take train #...").

Fields:
- kind: "cancellation" (a train is cancelled, fully or from/at a station), "delay" (a train or service is
  running late), "suspension" (service on a line or segment is suspended or replaced, e.g. by buses),
  "cross_honor" (tickets are cross-honored on another carrier), "advisory" (anything else: schedule
  changes, information, construction notices).
- lines: NJ TRANSIT rail line names the alert applies to, using exactly these names:
  {line_names}. Expand abbreviations (NEC = Northeast Corridor Line, NJCL = North Jersey Coast Line,
  M and E / ME = Morris & Essex Line, MBPJ = Main Line, Bergen County Line and Port Jervis Line only if the
  message says which; for a single MBPJ train give the line the text implies, else all three).
  Empty list if none is named.
- trains: affected train numbers as digits without "#" or leading zeros. Not alternative trains.
- stations: stations named in the message, as written.
- cut_at_station: for a partial cancellation ("cancelled at X", "will terminate at X", "will originate at X"),
  the station X as written; else null.
- delay_min: for kind "delay" only, the stated delay in minutes ("up to 1 hour and 10 minutes" = 70);
  else null.
- cause: "equipment" (mechanical issues, equipment availability, disabled train of this service),
  "operations" (operational issues, crew, late connections, speed restrictions, police or medical
  activity), "congestion" (congestion or traffic from other trains), "infrastructure" (signal, switch,
  overhead wire, track, bridge, tunnel or maintenance work); null if no cause is given.
- direction: only if the text states it explicitly ("westbound", "eastbound", "to New York",
  "from Hoboken", "in both directions"): "toward_ny" (toward New York / Hoboken), "away_from_ny",
  or "both". null otherwise; do not infer it from train numbers or terminals.
- penn_disruption: true only if New York Penn Station itself or the North River tunnels into it are
  impaired (closure, track or signal problems at Penn or in the tunnels, Amtrak Penn Station
  issues). A train that is merely late arriving at or departing from Penn Station is false.
""",
}


def system_prompt(version: str = config.ALERT_PROMPT_VERSION) -> str:
    return PROMPTS[version].replace("{line_names}", "; ".join(config.LINE_NAME_TO_ROUTES))


def cache_key(msg_text: str, prompt_version: str = config.ALERT_PROMPT_VERSION,
              model_id: str = config.GEMINI_MODEL_ID) -> str:
    return f"{hashlib.sha256(msg_text.encode('utf-8')).hexdigest()}|{prompt_version}|{model_id}"


# --- Parsed output ------------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    kind: str
    lines: tuple[str, ...]
    trains: tuple[str, ...]
    stations: tuple[str, ...]
    cut_at_station: str | None
    delay_min: int | None
    cause: str | None
    direction: str | None
    penn_disruption: bool


def parse_alerts(obj: dict) -> list[Alert]:
    """Validate a response object against the schema; raises ValueError on any violation."""
    if not isinstance(obj, dict) or not isinstance(obj.get("alerts"), list):
        raise ValueError(f"response is not {{'alerts': [...]}}: {obj!r}")
    out = []
    for a in obj["alerts"]:
        if a.get("kind") not in KINDS:
            raise ValueError(f"bad kind {a.get('kind')!r}")
        if a.get("cause") not in (*CAUSES, None) or a.get("direction") not in (*DIRECTIONS, None):
            raise ValueError(f"bad cause / direction in {a!r}")
        dm = a.get("delay_min")
        if dm is not None and not isinstance(dm, int):
            raise ValueError(f"bad delay_min {dm!r}")
        if not isinstance(a.get("penn_disruption"), bool):
            raise ValueError(f"bad penn_disruption in {a!r}")
        out.append(Alert(a["kind"], tuple(str(x) for x in a.get("lines") or ()),
                         tuple(str(x).lstrip("#").lstrip("0") or "0" for x in a.get("trains") or ()),
                         tuple(str(x) for x in a.get("stations") or ()), a.get("cut_at_station"), dm,
                         a.get("cause"), a.get("direction"), a["penn_disruption"]))
    return out


# --- Cache ------------------------------------------------------------------------

def load_cache(path: Path = config.GEMINI_CACHE_JSONL) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                out[row["key"]] = row
    return out


def append_cache(row: dict, path: Path = config.GEMINI_CACHE_JSONL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def replay(msg_text: str, cache: dict[str, dict], prompt_version: str = config.ALERT_PROMPT_VERSION,
           model_id: str = config.GEMINI_MODEL_ID) -> list[Alert]:
    """Cached alerts for a message; KeyError if it was never extracted with this prompt + model."""
    key = cache_key(msg_text, prompt_version, model_id)
    if key not in cache:
        raise KeyError(f"alert not in cache ({key}); run python -m src.build.alerts_extract --live")
    return parse_alerts(cache[key]["parsed"])


# --- Live call --------------------------------------------------------------------

def _api_key() -> str:
    try:
        from dotenv import load_dotenv

        for env in (config.REPO_ROOT / ".env", config.DATA_DIR.parent / ".env"):
            load_dotenv(env)
    except ImportError:
        pass
    key = os.environ.get(config.GEMINI_API_KEY_ENV)
    if not key:
        raise SystemExit(f"{config.GEMINI_API_KEY_ENV} is not set (env or .env)")
    return key


def call_gemini(msg_text: str, api_key: str, prompt_version: str = config.ALERT_PROMPT_VERSION,
                model_id: str = config.GEMINI_MODEL_ID) -> dict:
    """One generateContent call → the raw JSON response."""
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt(prompt_version)}]},
        "contents": [{"role": "user", "parts": [{"text": msg_text}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "responseSchema": RESPONSE_SCHEMA},
    }
    req = urllib.request.Request(config.GEMINI_API_URL.format(model=model_id),
                                 data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    for attempt in range(config.GEMINI_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=config.GEMINI_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (429, 503) and attempt < config.GEMINI_MAX_RETRIES:
                m = re.search(r"retry in ([\d.]+)s", detail)
                wait = float(m.group(1)) + 1 if m else config.GEMINI_MIN_INTERVAL_S * (attempt + 1)
                log.info("Gemini HTTP %d, retrying in %.0f s", e.code, wait)
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {detail[:500]}") from None
    raise AssertionError("unreachable")


def response_text(raw: dict) -> str:
    return raw["candidates"][0]["content"]["parts"][0]["text"]


def extract_live(row: dict[str, str], api_key: str, prompt_version: str = config.ALERT_PROMPT_VERSION,
                 model_id: str = config.GEMINI_MODEL_ID) -> dict:
    """Call Gemini for one sample row; returns the cache row (not yet written)."""
    raw = call_gemini(row["msg_text"], api_key, prompt_version, model_id)
    parsed = json.loads(response_text(raw))
    parse_alerts(parsed)  # schema check before caching
    return {"key": cache_key(row["msg_text"], prompt_version, model_id), "msg_text": row["msg_text"],
            "pubdate_utc": row.get("pubdate_utc", ""), "alert_id": row.get("alert_id", ""),
            "prompt_version": prompt_version, "model_id": model_id, "raw_response": raw, "parsed": parsed,
            "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def main(argv: list[str] | None = None) -> None:
    from src.build.alert_labels import read_rows

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="call Gemini for uncached messages")
    parser.add_argument("--prompt-version", default=config.ALERT_PROMPT_VERSION)
    parser.add_argument("--ids", default="", help="comma-separated alert_ids (default: every sample)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _, rows = read_rows()
    if args.ids:
        wanted = set(args.ids.split(","))
        rows = [r for r in rows if r["alert_id"] in wanted]
    cache = load_cache()
    todo = [r for r in rows if cache_key(r["msg_text"], args.prompt_version) not in cache]
    # Free-tier quotas are small: reviewed rows first, then demo-date replay candidates.
    todo.sort(key=lambda r: (r.get("lbl_reviewed") != "Y", r.get("on_demo_date") != "Y"))
    seen: set[str] = set()
    if todo and (args.live or config.ALERTS_LIVE):
        key = _api_key()
        for r in todo:
            k = cache_key(r["msg_text"], args.prompt_version)
            if k in seen:  # identical text twice in the sample: one call
                continue
            if seen:
                time.sleep(config.GEMINI_MIN_INTERVAL_S)
            row = extract_live(r, key, args.prompt_version)
            append_cache(row)
            seen.add(k)
            log.info("%s: %s", r["alert_id"], json.dumps(row["parsed"], ensure_ascii=False))
        cache = load_cache()
    missing = [r["alert_id"] for r in rows if cache_key(r["msg_text"], args.prompt_version) not in cache]
    if missing:
        raise SystemExit(f"not cached for prompt {args.prompt_version} / {config.GEMINI_MODEL_ID}: "
                         f"{', '.join(missing)} (run with --live)")
    for r in rows:
        replay(r["msg_text"], cache, args.prompt_version)  # every cached response still validates
    print(f"{len(rows)} messages covered by {config.GEMINI_CACHE_JSONL.name} "
          f"(prompt {args.prompt_version}, {config.GEMINI_MODEL_ID}); {len(seen)} new calls")


if __name__ == "__main__":
    main()
