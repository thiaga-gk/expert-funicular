"""Internal deterministic join keys (16 hex). Public IDs come from publisher.contracts.reporter_contract."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from publisher.common.hashing import canonical_json
from publisher.config.versions import ANALYZER_VERSION, CONTRACT_VERSION, DECISION_VERSION, ESTIMATOR_VERSION, POLICY_VERSION


# Internal join keys. They tie findings, decisions and plan steps together while a run is in memory, and `epoch_id` also
# keys epochs in evidence.json. They are not public identities: packages carry only the contract IDs from
# publisher/contracts/reporter_contract.py (make_epoch_id, make_evidence_id, make_candidate_id, make_scenario_id), which never read
# these. tests/unit/packager/test_locked_reporter_integration.py pins that.
def epoch_id(workspace_id: str, warehouse_id: str, start: str) -> str:
    """Internal epoch key: 16 hex over workspace, warehouse and start time. Not the public EPOCH- identity."""
    raw = f"{workspace_id}|{warehouse_id}|{start}"
    return "EPOCH-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def finding_id(workspace_id: str, warehouse_id: str, knob: str, epoch: str, metric: str) -> str:
    """Internal finding key, mapped to a public EVID- identity when the package is built."""
    raw = f"{ANALYZER_VERSION}|{workspace_id}|{warehouse_id}|{knob}|{epoch}|{metric}"
    return "FND-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def recommendation_id(workspace_id: str, warehouse_id: str, knob: str, epoch: str, current: Mapping[str, Any], recommended: Mapping[str, Any]) -> str:
    """Internal decision key, mapped to a public CAND- identity when the package is built."""
    raw = canonical_json({
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "decision_version": DECISION_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "knob": knob,
        "epoch": epoch,
        "current": current,
        "recommended": recommended,
    })
    return f"WHOPT-{knob.upper()}-{hashlib.sha256(raw.encode()).hexdigest()[:16].upper()}"


def scenario_id(workspace_id: str, warehouse_id: str, name: str, recommendation_ids: Sequence[str]) -> str:
    """Internal plan key, mapped to a public SCN- identity when the package is built."""
    raw = canonical_json({"v": DECISION_VERSION, "workspace": workspace_id, "warehouse": warehouse_id, "name": name, "ids": list(recommendation_ids)})
    return "SCN-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
