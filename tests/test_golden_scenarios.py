"""Golden scenarios from docs/proposals/golden-scenarios.md (fixtures tests/golden/c*.json and a*.json).

Each fixture holds normalized evidence plus an ``expect`` block:

    "expect": {
      "scenario": "C32",
      "warehouse": "<warehouse_id>",
      "assertions": [{"path": "cost_baseline.forward_365d.dbu_cost", "equals": null}, ...]
    }

Paths address the serialized RecommendationPackage. A collector-level fixture instead carries raw
``snapshots`` (system.compute.warehouses rows) and ``now``: the real CONFIG_EPOCHS_SQL runs in sqlite, its rows go
through extract_evidence, and ``evidence_assertions`` address the serialized WarehouseEvidence. Optional
``collector_rows`` ({query label: rows}) feed the other collector queries and ``analysis_time`` pins the clock. A segment is a key, ``key[*]`` (every list item) or
``key[field=value]`` (list items, or dict values, whose field equals value). Operators: ``equals``, ``includes`` (list contains
every given value), ``excludes`` (list contains none of them), ``length`` (list length).

With ``"recompute_forecast": true`` in ``expect``, the real Modeler is re-run on each warehouse's ``daily_cost_365``
rows (as the collector does) before the package is built, so a fixture can exercise the forecast itself. With ``[*]`` or a filter the operator applies
to every matched item, and at least one item must match.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

GOLDEN = ROOT / "tests" / "golden"
_SEGMENT = re.compile(r"^(?P<key>[A-Za-z0-9_]+)(?:\[(?P<sel>\*|[A-Za-z0-9_]+=[^\]]*)\])?$")


def scenario_fixtures():
    return sorted(x for x in GOLDEN.glob("*.json") if re.match(r"^[ca]\d{2}-", x.name))


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


def recompute_forecast(e):
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


def check(package, assertion):
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
    return None


class GoldenScenarioTests(unittest.TestCase):
    def test_fixtures_exist_for_harness(self):
        self.assertTrue(scenario_fixtures(), "no golden scenario fixtures found")
        for fixture in scenario_fixtures():
            payload = json.loads(fixture.read_text())
            key = "evidence_assertions" if "snapshots" in payload else "assertions"
            self.assertTrue(payload["expect"].get(key), fixture.name)

    def test_collector_golden_scenarios(self):
        from tests.test_config_epochs_sql import run_sql

        for fixture in scenario_fixtures():
            payload = json.loads(fixture.read_text())
            if "snapshots" not in payload:
                continue
            expect = payload["expect"]
            with self.subTest(scenario=expect.get("scenario"), fixture=fixture.name):
                epoch_rows = run_sql(p.CONFIG_EPOCHS_SQL, payload["snapshots"], payload["now"])

                other_rows = payload.get("collector_rows", {})

                class Client:
                    def query(self, statement, *, label="collector_sql"):
                        return epoch_rows if label == "config_epochs" else other_rows.get(label, [])

                evidence = {e.warehouse_id: e for e in p.extract_evidence(
                    Client(), p.Thresholds(), "us-east-1", analysis_time=payload.get("analysis_time"))}
                serialized = p.as_serializable(evidence[expect["warehouse"]])
                failures = [msg for msg in (check(serialized, a) for a in expect["evidence_assertions"]) if msg]
                self.assertEqual(failures, [], f"{expect.get('scenario')} {fixture.name}")

    def test_golden_scenarios(self):
        for fixture in scenario_fixtures():
            payload = json.loads(fixture.read_text())
            if "snapshots" in payload:
                continue
            expect = payload["expect"]
            with self.subTest(scenario=expect.get("scenario"), fixture=fixture.name):
                evidence = [p.evidence_from_dict(x) for x in payload["evidence"]]
                if expect.get("recompute_forecast"):
                    for e in evidence:
                        recompute_forecast(e)
                packages = {pkg.warehouse.warehouse_id: pkg for pkg in p.build_recommendation_packages(evidence, p.Thresholds())}
                package = packages[expect["warehouse"]].to_dict()
                p.validate_locked_package_schema(package)
                failures = [msg for msg in (check(package, a) for a in expect["assertions"]) if msg]
                self.assertEqual(failures, [], f"{expect.get('scenario')} {fixture.name}")


if __name__ == "__main__":
    unittest.main()
