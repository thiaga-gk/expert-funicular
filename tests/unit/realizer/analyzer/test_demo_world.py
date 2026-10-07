"""The demo's simulated world (src/realizer/analyzer/collector/demo_world.py): what ``realizer --demo`` measures.

A package of a Publisher demo-fleet warehouse is applied in that warehouse's simulated world (its first authority-eligible
step, seven days after the package was generated, at 14:32 UTC) and measured through the live path at 28 settled days
after the change, exactly as the end-to-end goldens do: the world of GE-01's package is GE-01's world. A package of any
other warehouse is refused before anything is simulated.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT, fixture_package
from publisher.contracts import reporter_contract as publisher_contract  # noqa: E402
from realizer.analyzer.collector import demo_world  # noqa: E402
from realizer.common.errors import InputError  # noqa: E402
from realizer.realizer import realize  # noqa: E402

GE01 = ROOT / "tests" / "golden" / "realizer" / "cases" / "ge01-auto-stop-exact-scoped" / "expected"


def rehashed(package):
    """*package* with its id and full hash recomputed, so that it passes the package gate."""
    payload = publisher_contract.package_hash_payload_from_document(package)
    package["recommendation_package_id"] = publisher_contract.content_id("PKG", payload)
    package["provenance"]["package_hash_full_sha256"] = publisher_contract.sha256_hex(payload)
    return package


def demo_run(package, **kwargs):
    """The demo's observation and snapshot of *package*, and the evidence files it wrote."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        observation = demo_world.measure_demo(package, out, **kwargs)
        written = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "realization" / "measurements").glob("*.json"))}
        snapshot = realize(package, observation, out).to_dict()
    return observation, snapshot, written


