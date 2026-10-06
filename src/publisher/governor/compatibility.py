"""Migration compatibility gate: read-only API evidence (B) or the deterministic HITL fallback (A)."""
from __future__ import annotations

from typing import Any, Dict

from publisher.analyzer.epochs import carried_query_view
from publisher.analyzer.normalizer import hive_reference_count
from publisher.common.logging import LOGGER
from publisher.common.utils import safe_float
from publisher.contracts.source import WarehouseEvidence


def migration_compatibility(e: WarehouseEvidence, target_type: str) -> Dict[str, Any]:
    """Deterministic compatibility gate for a warehouse-type migration: read-only API evidence (B) or the HITL fallback (A).

    Returns {"target_warehouse_type", "assessment", "evidence_mode", "checks"}. Each check has a status (PASS, FAIL,
    REVIEW, UNKNOWN, PASS_WITH_LIMITATION, PASS_BY_EXISTING_SERVERLESS); the assessment is the worst of them: any FAIL
    gives FAIL, any REVIEW or UNKNOWN gives REVIEW_REQUIRED (human-in-the-loop), limitations give
    PASS_WITH_LIMITATIONS. Unknown evidence is never read as a pass. Evidence is read-only: system tables, query
    history aggregates and (mode B) the read-only workspace API; nothing is mutated and no statement text is kept.
    """
    q=carried_query_view(e.current_epoch.query)[0]; checks=[]
    target=target_type.upper(); api=dict(e.compatibility_evidence or {})
    if target=="SERVERLESS":
        region=(e.source_region or "").split(",")[0]
        observed=bool(e.billing_features.get("workspace_serverless_observed_from_system_compute_warehouses"))
        # No verified per-region Serverless availability list is used, so a region label proves nothing; a Serverless
        # warehouse already running in this workspace does.
        checks.append({"check":"aws_region_serverless_availability","status":"PASS" if observed else "UNKNOWN","evidence":{"region":region or None,"existing_serverless_warehouse_in_workspace":observed,"policy":"No verified per-region Serverless availability list; confirm the region supports Serverless SQL unless the workspace already runs a Serverless warehouse."},"source":"system.compute.warehouses"})
        checks.append({"check":"workspace_serverless_observed","status":"PASS" if observed else "UNKNOWN","evidence":observed,"source":"system.compute.warehouses"})
        if api.get("mode")=="B_READ_ONLY_API" and api.get("status")=="AVAILABLE":
            enabled=api.get("enable_serverless_compute")
            checks.append({"check":"workspace_serverless_enablement","status":"PASS" if enabled is True else "FAIL" if enabled is False else "UNKNOWN","evidence":enabled,"source":api.get("source")})
            external=api.get("external_legacy_hive_metastore_detected")
            checks.append({"check":"external_legacy_hive_metastore_workspace_setting","status":"FAIL" if external is True else "PASS" if external is False else "UNKNOWN","evidence":{"detected":external,"config_keys":api.get("external_legacy_hive_metastore_config_keys",[])},"source":api.get("source")})
            checks.append({"check":"s3_access_policy_compatibility","status":"PASS_BY_EXISTING_SERVERLESS" if observed else "UNKNOWN","evidence":api.get("s3_access_policy_status"),"source":api.get("source")})
        else:
            checks.append({"check":"workspace_serverless_enablement","status":"PASS_BY_EXISTING_SERVERLESS" if observed else "UNKNOWN","evidence":api,"source":"fallback A"})
            checks.append({"check":"external_legacy_hive_metastore_workspace_setting","status":"UNKNOWN","evidence":"Read-only compatibility API unavailable or workspace not directly connected","source":"fallback A"})
            checks.append({"check":"s3_access_policy_compatibility","status":"PASS_BY_EXISTING_SERVERLESS" if observed else "UNKNOWN","evidence":"No verified read-only endpoint used; HITL unless existing Serverless proves workspace eligibility","source":"fallback A"})
        visible=safe_float(q.get("statement_text_visible_pct_30")); hive=hive_reference_count(q)
        checks.append({"check":"legacy_hive_metastore_sql_reference","status":"REVIEW" if hive else "PASS_WITH_LIMITATION" if visible>0 else "UNKNOWN","evidence":{"visible_pct":visible,"reference_queries":hive},"source":"system.query.history"})
    else:
        observed=bool(e.billing_features.get("workspace_pro_classic_observed_from_system_compute_warehouses"))
        checks.append({"check":"workspace_pro_classic_observed","status":"PASS" if observed else "UNKNOWN","evidence":observed,"source":"system.compute.warehouses"})
        if api.get("mode")=="B_READ_ONLY_API" and api.get("status")=="AVAILABLE":
            types=api.get("enabled_warehouse_types") or {}
            if "PRO" in types:
                checks.append({"check":"pro_warehouse_type_enabled","status":"PASS" if types.get("PRO") else "FAIL","evidence":types.get("PRO"),"source":api.get("source")})
        region_prices=bool(e.aws_cost_model.get("region_instance_prices"))
        checks.append({"check":"aws_price_registry_region","status":"PASS" if region_prices else "UNKNOWN","evidence":e.source_region})
    statuses={x["status"] for x in checks}
    result="FAIL" if "FAIL" in statuses else "REVIEW_REQUIRED" if ("REVIEW" in statuses or "UNKNOWN" in statuses) else "PASS_WITH_LIMITATIONS" if ("PASS_WITH_LIMITATION" in statuses or "PASS_BY_EXISTING_SERVERLESS" in statuses) else "PASS"
    LOGGER.debug("governor.migration_compatibility workspace_id=%s warehouse_id=%s target=%s assessment=%s evidence_mode=%s open_checks=%s",
                 e.workspace_id, e.warehouse_id, target, result, api.get("mode", "A_HITL_FALLBACK"),
                 ",".join(c["check"] for c in checks if c["status"] in ("FAIL", "REVIEW", "UNKNOWN")) or "none")
    return {"target_warehouse_type":target,"assessment":result,"evidence_mode":api.get("mode","A_HITL_FALLBACK"),"checks":checks}
