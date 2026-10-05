"""Golden case harness: the assertion DSL and the two ways a case is run (documented in test_golden_publisher.py)."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_SEGMENT = re.compile(r"^(?P<key>[A-Za-z0-9_]+)(?:\[(?P<sel>\*|[A-Za-z0-9_]+=[^\]]*)\])?$")


def resolve(node, path):
    """Return the list of values the path addresses (one value unless a list selector fans out)."""
    values = [node]
    for raw in path.split("."):
        m = _SEGMENT.match(raw)
        if not m:
            raise ValueError(f"Bad path segment {raw!r} in {path!r}")
        nxt = []
        for v in values:
            child = v[m.group("key")]
            sel = m.group("sel")
            items = list(child.values()) if isinstance(child, dict) else child  # selectors also walk dict values (e.g. evidence by id)
            if sel is None:
                nxt.append(child)
            elif sel == "*":
                nxt.extend(items)
            else:
                field, want = sel.split("=", 1)
                nxt.extend(x for x in items if str(x.get(field)) == want)
        values = nxt
    return values


def check(package, assertion) -> Optional[str]:
    path = assertion["path"]
    values = resolve(package, path)
    if not values:
        return f"{path}: no value matched"
    for value in values:
        if "equals" in assertion and value != assertion["equals"]:
            return f"{path}: expected {assertion['equals']!r}, got {value!r}"
        if "includes" in assertion and not set(assertion["includes"]) <= set(value or []):
            return f"{path}: expected to include {assertion['includes']!r}, got {value!r}"
        if "excludes" in assertion and set(assertion["excludes"]) & set(value or []):
            return f"{path}: expected to exclude {assertion['excludes']!r}, got {value!r}"
        if "length" in assertion and len(value) != assertion["length"]:
            return f"{path}: expected length {assertion['length']}, got {len(value)}"
        if "between" in assertion:
            low, high = assertion["between"]
            if not isinstance(value, (int, float)) or not low <= value <= high:
                return f"{path}: expected between {low} and {high}, got {value!r}"
    return None


def recompute_forecast(p, e) -> None:
    """Mirror extract_evidence: forecast fields come from Modeler.forecast_from_daily_cost over the daily rows."""
    model = p.Modeler(p.Thresholds()).forecast_from_daily_cost(
        e.daily_cost_365, e.current_epoch.epoch_start, e.analysis_time,
        billing_watermark=e.forecast_metadata.get("billing_watermark"),
    )
    e.current_cost_365 = model["current_cost_365"]
    e.baseline_forecast_cost_365 = model["forecast_cost_365"]
    e.currency = model["currency"]
    e.cost_history_days = model["cost_history_days"]
    e.cost_cv_84 = model["cost_cv_84"]
    e.forecast_metadata = {k: v for k, v in model.items() if k not in {"current_cost_365", "forecast_cost_365", "currency"}}


def collector_evidence(p, case: Dict[str, Any]) -> List[Any]:
    """Run the real CONFIG_EPOCHS_SQL over the case's snapshots in sqlite and extract evidence from its rows."""
    from tests.unit.analyzer.test_config_epochs_sql import run_sql

    epoch_rows = run_sql(p.CONFIG_EPOCHS_SQL, case["snapshots"], case["now"])
    other_rows = case.get("collector_rows", {})

    class Client:
        def query(self, statement, *, label="collector_sql"):
            return epoch_rows if label == "config_epochs" else other_rows.get(label, [])

    return list(p.extract_evidence(Client(), p.Thresholds(), "us-east-1", analysis_time=case.get("analysis_time")))
