"""JSON Schema builders for Realizer Contract v1.0.0.

``validate_realization_package`` and ``validate_ledger_entry`` check every document the Realizer writes, before it is
written. The committed schema files are these builders' output serialized as ``json.dumps(schema, indent=2, sort_keys=True)``
plus a trailing newline (``schema/realization-package.schema.json`` and ``schema/realization-ledger-entry.schema.json``).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Mapping

from realizer.contracts import realization as rc


def realization_package_schema() -> Dict[str, Any]:
    required = [
        "contract_version", "realization_package_id", "snapshot_id", "source_recommendation",
        "warehouse", "as_of_utc", "current_state", "application", "validation", "economics",
        "attribution", "maturity", "drift", "regeneration", "history", "provenance",
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RealizationPackage",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "contract_version": {"const": rc.CONTRACT_VERSION},
            "realization_package_id": {"type": "string", "pattern": r"^RPKG-[0-9A-F]{24}$"},
            "snapshot_id": {"type": "string", "pattern": r"^RSNAP-[0-9A-F]{24}$"},
            "source_recommendation": {"type": "object"},
            "warehouse": {"type": "object"},
            "as_of_utc": {"type": "string", "format": "date-time"},
            "current_state": {
                "type": "object",
                "additionalProperties": False,
                "required": ["realization_state", "validation_state", "maturity_state", "source_package_disposition"],
                "properties": {
                    "realization_state": {"enum": [x.value for x in rc.RealizationState]},
                    "validation_state": {"enum": [x.value for x in rc.ValidationState]},
                    "maturity_state": {"enum": [x.value for x in rc.MaturityState]},
                    "source_package_disposition": {"enum": [x.value for x in rc.SourcePackageDisposition]},
                },
            },
            "application": {"type": "object"},
            "validation": {"type": "object"},
            "economics": {"type": "object"},
            "attribution": {"type": "object"},
            "maturity": {"type": "object"},
            "drift": {"type": "object"},
            "regeneration": {"type": "object"},
            "history": {"type": "object"},
            "provenance": {"type": "object"},
        },
    }


def ledger_entry_schema() -> Dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RealizationLedgerEntry",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ledger_entry_id", "ledger_contract_version", "realization_package_id",
            "recommendation_package_id", "event_type", "effective_at_utc", "recorded_at_utc",
            "workspace_id", "warehouse_id", "application_episode_id", "validation_episode_id",
            "measurement_id", "payload", "evidence_refs", "caused_by_entry_id",
            "corrects_entry_id", "provenance",
        ],
        "properties": {
            "ledger_entry_id": {"type": "string", "pattern": r"^RLE-[0-9A-F]{24}$"},
            "ledger_contract_version": {"const": rc.LEDGER_CONTRACT_VERSION},
            "realization_package_id": {"type": "string", "pattern": r"^RPKG-[0-9A-F]{24}$"},
            "recommendation_package_id": {"type": "string", "pattern": r"^PKG-[0-9A-F]+$"},
            "event_type": {"enum": [x.value for x in rc.EventType]},
            "effective_at_utc": {"type": "string", "format": "date-time"},
            "recorded_at_utc": {"type": "string", "format": "date-time"},
            "workspace_id": {"type": "string", "minLength": 1},
            "warehouse_id": {"type": "string", "minLength": 1},
            "application_episode_id": {"type": ["string", "null"]},
            "validation_episode_id": {"type": ["string", "null"]},
            "measurement_id": {"type": ["string", "null"]},
            "payload": {"type": "object"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "caused_by_entry_id": {"type": ["string", "null"]},
            "corrects_entry_id": {"type": ["string", "null"]},
            "provenance": {"type": "object"},
        },
    }


@lru_cache(maxsize=None)
def _validator(builder: str) -> Any:
    import jsonschema  # a declared dependency; a gate that cannot run must not pass

    return jsonschema.Draft202012Validator(globals()[builder]())


def validate_realization_package(document: Mapping[str, Any]) -> None:
    """Check a realization package or snapshot document against its schema before it is written (raises
    ``jsonschema.ValidationError``: an invalid output is a defect, never written)."""
    _validator("realization_package_schema").validate(dict(document))


def validate_ledger_entry(document: Mapping[str, Any]) -> None:
    """Check a ledger entry document against its schema before it is appended (raises ``jsonschema.ValidationError``)."""
    _validator("ledger_entry_schema").validate(dict(document))
