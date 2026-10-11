"""Exclusion: what a RAPTOR run may not use (Phase 3.1; reused by backup routing).

Network-agnostic: every id is a router id (namespaced stop / route ids as in
src/routing/network.py, global trip indices). route_profile applies it as set
lookups in the scan; the cached network arrays are never changed.

No imports from src: config builds its asset exclusions from this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Exclusion:
    """Hashable ban list for one router run.

    no_board / no_alight: stop ids where no ride may start / end (walks still pass).
    banned_trips: global trip indices (Network order) that may not be boarded.
    banned_routes: namespaced route ids whose patterns are not scanned.
    banned_footpaths: (from_stop, to_stop) footpaths that may not be walked.
    """

    no_board: frozenset[str] = frozenset()
    no_alight: frozenset[str] = frozenset()
    banned_trips: frozenset[int] = frozenset()
    banned_routes: frozenset[str] = frozenset()
    banned_footpaths: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        for name in ("no_board", "no_alight", "banned_routes"):
            object.__setattr__(self, name, frozenset(str(v) for v in getattr(self, name)))
        object.__setattr__(self, "banned_trips", frozenset(int(v) for v in self.banned_trips))
        object.__setattr__(self, "banned_footpaths",
                           frozenset((str(a), str(b)) for a, b in self.banned_footpaths))

    def __bool__(self) -> bool:
        return bool(self.no_board or self.no_alight or self.banned_trips or self.banned_routes
                    or self.banned_footpaths)
