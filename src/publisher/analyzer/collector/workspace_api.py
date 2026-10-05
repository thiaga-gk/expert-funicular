"""Read-only Databricks REST: OAuth from a CLI profile, execution-target validation and workspace compatibility evidence."""
from __future__ import annotations

import configparser
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.config.versions import PUBLISHER_VERSION


@dataclass(frozen=True)
class AuthContext:
    """Resolved local OAuth authentication context; token is never reported or logged."""
    profile: str
    host: str
    http_path: str
    access_token: str
    token_expiry: Optional[str]
    execution_kind: str
    execution_id: str
    workspace_id: Optional[str] = None


def _load_profile_values(profile: str, config_file: Optional[str] = None) -> Dict[str, str]:
    path = Path(config_file or os.getenv("DATABRICKS_CONFIG_FILE") or Path.home() / ".databrickscfg")
    if not path.exists():
        raise PublisherError(f"Databricks CLI config not found: {path}")
    parser = configparser.ConfigParser()
    parser.read(path)
    if profile == "DEFAULT":
        section: Mapping[str, str] = parser.defaults()
    elif parser.has_section(profile):
        section = parser[profile]
    else:
        raise PublisherError(f"Databricks CLI profile not found: {profile} in {path}")
    return {str(k): str(v) for k, v in section.items()}


def _load_profile_host(profile: str, config_file: Optional[str] = None) -> str:
    section = _load_profile_values(profile, config_file)
    host = str(section.get("host") or "").strip()
    if not host:
        raise PublisherError(f"Databricks host is missing from CLI profile: {profile}")
    return host.replace("https://", "").replace("http://", "").rstrip("/")


def _resolve_cli_token(profile: str, config_file: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    values = _load_profile_values(profile, config_file)
    if values.get("token"):
        raise PublisherError(f"Profile {profile} contains a PAT token. Use an OAuth U2M Databricks CLI profile.")
    host = _load_profile_host(profile, config_file)
    cmd = ["databricks", "auth", "token", profile, "-o", "json"]
    clean_env = dict(os.environ)
    clean_env.pop("DATABRICKS_TOKEN", None)
    if config_file:
        clean_env["DATABRICKS_CONFIG_FILE"] = str(config_file)
    LOGGER.info("auth.resolve profile=%s host=%s", profile, host)
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=clean_env)
    except FileNotFoundError as exc:
        raise PublisherError("Databricks CLI is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise PublisherError(f"Unable to resolve OAuth token for profile {profile}: {detail}") from exc
    payload = json.loads(result.stdout)
    token = str(payload.get("access_token") or "")
    if not token:
        raise PublisherError(f"Databricks CLI returned no access token for profile {profile}")
    return host, token, str(payload.get("expiry") or "") or None


def _api_get(host: str, token: str, path: str, params: Optional[Mapping[str, Any]] = None, *, return_headers: bool = False) -> Any:
    url = f"https://{host.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": f"sql-warehouse-optimizer/{PUBLISHER_VERSION}"}, method="GET")
    started = time.monotonic()
    LOGGER.info("api.get start path=%s", path)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body.strip() else {}
            LOGGER.info("api.get done path=%s status=%s elapsed_ms=%d", path, getattr(response, "status", 200), int((time.monotonic()-started)*1000))
            return (payload, dict(response.headers.items())) if return_headers else payload
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("api.get failed path=%s error=%s", path, type(exc).__name__)
        raise


def _current_workspace_id(host: str, token: str) -> Optional[str]:
    try:
        _payload, headers = _api_get(host, token, "/api/2.0/preview/scim/v2/Me", return_headers=True)
        for key, value in headers.items():
            if key.lower() == "x-databricks-org-id" and str(value).strip():
                return str(value).strip()
    except Exception:
        return None
    return None


def _validate_all_purpose_cluster(host: str, token: str, cluster_id: str) -> Dict[str, Any]:
    payload = _api_get(host, token, "/api/2.1/clusters/get", {"cluster_id": cluster_id})
    source = str(payload.get("cluster_source") or "").upper()
    if source == "JOB":
        raise PublisherError("The Databricks SQL Connector does not support jobs compute. Supply an all-purpose cluster ID.")
    LOGGER.info("execution.cluster validated cluster_id=%s name=%s source=%s state=%s", cluster_id, payload.get("cluster_name"), source or "UNKNOWN", payload.get("state"))
    return {"cluster_id": cluster_id, "cluster_name": payload.get("cluster_name"), "cluster_source": source, "state": payload.get("state")}


def resolve_cli_oauth(profile: str, execution_kind: str, execution_id: Optional[str] = None, config_file: Optional[str] = None, workspace_id: Optional[str] = None) -> AuthContext:
    """Resolve U2M OAuth and build the connector path for a SQL warehouse or all-purpose cluster.

    Read-only REST is used only when necessary to resolve/validate an all-purpose
    cluster execution target. It is never a source of recommendation configs.
    Backward compatibility: resolve_cli_oauth(profile, warehouse_id, config_file).
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
