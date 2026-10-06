"""Run scope: which knobs a run may recommend and which warehouses it reports on (resolved by the config layer)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from publisher.config.defaults import ACTIVE_KNOBS, SCOPE_SCENARIOS


@dataclass(frozen=True)
class WarehouseSelector:
    """One requested warehouse: ``warehouse_id`` alone, or qualified by ``workspace_id`` when the ID is ambiguous."""
    warehouse_id: str
    workspace_id: Optional[str] = None

    def label(self) -> str:
        """The selector as the user wrote it: ``<warehouse_id>`` or ``<workspace_id>/<warehouse_id>``."""
        return self.warehouse_id if self.workspace_id is None else f"{self.workspace_id}/{self.warehouse_id}"


@dataclass(frozen=True)
class RunScope:
    """The knobs a run may recommend (sorted) and the selected warehouses (empty = all, sorted).

    Scope is a decision input: the Optimizer drops out-of-scope candidates (a canary rollback is always kept), the
    Packager builds only the in-scope scenarios, and a restricted scope is recorded in the package. The full scope
    (every active knob, all warehouses) is the default and reproduces an unscoped run.
    """
    knobs: Tuple[str, ...] = tuple(sorted(ACTIVE_KNOBS))
    warehouses: Tuple[WarehouseSelector, ...] = ()

    @property
    def is_full(self) -> bool:
        """Every active knob is in scope (the warehouse selection does not change what a package contains)."""
        return set(self.knobs) == set(ACTIVE_KNOBS)

    def scenario_enabled(self, scenario: str) -> bool:
        """At least one technique of ``scenario`` (a SCOPE_SCENARIOS key) is in scope."""
        return any(k in self.knobs for k in SCOPE_SCENARIOS[scenario][1].values())

    def scenario_knobs(self) -> List[Tuple[str, List[str]]]:
        """(contract scenario type, sorted in-scope knobs) per enabled scenario, sorted by scenario type."""
        out = [(stype, sorted(k for k in techniques.values() if k in self.knobs)) for stype, techniques in SCOPE_SCENARIOS.values()]
        return sorted((stype, knobs) for stype, knobs in out if knobs)

    def describe(self) -> str:
        """Human label, e.g. ``current: auto_stop; migration: warehouse_type`` (``full`` for the full scope)."""
        if self.is_full:
            return "full"
        parts: List[str] = []
        for scenario, (_, techniques) in SCOPE_SCENARIOS.items():
            names = [name for name, knob in techniques.items() if knob in self.knobs]
            if names:
                parts.append(f"{scenario}: {', '.join(names)}")
        return "; ".join(parts)


FULL_SCOPE = RunScope()


SCOPE_FORMAT_VERSION = "1.0.0"

# A warehouse selector: a warehouse ID, or workspace_id/warehouse_id (the same form --cost-profile accepts).
WAREHOUSE_REF_PATTERN = r"^(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+$"


def technique_knobs() -> Dict[str, Dict[str, str]]:
    """Scenario -> {technique name -> knob}, from SCOPE_SCENARIOS."""
    return {scenario: dict(techniques) for scenario, (_, techniques) in SCOPE_SCENARIOS.items()}


def build_scope_schema() -> Dict[str, Any]:
    """JSON Schema (Draft 2020-12) of a ``--scope-file``, generated from SCOPE_SCENARIOS.

    The committed copy is schema/run-scope.schema.json (checked byte-for-byte in tests). Every scenario and technique
    toggle is required and unknown keys are rejected; the loader adds the rules a schema cannot state (an enabled
    scenario needs a technique, at least one scenario is enabled, ``all`` is not a warehouse ID).
    """
    def closed(properties: Dict[str, Any]) -> Dict[str, Any]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    scenarios = {
        scenario: closed({
            "enabled": {"type": "boolean"},
            "techniques": closed({name: {"type": "boolean"} for name in techniques}),
        })
        for scenario, (_, techniques) in SCOPE_SCENARIOS.items()
    }
    schema = closed({
        "scope_format_version": {"const": SCOPE_FORMAT_VERSION},
        "warehouses": {"anyOf": [
            {"const": "all"},
            {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "pattern": WAREHOUSE_REF_PATTERN}},
        ]},
        "scenarios": closed(scenarios),
    })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:databricks-sql-warehouse-optimizer:run-scope:{SCOPE_FORMAT_VERSION}",
        "title": "Publisher run scope",
        "description": "Which scenarios, techniques and warehouses one publisher run recommends (--scope-file).",
        **schema,
    }


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "schema/run-scope.schema.json")
    target.write_text(json.dumps(build_scope_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