class FleetMembershipTests(unittest.TestCase):
    def test_a_fleet_warehouse_is_its_position_in_the_fleet(self):
        self.assertEqual(demo_world.fleet_index("wh-demo-013"), 13)
        self.assertEqual(demo_world.fleet_index("wh-demo-013", "2234567890123456"), 13)
        self.assertEqual(demo_world.fleet_index("wh-demo-001", "1234567890123456"), 1)

    def test_other_warehouses_are_refused_without_their_values(self):
        for warehouse in ("wh-prod-013", "wh-demo-000", "wh-demo-999", "wh-demo-13", "wh-demo-0130", "abc123"):
            with self.subTest(warehouse=warehouse), self.assertRaises(InputError) as caught:
                demo_world.fleet_index(warehouse)
            self.assertNotIn(warehouse, str(caught.exception))
            self.assertEqual(len(str(caught.exception).splitlines()), 1)

    def test_a_fleet_warehouse_id_in_another_workspace_is_refused(self):
        with self.assertRaises(InputError) as caught:
            demo_world.fleet_index("wh-demo-013", "1234567890123456")
        self.assertNotIn("1234567890123456", str(caught.exception))

    def test_a_package_of_another_warehouse_is_refused_before_anything_is_simulated(self):
        package = copy.deepcopy(world.package("autostop-scoped-pro"))
        package["warehouse"]["workspace_id"] = "1234567890123456"
        with self.assertRaises(InputError) as caught, tempfile.TemporaryDirectory() as tmp:
            demo_world.measure_demo(rehashed(package), Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertIn("demo-fleet", str(caught.exception))
        self.assertNotIn("wh-demo-013", str(caught.exception))


class TimelineTests(unittest.TestCase):
    def test_the_change_is_a_week_after_generation_and_measured_28_settled_days_later(self):
        change = demo_world.change_at(world.package("autostop-scoped-pro"))
        self.assertEqual(change.isoformat(), "2026-10-08T14:32:00+00:00")
        self.assertEqual(demo_world.settled_as_of(change), world.AS_OF)
        self.assertEqual(demo_world.change_at(world.package("autostop-daily-oct02")).isoformat(), "2026-10-09T14:32:00+00:00")

    def test_the_applied_change_is_the_first_authority_eligible_step(self):
        self.assertEqual(demo_world.applied_change(world.package("autostop-scoped-pro")), {"auto_stop": 10})
        self.assertEqual(demo_world.applied_change(fixture_package()), {"size": "MEDIUM"})
        self.assertEqual(demo_world.applied_change(fixture_package(), modified=True), {"size": "SMALL"})
        self.assertEqual(demo_world.applied_change(world.package("autostop-scoped-pro"), modified=True), {"auto_stop": 10})
        self.assertIsNone(demo_world.applied_change(world.package("scope-migration-only")))


class SimulatedWorldTests(unittest.TestCase):
    def test_an_auto_stop_package_is_applied_exactly_and_measured(self):
        observation, snapshot, written = demo_run(world.package("autostop-scoped-pro"))
        self.assertEqual(snapshot["application"]["implementation_conformance"], "EXACT")
        self.assertEqual(observation["applied_at_utc"], "2026-10-08T14:32:00Z")
        self.assertEqual(observation["config"]["auto_stop_minutes"], 10)
        self.assertGreater(float(observation["economics"]["dbu_realized"]), 0)
        self.assertEqual(snapshot["current_state"]["realization_state"], "REALIZED")
        self.assertEqual(list(written), ["2234567890123456__wh-demo-013__20261107T120000Z.json"])
        evidence = written["2234567890123456__wh-demo-013__20261107T120000Z.json"]
        self.assertEqual(evidence["source"], "DEMO_SIMULATION")

    def test_the_demo_world_is_the_end_to_end_golden_twin(self):
        package = json.loads((GE01 / "publisher" / "package.json").read_text(encoding="utf-8"))
        stored = json.loads((GE01 / "realizer" / "measurement.json").read_text(encoding="utf-8"))
        self.assertEqual(demo_world.simulated_evidence(package), stored)
        observation, _, _ = demo_run(package)
        self.assertEqual(observation, json.loads((GE01 / "realizer" / "observation.json").read_text(encoding="utf-8")))

    def test_a_size_package_is_applied_and_observed(self):
        observation, snapshot, _ = demo_run(fixture_package())
        self.assertEqual(observation["config"]["warehouse_size"], "MEDIUM")
        self.assertEqual(snapshot["application"]["implementation_conformance"], "EXACT")
        self.assertEqual(observation["measurement"]["knob"], "warehouse_size")
        self.assertEqual(observation["measurement"]["not_measured_reason"], "KNOB_NOT_REPLAYED")
        self.assertIsNone(observation["economics"])

    def test_a_modified_size_is_applied_as_small(self):
        observation, snapshot, _ = demo_run(fixture_package(), modified=True)
        self.assertEqual(observation["config"]["warehouse_size"], "SMALL")
        self.assertEqual(snapshot["application"]["implementation_conformance"], "MODIFIED")

    def test_a_package_without_an_eligible_step_leaves_the_world_unchanged(self):
        observation, snapshot, written = demo_run(world.package("scope-migration-only"))
        self.assertIsNone(observation["applied_at_utc"])
        self.assertEqual(snapshot["application"]["implementation_conformance"], "NOT_APPLIED")
        self.assertEqual(len(written), 1)

    def test_as_of_is_the_measurement_time(self):
        observation, _, written = demo_run(world.package("autostop-scoped-pro"), as_of="2026-10-20T12:00:00Z")
        self.assertEqual(list(written), ["2234567890123456__wh-demo-013__20261020T120000Z.json"])
        self.assertEqual(observation["observed_at_utc"], "2026-10-20T12:00:00Z")
        # Ten settled days after the change: the window is the last whole week of them.
        window = observation["measurement"]["post_window"]
        self.assertEqual((window["first_day"], window["last_day"]), ("2026-10-12", "2026-10-18"))

    def test_before_the_change_time_nothing_is_applied(self):
        observation, snapshot, _ = demo_run(world.package("autostop-scoped-pro"), as_of="2026-10-08T12:00:00Z")
        self.assertEqual(observation["config"]["auto_stop_minutes"], 30)
        self.assertEqual(snapshot["application"]["implementation_conformance"], "NOT_APPLIED")

    def test_an_unreadable_as_of_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(InputError):
            demo_world.measure_demo(world.package("autostop-scoped-pro"), Path(tmp), as_of="yesterday")


if __name__ == "__main__":
    unittest.main()
