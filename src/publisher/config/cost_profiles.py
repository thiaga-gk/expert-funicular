"""Cost profile selection: parse and validate the --cost-profile flags of a run (the policy itself is governor/cost_profiles.py).

``--cost-profile PROFILE`` applies to every warehouse; ``--cost-profile PROFILE=WH[,WH...]`` to the listed warehouses,
each a warehouse ID or ``workspace_id/warehouse_id``. Without a flag a warehouse gets its tag default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from publisher.common.errors import PublisherError
from publisher.contracts.reporter_contract import CostProfileName

COST_PROFILES: Tuple[str, ...] = tuple(x.value for x in CostProfileName)
_WAREHOUSE_REF = re.compile(r"^(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class CostProfileSelection:
    """The --cost-profile overrides of one run: a profile for every warehouse (or None) and per-warehouse profiles,
    as sorted (warehouse reference, profile) pairs. An empty selection leaves every warehouse at its tag default."""
    all_profile: Optional[str] = None
    by_warehouse: Tuple[Tuple[str, str], ...] = ()


def _profile(value: str, flag: str) -> str:
    name = value.strip().lower()
    if name not in COST_PROFILES:
        raise PublisherError(f"Unknown cost profile {value!r} in --cost-profile {flag}; supported: {', '.join(COST_PROFILES)}")
    return name


def parse_cost_profile_flags(flags: Sequence[str]) -> CostProfileSelection:
    """The selection the --cost-profile flags describe.

    Raises PublisherError for an unknown profile, an empty or malformed warehouse list, two all-warehouse flags with
    different profiles, or one warehouse listed with two different profiles. Repeating the same choice is accepted.
    """
    all_profile: Optional[str] = None
    by_warehouse: Dict[str, str] = {}
    for flag in flags:
        if "=" not in flag:
            name = _profile(flag, flag)
            if all_profile is not None and all_profile != name:
                raise PublisherError(f"--cost-profile is given for all warehouses twice ({all_profile}, {name}); keep one")
            all_profile = name
            continue
        value, _, refs = flag.partition("=")
        name = _profile(value, flag)
        listed = [r.strip() for r in refs.split(",")]
        if not refs.strip() or any(not r for r in listed):
            raise PublisherError(f"--cost-profile {flag}: list one or more warehouse IDs after '='")
        for ref in listed:
            if not _WAREHOUSE_REF.match(ref):
                raise PublisherError(f"--cost-profile {flag}: {ref!r} is not a warehouse ID or workspace_id/warehouse_id")
            if by_warehouse.get(ref, name) != name:
                raise PublisherError(f"--cost-profile: warehouse {ref} is listed with two profiles ({by_warehouse[ref]}, {name})")
            by_warehouse[ref] = name
    return CostProfileSelection(all_profile, tuple(sorted(by_warehouse.items())))
