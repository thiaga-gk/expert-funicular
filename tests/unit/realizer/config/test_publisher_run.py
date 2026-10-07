"""Reading a Publisher run for a fleet run (``load_publisher_run``, ``select_warehouses`` in src/realizer/config/loader.py).

A run folder is read as named; a root holding ``run_date=*/run_ts=*`` folders gives its newest run. The manifest's
package entries become the run's entries in (workspace, warehouse) order; anything the Realizer cannot read as a
Publisher run is refused (``InputError``) before a fleet run starts.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tests.unit.realizer.helpers import fixture_package  # noqa: F401  (src/realizer on the path)
from tests.unit.realizer import fleet_support
from tests import realizer_world as world
from realizer.common.errors import InputError
from realizer.config.loader import load_publisher_run, select_warehouses


class PublisherRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.packages = [world.package("autostop-scoped-pro"), world.package("autostop-scoped-serverless"), fixture_package()]

    def test_a_root_gives_its_newest_run_and_a_run_folder_itself(self):
        older = fleet_support.run_folder(self.root, self.packages[:1], "run_date=2026-10-06/run_ts=20261006T120000Z")
        newer = fleet_support.run_folder(self.root, self.packages, "run_date=2026-10-07/run_ts=20261007T120000Z")
        run = load_publisher_run(self.root)
        self.assertEqual((run.run_dir, run.package_count), (newer, 3))
        self.assertEqual([(e.workspace_id, e.warehouse_id) for e in run.entries],
                         sorted((p["warehouse"]["workspace_id"], p["warehouse"]["warehouse_id"]) for p in self.packages))
        self.assertEqual(load_publisher_run(older).entries[0].recommendation_package_id, self.packages[0]["recommendation_package_id"])

    def test_a_root_skips_the_realizers_own_run_folders(self):
        # The Realizer's fleet run writes run_date=*/run_ts=* folders too; under a root shared with the Publisher, its
        # newest folder is a Realizer run (a run manifest, no packages list) and is never read as a Publisher run.
        publisher = fleet_support.run_folder(self.root, self.packages, "run_date=2026-10-07/run_ts=20261007T120000Z")
        realizer_run = self.root / "run_date=2026-10-07" / "run_ts=20261007T130000Z"
        realizer_run.mkdir(parents=True)
        (realizer_run / "manifest.json").write_text(json.dumps({"run_manifest_version": "1.0.0", "warehouses": []}), encoding="utf-8")
        self.assertEqual(load_publisher_run(self.root).run_dir, publisher)
        with self.assertRaisesRegex(InputError, r"is a Realizer run, not a Publisher run$"):
            load_publisher_run(realizer_run)

    def test_a_root_with_only_realizer_runs_is_refused(self):
        realizer_run = self.root / "run_date=2026-10-07" / "run_ts=20261007T130000Z"
        realizer_run.mkdir(parents=True)
        (realizer_run / "manifest.json").write_text(json.dumps({"run_manifest_version": "1.0.0", "warehouses": []}), encoding="utf-8")
        with self.assertRaisesRegex(InputError, r"^no Publisher run"):
            load_publisher_run(self.root)

    def test_the_real_publisher_demo_run(self):
        run = load_publisher_run(fleet_support.publisher_demo_root())
        self.assertEqual([e.warehouse_id for e in run.entries], list(fleet_support.DEMO_WAREHOUSES))
        self.assertTrue(all((run.run_dir / e.path).is_file() for e in run.entries))

    def test_what_is_not_a_publisher_run_is_refused(self):
        with self.assertRaisesRegex(InputError, r"^no Publisher run"):
            load_publisher_run(self.root / "missing")
        with self.assertRaisesRegex(InputError, r"^no Publisher run"):
            load_publisher_run(self.root)
        folder = fleet_support.run_folder(self.root, self.packages)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        for label, document, pattern in (
            ("not JSON", "{", r"^Publisher run manifest file is not valid JSON"),
            ("no packages", {"packages": None}, r"has no packages list$"),
            ("missing field", {"packages": [{"path": "p.json"}]}, r"^Publisher run manifest entry 0 needs path, workspace_id"),
            ("outside", {"packages": [{**manifest["packages"][0], "path": "../x.json"}]}, r"names a package outside the run folder$"),
            ("twice", {"packages": [manifest["packages"][0], manifest["packages"][0]]}, r"two packages of one warehouse$"),
        ):
            with self.subTest(case=label):
                (folder / "manifest.json").write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(InputError, pattern):
                    load_publisher_run(folder)

    def test_select_warehouses(self):
        run = load_publisher_run(fleet_support.run_folder(self.root, self.packages))
        self.assertIs(select_warehouses(run, []), run)
        pro = self.packages[0]["warehouse"]
        chosen = select_warehouses(run, [pro["warehouse_id"], f"{self.packages[2]['warehouse']['workspace_id']}/{self.packages[2]['warehouse']['warehouse_id']}"])
        self.assertEqual({e.warehouse_id for e in chosen.entries}, {pro["warehouse_id"], self.packages[2]["warehouse"]["warehouse_id"]})
        self.assertEqual(chosen.package_count, 3)
        for name in ("wh-nope", f"999/{pro['warehouse_id']}"):
            with self.subTest(name=name), self.assertRaisesRegex(InputError, rf"^--warehouse {name} is not a warehouse of the Publisher run$"):
                select_warehouses(run, [name])


if __name__ == "__main__":
    unittest.main()
