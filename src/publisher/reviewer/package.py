"""Reviewer package gate: report-contract consistency of the frozen packages (semantic invariants and the committed schema)."""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from publisher.config.settings import _CONTRACT_SCHEMA_PATH
from publisher.contracts import reporter_contract as rc
from publisher.governor.cost_profiles import applied_thresholds, thresholds_for_profile
from publisher.governor.thresholds import Thresholds


def validate_locked_package_schema(payload: Mapping[str, Any]) -> None:
    """Validate the serialized package against the locked Draft 2020-12 schema.

    Raises RuntimeError when ``jsonschema`` is missing (a gate that cannot run must not pass) and the library's
    ValidationError on a violation.
    """
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"jsonschema>=4.23 is required to validate Reporter Contract v{rc.CONTRACT_VERSION} output") from exc
    schema = json.loads(_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(dict(payload))


def review_contract(package: rc.RecommendationPackage, thresholds: Optional[Thresholds] = None) -> None:
    """Report-contract consistency of one frozen package: semantic invariants, then the committed JSON Schema.

    With the run's base ``thresholds``, also checks that the published cost profile carries exactly the thresholds its
    name applies (raises ValueError otherwise).
    """
    package.validate()
    if thresholds is not None:
        name = rc.CostProfileName(package.cost_profile.name).value
        if package.cost_profile.applied_thresholds != applied_thresholds(thresholds_for_profile(thresholds, name)):
            raise ValueError(f"cost_profile.applied_thresholds do not match the {name} profile")
    validate_locked_package_schema(package.to_dict())
