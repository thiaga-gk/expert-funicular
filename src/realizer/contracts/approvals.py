"""Approvals file format 1.0.0: the changes an owner approved, which make a measured change's origin CONFIRMED.

A live measurement infers the origin of a change (INFERRED: applied after the package was generated, to the knob the
package recommended). An approvals file states it: each approval names the recommendation package, the knob and the
value approved, and a reference to the approval record (a change ticket, for example). A measured change that an
approval names is CONFIRMED, with intent ACCEPTED and the reference recorded. No person, email or free text is kept.
The file is checked against ``schema/realization-approvals.schema.json`` (built by ``approvals_schema``) when loaded.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Set

from realizer.common.logging import LOGGER

APPROVALS_FORMAT_VERSION = "1.0.0"


class Approvals(dict):
    """A loaded approvals file. It is the mapping the file holds and also keeps, as the origin of each measured change
    consults it, which packages were consulted and which approvals matched (``warn_unmatched`` reports the rest)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.consulted: Set[str] = set()
        self.matched: Set[int] = set()


def warn_unmatched(approvals: Optional[Mapping[str, Any]]) -> None:
    """Log one warning for each approval that confirmed no change of the run (a run is every package whose origin was
    determined): ``PACKAGE_NOT_IN_RUN`` when its package was not part of the run, ``NO_MATCHING_CHANGE`` when it was and no
    change of the approval's knob to its value was observed. The line names the approval's position in the file, its package
    and its knob, never its value or reference. Nothing is logged for approvals that were not tracked (a plain mapping)."""
    if not isinstance(approvals, Approvals):
        return
    for index, approval in enumerate(approvals.get("approvals") or []):
        if index in approvals.matched:
            continue
        package = str(approval["recommendation_package_id"])
        reason = "NO_MATCHING_CHANGE" if package in approvals.consulted else "PACKAGE_NOT_IN_RUN"
        LOGGER.warning("approvals.unmatched index=%d package=%s knob=%s reason=%s", index, package, approval["knob"], reason)


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
