"""Golden cases from docs/proposals/golden-scenarios.md, one folder per case under tests/golden/cases/.

Every case is checked twice: its ``expected/`` output must equal what the publisher produces now (the behavioral
baseline; ``scripts/update_goldens.py`` rewrites it only for an approved decision-affecting change), and a scenario
case (``c<NN>-``/``a<NN>-``) must also pass the assertions in its ``case.yaml`` ``expect`` block:

    expect:
      scenario: C32
      warehouse: <warehouse_id>
      assertions:
      - path: cost_baseline.forward_365d.dbu_cost
        equals: null

Paths address the serialized RecommendationPackage. A collector-level case instead carries raw ``source/snapshots.json``
(system.compute.warehouses rows) and ``now``: the real CONFIG_EPOCHS_SQL runs in sqlite, its rows go
through extract_evidence, and ``evidence_assertions`` address the serialized WarehouseEvidence. Optional
``source/collector_rows.json`` ({query label: rows}) feeds the other collector queries and ``analysis_time`` pins the clock. A segment is a key, ``key[*]`` (every list item) or
``key[field=value]`` (list items, or dict values, whose field equals value). Operators: ``equals``, ``includes`` (list contains
every given value), ``excludes`` (list contains none of them), ``length`` (list length), ``between`` ([low, high],
inclusive, for numbers).

With ``recompute_forecast: true`` in ``expect``, the real Modeler is re-run on each warehouse's ``daily_cost_365``
rows (as the collector does) before the package is built, so a case can exercise the forecast itself. With ``[*]`` or a filter the operator applies
to every matched item, and at least one item must match.

Adding a case: create its folder with ``case.yaml`` and ``source/``, then generate ``expected/`` with
``python scripts/update_goldens.py --case <name> --approved-change "<why>"``.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.common.utils import as_serializable  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.reviewer.package import validate_locked_package_schema  # noqa: E402
from tests.golden import case_dir, case_names, load_case  # noqa: E402
from tests.golden.harness import check, collector_evidence, recompute_forecast  # noqa: E402
from tests.regression import canonical  # noqa: E402


def scenario_cases():
    return [n for n in case_names() if re.match(r"^[ca]\d{2}-", n)]


class GoldenScenarioTests(unittest.TestCase):
    def test_cases_exist_for_harness(self):
        self.assertTrue(scenario_cases(), "no golden scenario cases found")
        for name in scenario_cases():
            payload = load_case(name)
            key = "evidence_assertions" if "snapshots" in payload else "assertions"
            self.assertTrue(payload["expect"].get(key), name)

    def test_collector_golden_scenarios(self):
        for name in scenario_cases():
            payload = load_case(name)
            if "snapshots" not in payload:
                continue
            expect = payload["expect"]
            with self.subTest(scenario=expect.get("scenario"), case=name):
                evidence = {e.warehouse_id: e for e in collector_evidence(p, payload)}
                serialized = as_serializable(evidence[expect["warehouse"]])
                failures = [msg for msg in (check(serialized, a) for a in expect["evidence_assertions"]) if msg]
                self.assertEqual(failures, [], f"{expect.get('scenario')} {name}")

    def test_golden_scenarios(self):
        for name in scenario_cases():
            payload = load_case(name)
            if "snapshots" in payload:
                continue
            expect = payload["expect"]
            with self.subTest(scenario=expect.get("scenario"), case=name):
                evidence = [evidence_from_dict(x) for x in payload["evidence"]]
                if expect.get("recompute_forecast"):
                    for e in evidence:
                        recompute_forecast(p, e)
                packages = {pkg.warehouse.warehouse_id: pkg for pkg in p.build_recommendation_packages(evidence, Thresholds())}
                package = packages[expect["warehouse"]].to_dict()
                validate_locked_package_schema(package)
                failures = [msg for msg in (check(package, a) for a in expect["assertions"]) if msg]
                self.assertEqual(failures, [], f"{expect.get('scenario')} {name}")


class GoldenExpectedOutputTests(unittest.TestCase):
    """Exact behavioral baseline: every case's expected/ output equals what the publisher produces now."""

    def test_every_case_matches_its_expected_output(self):
        for name in case_names():
            with self.subTest(case=name):
                produced = canonical.golden_expected_files(p, name)
                stored = {path.name: path.read_text(encoding="utf-8") for path in sorted((case_dir(name) / "expected").glob("*.json"))}
                self.assertEqual(sorted(stored), sorted(produced), f"{name}: expected/ files")
                for file, text in produced.items():
                    self.assertTrue(stored[file] == text, f"{name}: expected/{file} differs from the publisher output "
                                    "(run scripts/compare_single_file.py to diagnose)")


if __name__ == "__main__":
    unittest.main()
