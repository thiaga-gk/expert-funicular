"""Governor output: the policy decisions the Realizer may use, each held lazily.

``GovernanceResult`` is an internal hand-off value, not public contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

if TYPE_CHECKING:  # annotations only: the contracts import nothing at run time from common or governor
    from realizer.common.utils import Deferred
    from realizer.governor.policy import RealizerPolicy


@dataclass(frozen=True)
class GovernanceResult:
    """The policy in force and the policy checks of one observation.

    Each check is evaluated by the Governor when it runs and held as a ``Deferred``; the consumer calls ``get()`` at
    the point where it has always needed the answer. A check whose inputs are malformed therefore raises there, with
    the same exception, and a check that is never needed never raises, exactly as when the checks ran inline. All four
    checks (and nothing else) are ``None`` when the run has no observation.

    ``readiness``: evidence sufficiency (minimum days and queries, representative regime, evidence quality).
    ``performance``: the hard performance guardrails of the validation episode.
    ``counterfactual``: ``(claim eligible, method)`` for the counterfactual baseline.
    ``economic_quality``: the financial-quality gates of the economics.
    """
    policy: "RealizerPolicy"
    policy_snapshot_id: "Deferred[str]"
    readiness: Optional["Deferred[Mapping[str, Any]]"] = None
    performance: Optional["Deferred[Mapping[str, Any]]"] = None
    counterfactual: Optional["Deferred[Tuple[bool, str]]"] = None
    economic_quality: Optional["Deferred[Mapping[str, Any]]"] = None
