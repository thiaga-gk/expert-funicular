"""Cost profiles: the risk-appetite policy each warehouse is evaluated under, and how a warehouse gets its profile.

A profile changes only how much performance risk a supported cut may take (the fields in COST_PROFILE_GROUPS). It
never changes evidence floors, product constants, estimator or forecast parameters, confidence, classification rules,
the failure-cancel guard or the rules that add capacity, so a candidate's economics are the same under every profile.
balanced is the Thresholds defaults. Values and rationale: docs/reference/publisher/thresholds.md (Cost profiles).
"""
from __future__ import annotations

import dataclasses
import functools
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.config.cost_profiles import CostProfileSelection
from publisher.contracts.analysis import CostProfileChoice
from publisher.contracts.reporter_contract import COST_PROFILE_GROUPS, CostProfileName, CostProfileSource
from publisher.contracts.source import WarehouseEvidence
from publisher.governor.thresholds import Thresholds

AGGRESSIVE, BALANCED, CONSERVATIVE = (x.value for x in (CostProfileName.AGGRESSIVE, CostProfileName.BALANCED, CostProfileName.CONSERVATIVE))
PROFILED_FIELDS: Tuple[str, ...] = tuple(f for fields in COST_PROFILE_GROUPS.values() for f in fields)

# The values that differ from balanced (the Thresholds defaults); every profiled field is set in each.
_OVERLAYS: Dict[str, Dict[str, Any]] = {
    CONSERVATIVE: {
        "spill_query_pct_downsize_safe": 0.05, "p95_execution_ms_downsize_safe": 3_000, "p99_execution_ms_downsize_safe": 20_000,
        "time_at_max_pct_safe_reduce": 0.5, "max_cluster_cut_headroom_pct": 35.0,
        "time_at_min_pct_reduce": 40.0, "queue_wait_p95_ms_safe": 2_000, "min_cluster_busy_start_percentile": 99.0,
        "cold_start_added_share_max_pct": 1.0,
        "cost_change_min_annual_savings": 1000.0, "queue_query_pct_safe": 0.25, "canary_p95_regression_pct": 15.0,
        "growth_hold_weekly_ratio": 1.05,
    },
    AGGRESSIVE: {
        "spill_query_pct_downsize_safe": 0.25, "p95_execution_ms_downsize_safe": 10_000, "p99_execution_ms_downsize_safe": 60_000,
        "time_at_max_pct_safe_reduce": 2.0, "max_cluster_cut_headroom_pct": 15.0,
        "time_at_min_pct_reduce": 15.0, "queue_wait_p95_ms_safe": 10_000, "min_cluster_busy_start_percentile": 90.0,
        "cold_start_added_share_max_pct": 10.0,
        "cost_change_min_annual_savings": 250.0, "queue_query_pct_safe": 1.0, "canary_p95_regression_pct": 40.0,
        "growth_hold_weekly_ratio": 1.20,
    },
}


def profile_values(name: str) -> Dict[str, Any]:
    """The profiled field values of a profile (balanced: the Thresholds defaults)."""
    if name == BALANCED:
        defaults = {f.name: f.default for f in dataclasses.fields(Thresholds)}
        return {k: defaults[k] for k in PROFILED_FIELDS}
    return dict(_OVERLAYS[name])


def thresholds_for_profile(base: Thresholds, name: str) -> Thresholds:
    """The thresholds a warehouse under ``name`` is evaluated with: ``base`` itself for balanced, otherwise ``base``
    with the profile's values for the profiled fields (every other field stays as in ``base``)."""
    return base if name == BALANCED else _overlaid(base, name)


@functools.lru_cache(maxsize=None)
def _overlaid(base: Thresholds, name: str) -> Thresholds:
    return dataclasses.replace(base, **_OVERLAYS[name])


def applied_thresholds(t: Thresholds) -> Dict[str, Dict[str, Any]]:
    """The profiled fields of ``t`` grouped by technique, as published in the package's cost_profile."""
    return {group: {f: getattr(t, f) for f in fields} for group, fields in COST_PROFILE_GROUPS.items()}


