"""Realizer settings: version identifiers and the configuration knobs it reconciles.

``PRODUCT_VERSION`` is the repository release (the Publisher's version module). It and the component versions are written
into RLE and RPKG provenance; the RSNAP id hashes ``PROJECTOR_VERSION`` and counterfactual ids hash
``MODELER_GATEWAY_VERSION``, so changing those changes output IDs.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from publisher.config.defaults import SIZE_ORDER as PUBLISHER_SIZE_ORDER
from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES as PUBLISHER_SUPPORTED_WAREHOUSE_TYPES
from publisher.governor.thresholds import Thresholds as _PublisherThresholds
from publisher.config.settings import REPO_ROOT
from publisher.config.versions import PUBLISHER_VERSION

# The release of this repository (one version for the Publisher and the Realizer), written into ledger-entry and
# realization-package provenance.
PRODUCT_VERSION = PUBLISHER_VERSION
REALIZER_VERSION = "1.1.0"
PROJECTOR_VERSION = "realizer-projector-v1.0.0"
POLICY_VERSION = "realizer-policy-v1.1.0"
ANALYZER_GATEWAY_VERSION = "post-change-analyzer-adapter-v1.1.0"
MODELER_GATEWAY_VERSION = "m08-realization-adapter-v1.1.0"
ESTIMATOR_GATEWAY_VERSION = "realized-estimator-adapter-v1.1.0"
ATTRIBUTION_VERSION = "realizer-attribution-v1.1.0"
EXCLUSIVITY_VERSION = "realizer-exclusivity-v1.0.0"

CONFIG_KNOBS = (
    "warehouse_type",
    "warehouse_size",
    "min_clusters",
    "max_clusters",
    "auto_stop_minutes",
    "warehouse_channel",
    "enable_photon",
)

# The configuration a decision is made on, which the configuration hash covers (and so application identity and drift):
# the observed configuration also records the channel (no priced effect) and Photon (a billing-mix fact, never a change).
HASHED_CONFIG_KNOBS = ("warehouse_type", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes")
# Decision knobs whose change has no priced effect: recorded as an independent change, never drift, never against a claim.
UNPRICED_DECISION_KNOBS = ("warehouse_channel",)

# Reuse Publisher's canonical size registry; Realizer must not maintain a competing order.
SIZE_ORDER = {name: idx for idx, name in enumerate(PUBLISHER_SIZE_ORDER)}

# The warehouse types the Publisher supports (fail-closed): a measurement never routes another type into its algorithms.
SUPPORTED_WAREHOUSE_TYPES = PUBLISHER_SUPPORTED_WAREHOUSE_TYPES

# Execution states of a plan step that cannot carry realization authority (its change is only recorded as independent).
NON_AUTHORITATIVE_EXECUTION_STATES = frozenset({"PENDING_REEVALUATION", "BLOCKED", "INVALIDATED"})

# The regional EC2 price registry a live measurement prices modeled AWS with when none is given: the one committed with
# the Publisher, so the realized AWS component is priced like the package's (pass the Publisher run's own registry
# with --aws-price-registry when it used another).
DEFAULT_AWS_PRICE_REGISTRY = REPO_ROOT / "pricing" / "aws_ec2_on_demand.json"

# The Publisher's canary guard window: a canary-type change (one size down, a lower minimum) is guarded for its first 30
# days, so its realization stays provisional until then.
CANARY_GUARD_WINDOW_DAYS = 30
# Publisher guard values the Realizer's validation applies (its canary guard and cold-start budget), defaults of its
# Thresholds; the profiled ones are replaced by the package's cost-profile values (``guard_thresholds``).
GUARD_THRESHOLD_FIELDS = ("failure_cancel_pct_downsize_safe", "spill_query_pct_downsize_safe", "queue_query_pct_safe",
                          "canary_p95_regression_pct", "canary_p95_min_queries", "min_query_sample_size",
                          "severity_min_affected_queries", "cold_start_added_share_max_pct")
PUBLISHER_GUARD_DEFAULTS: Dict[str, float] = {f: float(getattr(_PublisherThresholds(), f)) for f in GUARD_THRESHOLD_FIELDS}


def guard_thresholds(applied_thresholds: Optional[Mapping[str, Any]]) -> Dict[str, float]:
    """The guard values for a package: the Publisher defaults, with every value its cost profile applied
    (``cost_profile.applied_thresholds``, grouped by technique) in their place."""
    values = dict(PUBLISHER_GUARD_DEFAULTS)
    for group in (applied_thresholds or {}).values():
        if isinstance(group, Mapping):
            for k, v in group.items():
                if k in values and isinstance(v, (int, float)) and not isinstance(v, bool):
                    values[k] = float(v)
    return values
