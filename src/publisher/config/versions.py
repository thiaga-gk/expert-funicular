"""Component and format version identifiers. A decision-affecting change bumps the affected components here."""
from __future__ import annotations

from publisher.contracts import reporter_contract as rc


PUBLISHER_VERSION = "2.15.0"


CONTRACT_VERSION = rc.CONTRACT_VERSION


POLICY_VERSION = "sql-warehouse-policy-v2.14.0"


ANALYZER_VERSION = "sql-warehouse-analyzer-v2.14.0"


OPTIMIZER_VERSION = "sql-warehouse-optimizer-v2.14.0"


MODEL_VERSION = "epoch-runrate-v2.10.0"


ESTIMATOR_VERSION = "counterfactual-v2.14.0"


BUNDLE_MODEL_VERSION = "stateful-resource-bundle-v2.14.0"


REVIEWER_VERSION = "recommendation-reviewer-v2.14.0"


DECISION_VERSION = "warehouse-decision-graph-v2.14.0"


TECHNIQUE_CATALOG_VERSION = "technique-catalog-v2"


EVIDENCE_FORMAT_VERSION = "1.0.0"