def nonprod_tag_match(tags: Mapping[str, Any], t: Thresholds) -> Optional[Tuple[str, str]]:
    """The first (key, value) of the warehouse tags, normalized to lower case, whose key is an environment key and whose
    value a nonprod value (``nonprod_tag_keys``, ``nonprod_tag_values``); None when no tag marks it nonprod."""
    keys, values = set(t.nonprod_tag_keys), set(t.nonprod_tag_values)
    matches = sorted((str(k).strip().lower(), str(v).strip().lower()) for k, v in (tags or {}).items())
    return next(((k, v) for k, v in matches if k in keys and v in values), None)


def _resolve_refs(selection: CostProfileSelection, evidence: Sequence[WarehouseEvidence]) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """(workspace_id, warehouse_id) -> (profile, reference) for the per-warehouse overrides; raises PublisherError for a
    reference that names no collected warehouse, a bare ID found in several workspaces, or one warehouse given two
    profiles through different references."""
    keys = sorted({(e.workspace_id, e.warehouse_id) for e in evidence})
    out: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for ref, name in selection.by_warehouse:
        if "/" in ref:
            matched: List[Tuple[str, str]] = [k for k in keys if k == tuple(ref.split("/", 1))]
        else:
            matched = [k for k in keys if k[1] == ref]
            if len(matched) > 1:
                raise PublisherError(f"--cost-profile: warehouse {ref} exists in several workspaces; use workspace_id/{ref}")
        if not matched:
            raise PublisherError(f"--cost-profile: warehouse {ref} is not among the collected warehouses")
        key = matched[0]
        if key in out and out[key][0] != name:
            raise PublisherError(f"--cost-profile: warehouse {key[0]}/{key[1]} is given two profiles ({out[key][0]}, {name})")
        out.setdefault(key, (name, ref))
    return out


def assign_cost_profiles(evidence: Sequence[WarehouseEvidence], selection: CostProfileSelection,
                         base: Thresholds) -> Dict[Tuple[str, str], CostProfileChoice]:
    """The cost profile of every collected warehouse, keyed by (workspace_id, warehouse_id).

    Precedence: a per-warehouse --cost-profile, then an all-warehouse --cost-profile, then the tag default (a nonprod
    tag on the current epoch selects aggressive; everything else is balanced). Raises PublisherError for a warehouse
    override that names no collected warehouse, so a typo never falls back silently.
    """
    overrides = _resolve_refs(selection, evidence)
    out: Dict[Tuple[str, str], CostProfileChoice] = {}
    for e in evidence:
        key = (e.workspace_id, e.warehouse_id)
        if key in overrides:
            name, ref = overrides[key]
            choice = CostProfileChoice(name, CostProfileSource.CLI_WAREHOUSE.value, f"--cost-profile {name}={ref}")
        elif selection.all_profile is not None:
            choice = CostProfileChoice(selection.all_profile, CostProfileSource.CLI_ALL.value, f"--cost-profile {selection.all_profile}")
        else:
            tag = nonprod_tag_match(e.current_epoch.tags, base)
            choice = (CostProfileChoice(AGGRESSIVE, CostProfileSource.TAG_DEFAULT.value, f"tag {tag[0]}={tag[1]}") if tag
                      else CostProfileChoice(BALANCED, CostProfileSource.DEFAULT.value, None))
        out[key] = choice
    return out


def log_assignments(choices: Mapping[Tuple[str, str], CostProfileChoice]) -> None:
    """One INFO line with the warehouse count per profile, and one DEBUG line per warehouse with its profile and source
    (identity and policy labels only)."""
    counts: Dict[str, int] = {}
    for (workspace_id, warehouse_id), choice in sorted(choices.items()):
        counts[choice.name] = counts.get(choice.name, 0) + 1
        LOGGER.debug("governor.cost_profile workspace_id=%s warehouse_id=%s profile=%s source=%s",
                     workspace_id, warehouse_id, choice.name, choice.source)
    LOGGER.info("governor.cost_profiles warehouses=%d %s", len(choices), " ".join(f"{name}={counts[name]}" for name in sorted(counts)))
