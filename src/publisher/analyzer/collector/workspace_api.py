"""Read-only Databricks REST: OAuth from a CLI profile, execution-target validation and workspace compatibility evidence.

Only GET requests are issued; there is no mutation or apply API. Authentication is an OAuth U2M token minted by the
Databricks CLI for a profile; a profile holding a static PAT is rejected. Tokens, Authorization headers and
data-access config values are never logged or reported; logs carry the REST path, status and exception class only.

The OAuth, REST GET and profile helpers are the shared read-only access layer (``workflow_shared.databricks.access``)
bound to the publisher's identity, ``PUBLISHER_ACCESS``: the ``publisher`` logger, the
``sql-warehouse-optimizer/<version>`` user agent and ``PublisherError``. They stay module globals here, so the
functions below (and patches of this module) resolve them by name.
"""
from __future__ import annotations

import os
import subprocess  # noqa: F401  (the shared token helper runs subprocess.run; tests patch it through this module)
from typing import Any, Dict, List, Mapping, Optional, Tuple

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.config.versions import PUBLISHER_VERSION
from workflow_shared.databricks import access
from workflow_shared.databricks.access import AccessConfig, AuthContext


PUBLISHER_ACCESS = AccessConfig(logger=LOGGER, user_agent=f"sql-warehouse-optimizer/{PUBLISHER_VERSION}", error_type=PublisherError)


def _load_profile_values(profile: str, config_file: Optional[str] = None) -> Dict[str, str]:
    """Raw key/values of a Databricks CLI profile; PublisherError when the file or profile is missing."""
    return access.load_profile_values(PUBLISHER_ACCESS, profile, config_file)


def _load_profile_host(profile: str, config_file: Optional[str] = None) -> str:
    """Workspace host of the profile without scheme or trailing slash; PublisherError when the profile has none."""
    return access.load_profile_host(PUBLISHER_ACCESS, profile, config_file)


