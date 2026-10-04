"""Golden scenarios from docs/proposals/golden-scenarios.md (fixtures tests/golden/c*.json and a*.json).

Each fixture holds normalized evidence plus an ``expect`` block:

    "expect": {
      "scenario": "C32",
      "warehouse": "<warehouse_id>",
      "assertions": [{"path": "cost_baseline.forward_365d.dbu_cost", "equals": null}, ...]
    }

Paths address the serialized RecommendationPackage. A segment is a key, ``key[*]`` (every list item) or
``key[field=value]`` (list items whose field equals value). Operators: ``equals``, ``includes`` (list contains
every given value), ``excludes`` (list contains none of them). With ``[*]`` or a filter the operator applies
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
            if sel is None:
                nxt.append(child)
            elif sel == "*":
                nxt.extend(child)
            else:
                field, want = sel.split("=", 1)
                nxt.extend(x for x in child if str(x.get(field)) == want)
        values = nxt
    return values


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
    return None


class GoldenScenarioTests(unittest.TestCase):
    def test_fixtures_exist_for_harness(self):
        self.assertTrue(scenario_fixtures(), "no golden scenario fixtures found")
        for fixture in scenario_fixtures():
            payload = json.loads(fixture.read_text())
            self.assertIn("assertions", payload["expect"], fixture.name)

    def test_golden_scenarios(self):
        for fixture in scenario_fixtures():
            payload = json.loads(fixture.read_text())
            expect = payload["expect"]
            with self.subTest(scenario=expect.get("scenario"), fixture=fixture.name):
                evidence = [p.evidence_from_dict(x) for x in payload["evidence"]]
                packages = {pkg.warehouse.warehouse_id: pkg for pkg in p.build_recommendation_packages(evidence, p.Thresholds())}
                package = packages[expect["warehouse"]].to_dict()
                p.validate_locked_package_schema(package)
                failures = [msg for msg in (check(package, a) for a in expect["assertions"]) if msg]
                self.assertEqual(failures, [], f"{expect.get('scenario')} {fixture.name}")


if __name__ == "__main__":
    unittest.main()
