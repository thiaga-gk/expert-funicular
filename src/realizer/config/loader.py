"""Input loading: the source package and the check that it is a package this Realizer reads, and the optional policy
override file.

The Realizer reads exactly the Reporter JSON Contract the Publisher writes (``SUPPORTED_PUBLISHER_CONTRACT``). A package
is accepted only when it passes the Publisher's committed JSON Schema and its id and full hash match its content, so a
package edited after publishing, or written by another contract version, never reaches the engine.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Mapping, Optional

from publisher.config.defaults import SUPPORTED_WAREHOUSE_TYPES
from publisher.contracts import reporter_contract as publisher_contract
from publisher.reviewer.package import validate_locked_package_schema
from realizer.common.utils import load_json
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION, observation_schema

SUPPORTED_PUBLISHER_CONTRACT = "3.1.0"


def load_package(path: str | Path) -> Any:
    """The RecommendationPackage JSON at *path*, parsed (it is checked by ``validate_package``)."""
    return load_json(path)


def load_policy_mapping(path: Optional[str | Path]) -> Optional[Any]:
    """The policy override mapping at *path*, or ``None`` when no file is given (``RealizerPolicy.from_mapping`` input)."""
    return load_json(path) if path else None


def validate_package(package: Any) -> None:
    """Accept *package* only if it is an unmodified Publisher package of the supported contract; raise ``ValueError``.

    Checks, in order: a mapping; ``contract_version`` equal to ``SUPPORTED_PUBLISHER_CONTRACT``; the Publisher's committed
    Draft 2020-12 schema; the ``PKG-`` id and ``provenance.package_hash_full_sha256`` recomputed from the hashed fields;
    the ``realization`` block is the Publisher's empty placeholder (status NOT_APPLIED, no records; the Realizer's own
    lineage is the RPKG, never this block); and no carried-forward plan (reserved, never published yet).
    """
    if not isinstance(package, Mapping):
        raise ValueError("a RecommendationPackage must be a JSON object")
    version = package.get("contract_version")
    if version != SUPPORTED_PUBLISHER_CONTRACT:
        raise ValueError(f"unsupported Publisher contract: {version} (this Realizer reads {SUPPORTED_PUBLISHER_CONTRACT})")
    try:
        validate_locked_package_schema(package)
    except Exception as exc:  # jsonschema.ValidationError: report the location, never the package values
        location = "/".join(str(p) for p in getattr(exc, "absolute_path", ())) or "<root>"
        raise ValueError(f"package fails the Reporter JSON Contract {SUPPORTED_PUBLISHER_CONTRACT} schema at {location}") from exc
    payload = publisher_contract.package_hash_payload_from_document(package)
    if publisher_contract.content_id("PKG", payload) != package["recommendation_package_id"]:
        raise ValueError(f"package id {package['recommendation_package_id']} does not match the package content")
    if publisher_contract.sha256_hex(payload) != package["provenance"]["package_hash_full_sha256"]:
        raise ValueError("provenance.package_hash_full_sha256 does not match the package content")
    realization = package["realization"]
    if realization.get("status") != "NOT_APPLIED" or realization.get("realization_records"):
        raise ValueError("package realization block must be the Publisher's empty NOT_APPLIED placeholder")
    if package["carried_forward_plan"] is not None:
        raise ValueError("packages with a carried-forward plan are not supported yet")
    recommended = package["recommended_plan"].get("scenario_id")
    if recommended is not None and recommended not in {s["scenario_id"] for s in package["scenarios"]}:
        raise ValueError(f"recommended_plan.scenario_id {recommended} names no scenario of the package")
    warehouse_type = str(package["current_config"].get("warehouse_type") or "").upper()
    if warehouse_type not in SUPPORTED_WAREHOUSE_TYPES:
        raise ValueError(f"unsupported warehouse type in the package: {warehouse_type or None}")


TIME_FIELDS = ("observed_at_utc", "recorded_at_utc", "applied_at_utc", "window_start_utc", "window_end_utc")


def _time(observation: Mapping[str, Any], field: str) -> Optional[dt.datetime]:
    """The observation's *field* as an aware datetime (``None`` when null); a value that is not an ISO-8601 time with a
    UTC offset raises ``ValueError`` naming the field, never the value."""
    value = observation[field]
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"observation {field} is not an ISO-8601 time with a UTC offset")
    return parsed


def validate_observation(observation: Any, package: Mapping[str, Any]) -> None:
    """Accept *observation* only if it is a well-formed observation of *package*'s warehouse; raise ``ValueError``.

    Checks, in order: observation format ``OBSERVATION_FORMAT_VERSION`` against its JSON Schema (every key present,
    strict types, enumerated vocabularies); the observation names the package's workspace and warehouse; its times are
    ISO-8601 with a UTC offset and ordered (applied at or before observed, window start at or before window end, window end at or before observed);
    measured economics come with a window; ``applied_scenario_id`` names a scenario of the package; the observed warehouse
    type is a supported one. The messages name the failing field, never its value, except ids and the type.
    """
    import jsonschema  # a gate that cannot run must not pass; jsonschema is a declared dependency

    if not isinstance(observation, Mapping):
        raise ValueError("an observation must be a JSON object")
    if observation.get("observation_format_version") != OBSERVATION_FORMAT_VERSION:
        raise ValueError(f"unsupported observation format: {observation.get('observation_format_version')} (this Realizer reads {OBSERVATION_FORMAT_VERSION})")
    validator = jsonschema.Draft202012Validator(observation_schema(), format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    error = next(iter(sorted(validator.iter_errors(dict(observation)), key=lambda e: list(e.absolute_path))), None)
    if error is not None:
        location = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise ValueError(f"observation fails format {OBSERVATION_FORMAT_VERSION} at {location} ({error.validator})")
    warehouse = package["warehouse"]
    if (observation["workspace_id"], observation["warehouse_id"]) != (warehouse["workspace_id"], warehouse["warehouse_id"]):
        raise ValueError(f"observation is for {observation['workspace_id']}/{observation['warehouse_id']}, "
                         f"the package for {warehouse['workspace_id']}/{warehouse['warehouse_id']}")
    times = {field: _time(observation, field) for field in TIME_FIELDS}
    observed, applied = times["observed_at_utc"], times["applied_at_utc"]
    start, end = times["window_start_utc"], times["window_end_utc"]
    if applied is not None and applied > observed:
        raise ValueError("observation applied_at_utc is after observed_at_utc")
    if start is not None and end is not None and start > end:
        raise ValueError("observation window_start_utc is after window_end_utc")
    if end is not None and end > observed:
        raise ValueError("observation window_end_utc is after observed_at_utc")
    if observation["economics"] is not None and (start is None or end is None):
        raise ValueError("observation economics need window_start_utc and window_end_utc")
    scenario = observation["applied_scenario_id"]
    if scenario is not None and scenario not in {s["scenario_id"] for s in package["scenarios"]}:
        raise ValueError(f"applied_scenario_id {scenario} names no scenario of the package")
    observed_type = observation["config"]["warehouse_type"].strip().upper()
    if observed_type not in SUPPORTED_WAREHOUSE_TYPES:
        raise ValueError(f"unsupported warehouse type observed: {observed_type}")
