"""Reviewer package gate: report-contract consistency of the frozen packages (semantic invariants and the committed schema)."""
from __future__ import annotations

import json
from typing import Any, Mapping

from publisher.config.settings import _CONTRACT_SCHEMA_PATH
from publisher.contracts import reporter_contract as rc


def validate_locked_package_schema(payload: Mapping[str, Any]) -> None:
    """Validate the serialized package against the locked Draft 2020-12 schema."""
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"jsonschema>=4.23 is required to validate Reporter Contract v{rc.CONTRACT_VERSION} output") from exc
    schema = json.loads(_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(dict(payload))


def review_contract(package: rc.RecommendationPackage) -> None:
    """Report-contract consistency of one frozen package: semantic invariants, then the committed JSON Schema."""
    package.validate()
    validate_locked_package_schema(package.to_dict())
