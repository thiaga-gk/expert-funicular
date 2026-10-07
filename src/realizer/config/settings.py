"""Realizer settings: version identifiers and the configuration knobs it reconciles.

``PRODUCT_VERSION`` is the repository release (the Publisher's version module). It and the component versions are written
into RLE and RPKG provenance; the RSNAP id hashes ``PROJECTOR_VERSION`` and counterfactual ids hash
``MODELER_GATEWAY_VERSION``, so changing those changes output IDs.
"""
from __future__ import annotations

from publisher.config.defaults import SIZE_ORDER as PUBLISHER_SIZE_ORDER
from publisher.config.settings import REPO_ROOT
from publisher.config.versions import PUBLISHER_VERSION

# The release of this repository (one version for the Publisher and the Realizer), written into ledger-entry and
# realization-package provenance.
PRODUCT_VERSION = PUBLISHER_VERSION
REALIZER_VERSION = "1.0.0"
PROJECTOR_VERSION = "realizer-projector-v1.0.0"
POLICY_VERSION = "realizer-policy-v1.0.0"
ANALYZER_GATEWAY_VERSION = "post-change-analyzer-adapter-v1.0.0"
MODELER_GATEWAY_VERSION = "m08-realization-adapter-v1.0.0"
ESTIMATOR_GATEWAY_VERSION = "realized-estimator-adapter-v1.0.0"
ATTRIBUTION_VERSION = "realizer-attribution-v1.0.0"
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

# Reuse Publisher's canonical size registry; Realizer must not maintain a competing order.
SIZE_ORDER = {name: idx for idx, name in enumerate(PUBLISHER_SIZE_ORDER)}

# Execution states of a plan step that cannot carry realization authority (its change is only recorded as independent).
NON_AUTHORITATIVE_EXECUTION_STATES = frozenset({"PENDING_REEVALUATION", "BLOCKED", "INVALIDATED"})

# The regional EC2 price registry a live measurement prices modeled AWS with when none is given: the one committed with
# the Publisher, so the realized AWS component is priced like the package's (pass the Publisher run's own registry
# with --aws-price-registry when it used another).
DEFAULT_AWS_PRICE_REGISTRY = REPO_ROOT / "pricing" / "aws_ec2_on_demand.json"
