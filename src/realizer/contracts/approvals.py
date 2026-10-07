"""Approvals file format 1.0.0: the changes an owner approved, which make a measured change's origin CONFIRMED.

A live measurement infers the origin of a change (INFERRED: applied after the package was generated, to the knob the
package recommended). An approvals file states it: each approval names the recommendation package, the knob and the
value approved, and a reference to the approval record (a change ticket, for example). A measured change that an
approval names is CONFIRMED, with intent ACCEPTED and the reference recorded. No person, email or free text is kept.
The file is checked against ``schema/realization-approvals.schema.json`` (built by ``approvals_schema``) when loaded.
"""
from __future__ import annotations

from typing import Any, Dict

APPROVALS_FORMAT_VERSION = "1.0.0"


def approvals_schema() -> Dict[str, Any]:
    """The Draft 2020-12 JSON Schema of approvals format 1.0.0."""
    approval = {
        "type": "object", "additionalProperties": False,
        "required": ["knob", "recommendation_package_id", "reference", "value"],
        "properties": {
            "recommendation_package_id": {"type": "string", "pattern": r"^PKG-[0-9A-F]{24}$"},
            "knob": {"enum": ["warehouse_type", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes"]},
            "value": {"type": ["integer", "string"]},
            "reference": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"^[A-Za-z0-9_.:/#-]+$"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RealizationApprovals",
        "type": "object",
        "additionalProperties": False,
        "required": ["approvals", "approvals_format_version"],
        "properties": {"approvals_format_version": {"const": APPROVALS_FORMAT_VERSION}, "approvals": {"type": "array", "items": approval}},
    }
