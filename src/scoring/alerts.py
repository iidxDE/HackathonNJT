"""Alerts → effects on the trip (3.2b-min). Request time: local files only, no live calls.

An AlertSet is what the scorer sees: one router Exclusion (passed to
route_all(base=...)), delay shifts (empty for now) and the notices the UI shows.
alert_to_effects maps one Alert:
- penn_disruption → config.AVOID_PENN_NRT (no ride starts or ends at NY Penn);
- every other kind → display only. Delay shifts and train bans are deferred.
A display-only notice still says whether it touches the trip, by a direction /
scope gate: it affects the trip only if its direction can be toward NY (or is
unknown) and it names a station one of the options boards or alights at (big
terminals that alerts cite only as reference points excluded).

Inputs supported now: the hand-built "Penn suspended" scenario (label
"scenario", no Gemini) and archived messages replayed from the Gemini cache
(label "replayed").
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src import config
from src.build.alerts_extract import Alert
from src.routing.exclusion import Exclusion


@dataclass(frozen=True)
class AlertNotice:
    """One line for the UI banner."""
    label: str  # "scenario" | "replayed"
    text: str  # the message (or scenario description)
    effect: str  # what the scorer does with it
    affects_trip: bool


@dataclass(frozen=True)
class AlertSet:
    """Hashable bundle of active alerts' effects (the simulation's alert key is `id`)."""
    id: str
    exclusion: Exclusion | None = None
    delay_shifts: tuple[tuple[str, float], ...] = ()  # (train number, minutes): deferred, always empty
    notices: tuple[AlertNotice, ...] = ()

    @property
    def name(self) -> str:
        return ALERT_CHOICES.get(self.id, self.id)


NO_ALERTS = AlertSet("none")

PENN_SCENARIO_TEXT = ("Scenario: NY Penn Station suspended. No NJ TRANSIT train may board or alight at "
                      "New York Penn Station (North River tunnels unusable); PATH unaffected.")
PENN_SCENARIO = Alert(kind="suspension", lines=(), trains=(), stations=("New York Penn Station",),
                      cut_at_station=None, delay_min=None, cause="infrastructure", direction="both",
                      penn_disruption=True)
REPLAY_ALERT_ID = "A017"  # Main/Bergen westbound bus substitution beyond Route 17 (cached Gemini output)
ALERT_CHOICES: dict[str, str] = {
    "none": "None",
    "penn_scenario": "Penn suspended (scenario)",
    REPLAY_ALERT_ID: f"{REPLAY_ALERT_ID} (replayed)",
}
# Stations alerts name only as reference points ("trains from Hoboken to ...").
SCOPE_IGNORE: frozenset[str] = frozenset(config.TOWARD_NY_TERMINALS)


def station_codes(names: Iterable[str], name_to_code: Mapping[str, str]) -> set[str]:
    """Alert station names → NJT stop codes (aliases first, then exact GTFS stop name)."""
    out = set()
    for n in names:
        key = re.sub(r"\s+", " ", n.upper().replace(".", "")).strip()
        code = config.ALERT_STATION_ALIASES.get(key) or name_to_code.get(key)
        if code:
            out.add(code)
    return out


def alert_to_effects(alert: Alert, label: str, text: str, trip_stations: set[str],
                     name_to_code: Mapping[str, str]) -> tuple[Exclusion | None, AlertNotice]:
    """One alert → (router exclusion or None, banner notice). Reasons name stations in words."""
    outbound_only = alert.direction == "away_from_ny"
    if alert.penn_disruption and not outbound_only:
        return config.AVOID_PENN_NRT, AlertNotice(
            label, text, "trains to / from NY Penn removed (Penn and North River tunnels unavailable)", True)
    if alert.penn_disruption:
        return None, AlertNotice(label, text, "Not affecting your trip: Penn alert for outbound trains only.", False)
    named = station_codes(alert.stations, name_to_code) - SCOPE_IGNORE
    names = _station_names(named, name_to_code)
    if outbound_only:
        return None, AlertNotice(label, text, "Not affecting your trip: away from NY only.", False)
    hit = named & trip_stations
    if hit:
        return None, AlertNotice(label, text, f"May affect your trip: {_station_names(hit, name_to_code)} "
                                 f"{'is' if len(hit) == 1 else 'are'} on your routes (display only: delays and "
                                 "cancellations are not applied yet).", True)
    why = (f"{names} {'is' if len(named) == 1 else 'are'} not on your routes." if named
           else "it names no station on your routes.")
    return None, AlertNotice(label, text, f"Not affecting your trip: {why}", False)


def _station_names(codes: Iterable[str], name_to_code: Mapping[str, str]) -> str:
    """Stop codes → readable GTFS stop names ('Port Jervis, Sloatsburg'), sorted."""
    by_code: dict[str, str] = {}
    for name, code in name_to_code.items():
        by_code.setdefault(code, name)
    return ", ".join(sorted(by_code.get(c, c).title() for c in codes))


def trip_stations(options: Mapping[str, list]) -> set[str]:
    """NJT stations any option boards or alights at (rail legs), the access stations included."""
    out = set()
    for opts in options.values():
        for o in opts:
            for leg in o:
                if leg.mode.value == "rail":
                    out |= {leg.from_stop, leg.to_stop}
    return out


def build_alert_set(alert_id: str, options: Mapping[str, list], name_to_code: Mapping[str, str]) -> AlertSet:
    """The AlertSet for a UI choice. options = the no-alert router options (for the scope gate)."""
    if alert_id == "none":
        return NO_ALERTS
    stations = trip_stations(options)
    if alert_id == "penn_scenario":
        ex, notice = alert_to_effects(PENN_SCENARIO, "scenario", PENN_SCENARIO_TEXT, stations, name_to_code)
        return AlertSet(alert_id, ex, (), (notice,))
    from src.build.alert_labels import read_rows
    from src.build.alerts_extract import load_cache, replay

    row = next((r for r in read_rows()[1] if r["alert_id"] == alert_id), None)
    if row is None:
        raise KeyError(f"alert {alert_id} not in alert_samples.csv")
    exclusion, notices = None, []
    for a in replay(row["msg_text"], load_cache()):  # cache only; missing key raises
        ex, notice = alert_to_effects(a, "replayed", row["msg_text"].replace("\r\n", " "), stations, name_to_code)
        exclusion = ex if exclusion is None else exclusion.union(ex)
        notices.append(notice)
    return AlertSet(alert_id, exclusion, (), tuple(notices))
