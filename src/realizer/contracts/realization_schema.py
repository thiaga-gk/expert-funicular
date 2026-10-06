"""JSON Schema builders for Realizer Contract v1.0.0.

The committed schema files are these builders' output serialized as ``json.dumps(schema, indent=2, sort_keys=True)``
plus a trailing newline (``schema/realization-package.schema.json`` and ``schema/realization-ledger-entry.schema.json``).
"""
from __future__ import annotations

from typing import Any, Dict

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
            "current_state": {"type": "object"},
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
