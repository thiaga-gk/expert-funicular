"""Stateful pricing chain: the evolving cost and resource tracks one scenario's steps apply in order."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class _ChainState:
    """Evolving cost/resource state for one pricing chain; `halt` = (status, method) once it cannot be priced further."""
    dbu_tracks: List[float]
    aws_tracks: Optional[List[float]]
    resource_tracks: List[float]
    # Floor clusters per track: a minimum-cluster cut realizes a different share of its reduction on each track (none on
    # the conservative one), so later steps scale with that track's own floor.
    floor_tracks: List[float]
    halt: Optional[Tuple[str, str]] = None
    # Net running wall-clock minutes an earlier auto-stop change removed per floor cluster, per track (a lower auto-stop:
    # idle removed minus restarts added; a higher one: restarts saved minus idle added). A later minimum-cluster cut
    # re-scales that effect to the lower floor, so the plan total does not depend on step order.
    auto_stop_wall_minutes: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Cluster-minutes an earlier minimum-cluster cut removed, per track: a later auto-stop's saving at the lower floor
    # gives back at most this much (the overlap of the two is bounded by what the floor cut removed), so the plan total
    # does not depend on step order even when the floor cut's claim is clamped at zero.
    floor_cut_minutes: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