def _resolve_cli_token(profile: str, config_file: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    """``(host, token, expiry)`` from ``databricks auth token``; PublisherError for a PAT profile or a CLI failure."""
    return access.resolve_cli_token(PUBLISHER_ACCESS, profile, config_file)


def _api_get(host: str, token: str, path: str, params: Optional[Mapping[str, Any]] = None, *, return_headers: bool = False) -> Any:
    """GET ``path`` and return the parsed JSON (plus response headers when asked); errors are logged and re-raised."""
    return access.api_get(PUBLISHER_ACCESS, host, token, path, params, return_headers=return_headers)


def _current_workspace_id(host: str, token: str) -> Optional[str]:
    """Workspace ID from the SCIM ``Me`` response header, or None when it cannot be read (best effort)."""
    return access.current_workspace_id(PUBLISHER_ACCESS, host, token)


def _validate_all_purpose_cluster(host: str, token: str, cluster_id: str) -> Dict[str, Any]:
    """Read the cluster and reject a jobs-compute cluster, which the SQL connector cannot use; returns its summary."""
    payload = _api_get(host, token, "/api/2.1/clusters/get", {"cluster_id": cluster_id})
    source = str(payload.get("cluster_source") or "").upper()
    if source == "JOB":
        raise PublisherError("The Databricks SQL Connector does not support jobs compute. Supply an all-purpose cluster ID.")
    LOGGER.info("execution.cluster validated cluster_id=%s source=%s state=%s", cluster_id, source or "UNKNOWN", payload.get("state"))
    return {"cluster_id": cluster_id, "cluster_name": payload.get("cluster_name"), "cluster_source": source, "state": payload.get("state")}


def resolve_cli_oauth(profile: str, execution_kind: str, execution_id: Optional[str] = None, config_file: Optional[str] = None, workspace_id: Optional[str] = None) -> AuthContext:
    """Resolve U2M OAuth and build the connector path for a SQL warehouse or all-purpose cluster.

    Read-only REST is used only when necessary to resolve/validate an all-purpose
    cluster execution target. It is never a source of recommendation configs.
    Backward compatibility: resolve_cli_oauth(profile, warehouse_id, config_file) is still accepted (a first argument
    that is not "warehouse"/"cluster" is read as the warehouse ID and the second as the config file).
    For a cluster the workspace ID comes from the argument, DATABRICKS_WORKSPACE_ID, then a read-only REST lookup;
    PublisherError when it cannot be resolved. A warehouse target needs no REST call.
    """
    if execution_kind.lower().strip() not in {"warehouse","cluster"}:
        legacy_warehouse_id=execution_kind
        if config_file is None and execution_id:
            config_file=str(execution_id)
        execution_kind="warehouse"; execution_id=legacy_warehouse_id
    if not execution_id:
        raise PublisherError("execution_id is required")
    host, token, expiry = _resolve_cli_token(profile, config_file)
    kind = execution_kind.lower().strip()
    resolved_workspace_id = workspace_id or os.getenv("DATABRICKS_WORKSPACE_ID")
    if kind == "warehouse":
        http_path = f"/sql/1.0/warehouses/{execution_id}"
    elif kind == "cluster":
        _validate_all_purpose_cluster(host, token, execution_id)
        resolved_workspace_id = resolved_workspace_id or _current_workspace_id(host, token)
        if not resolved_workspace_id:
            raise PublisherError("Unable to resolve workspace ID for all-purpose cluster HTTP path. Supply --workspace-id or DATABRICKS_WORKSPACE_ID.")
        http_path = f"sql/protocolv1/o/{resolved_workspace_id}/{execution_id}"
    else:
        raise PublisherError(f"Unsupported execution kind: {execution_kind}. Use warehouse or cluster.")
    LOGGER.info("execution.target kind=%s id=%s workspace_id=%s http_path=%s", kind, execution_id, resolved_workspace_id or "UNKNOWN", http_path)
    return AuthContext(profile=profile, host=host, http_path=http_path, access_token=token, token_expiry=expiry, execution_kind=kind, execution_id=execution_id, workspace_id=resolved_workspace_id)


def _external_hive_config_keys(data_access_config: Any) -> List[str]:
    """Sorted, de-duplicated config key names that indicate an external legacy Hive metastore.

    Only key names are inspected and returned; the values (which may hold credentials) are never read.
    """
    risky_tokens=("hive.metastore.uris","javax.jdo.option.connectionurl","datanucleus.connectionurl")
    keys=[]
    for item in data_access_config or []:
        key=str((item or {}).get("key") or "").strip()
        lower=key.lower()
        if key and any(tok in lower for tok in risky_tokens):
            keys.append(key)
    return sorted(set(keys))


def collect_workspace_compatibility_evidence(auth: AuthContext, mode: str = "auto") -> Dict[str, Any]:
    """Use read-only workspace SQL config when available; otherwise fall back to HITL.

    ``mode`` "off" skips the REST call (status API_DISABLED); any other value tries it. Every failure degrades to a
    HITL fallback record (WORKSPACE_ID_UNAVAILABLE or READ_ONLY_API_UNAVAILABLE) and never raises, so the governor
    treats migration compatibility as unproven rather than assumed.

    Data-access values are never retained or logged. Only config key names needed
    to detect an external legacy Hive metastore are kept.
    """
    if mode == "off":
        LOGGER.info("compatibility.api disabled workspace_id=%s", auth.workspace_id or "UNKNOWN")
        return {"mode":"A_HITL_ONLY","status":"API_DISABLED","workspace_id":auth.workspace_id}
    workspace_id=auth.workspace_id or _current_workspace_id(auth.host,auth.access_token)
    if not workspace_id:
        LOGGER.warning("compatibility.fallback reason=WORKSPACE_ID_UNAVAILABLE")
        return {"mode":"A_HITL_FALLBACK","status":"WORKSPACE_ID_UNAVAILABLE","workspace_id":None}
    try:
        cfg=_api_get(auth.host,auth.access_token,"/api/2.0/sql/config/warehouses")
        hive_keys=_external_hive_config_keys(cfg.get("data_access_config"))
        enabled_types={str(x.get("warehouse_type") or "").upper(): bool(x.get("enabled")) for x in (cfg.get("enabled_warehouse_types") or [])}
        LOGGER.info("compatibility.api available workspace_id=%s external_legacy_hive=%s", workspace_id, bool(hive_keys))
        return {
            "mode":"B_READ_ONLY_API",
            "status":"AVAILABLE",
            "workspace_id":str(workspace_id),
            "source":"GET /api/2.0/sql/config/warehouses",
            "enable_serverless_compute":cfg.get("enable_serverless_compute"),
            "enabled_warehouse_types":enabled_types,
            "security_policy":cfg.get("security_policy"),
            "external_legacy_hive_metastore_detected":bool(hive_keys),
            "external_legacy_hive_metastore_config_keys":hive_keys,
            "s3_access_policy_status":"UNKNOWN_NOT_EXPOSED_BY_VERIFIED_WORKSPACE_SQL_CONFIG_API",
            "data_access_config_values_retained":False,
        }
    except Exception as exc:
        LOGGER.warning("compatibility.fallback workspace_id=%s reason=READ_ONLY_API_UNAVAILABLE error=%s", workspace_id, type(exc).__name__)
        return {"mode":"A_HITL_FALLBACK","status":"READ_ONLY_API_UNAVAILABLE","workspace_id":str(workspace_id),"error_type":type(exc).__name__}
