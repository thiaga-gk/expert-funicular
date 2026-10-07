"""Realizer Contract v1.0.0 (src/realizer/contracts/realization.py, analysis.py, reconciliation.py).

Every public name, enum value, constant, canonical helper result, validation message and ID is the one stored in
``tests/fixtures/realizer/expectations/`` (generated from the single-file release), and the ledger entries and packages
stored in the compatibility baseline round-trip byte for byte through the dataclasses.
"""
import dataclasses
import datetime as dt
import json
import sys
import unittest
from decimal import Decimal
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
from tests import realizer_expect as expect  # noqa: E402
from tests.regression.realizer import canonical  # noqa: E402
from tests.unit.realizer.helpers import outcome  # noqa: E402

canonical.modular()  # src/realizer, never a shadowing copy
from realizer.analyzer.analysis import observed_config  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.common.utils import utc_iso  # noqa: E402
from realizer.contracts import analysis, reconciliation, realization as rc  # noqa: E402
from realizer.estimator.economics import make_cf_id  # noqa: E402
from realizer.governor.policy import counterfactual_eligible  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402


def _error(fn, *args, **kwargs):
    """(exception type name, message) raised by *fn*, or None."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - compared, not handled
        return type(exc).__name__, str(exc)
    return None


def _ledger_lines():
    """Every (scenario, line) of every stored RLE ledger."""
    for name in canonical.scenario_names():
        for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    yield name, line


def _package_files():
    """Every stored RPKG and RSNAP file."""
    for name in canonical.scenario_names():
        base = canonical.EXPECTED / name / "realization"
        yield from sorted(base.glob("packages/*.json"))
        yield from sorted(base.glob("snapshots/*.json"))


class PublicSurfaceTests(unittest.TestCase):
    def test_every_public_name_survives(self):
        for name in rc.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(rc, name))
        added = {"make_rpkg_id", "make_evidence_id", "make_app_id", "make_val_id", "make_measurement_id"}
        self.assertLessEqual(added, set(rc.__all__))
        # the single-file release's public names plus the ID builders the contract module gained
        expect.check(self, "names", sorted(rc.__all__), spot={"count": len(rc.__all__), "added": sorted(added)})

    def test_constants_are_identical(self):
        expect.check(self, "constants", {name: getattr(rc, name) for name in ("CONTRACT_VERSION", "LEDGER_CONTRACT_VERSION", "HASH_HEX_LENGTH")})

    def test_enums_have_identical_members_and_values(self):
        enums = [n for n in rc.__all__ if isinstance(getattr(rc, n), type) and issubclass(getattr(rc, n), Enum)]
        self.assertEqual(len(enums), 17)  # the single-file release's 16 plus StepRole
        for name in enums:
            self.assertTrue(issubclass(getattr(rc, name), str), name)
        expect.check(self, "members", {name: [(m.name, m.value) for m in getattr(rc, name)] for name in enums},
                     spot={"enums": enums, "members": sum(len(list(getattr(rc, n))) for n in enums)})

    def test_grouped_enums_are_reexported_not_copied(self):
        grouped = {**{n: analysis for n in analysis.__all__}, **{n: reconciliation for n in reconciliation.__all__}}
        self.assertEqual(set(analysis.__all__), {"IntentState", "OriginAttribution", "EvidenceQuality"})
        self.assertEqual(set(reconciliation.__all__), {"SourcePackageDisposition", "ImplementationConformance",
                                                       "ApplicationState", "PlanProgressState", "RegenerationStatus", "StepRole"})
        for name, module in grouped.items():
            self.assertIs(getattr(rc, name), getattr(module, name), name)


class CanonicalHelperTests(unittest.TestCase):
    VALUES = [
        Decimal("1.00"), Decimal("0.000"), Decimal("-10.5000"), Decimal("1E-7"), Decimal("123456789.123456789"),
        Decimal("1E+3"), "2.50", 5, 0.1, -0,
    ]

    def test_canonical_decimal(self):
        for value in self.VALUES:
            with self.subTest(value=value):
                expect.check(self, f"value {value!r}", outcome(rc.canonical_decimal, value))
        for bad in (Decimal("NaN"), Decimal("Infinity"), "abc"):
            with self.subTest(bad=bad):
                result = outcome(rc.canonical_decimal, bad)
                self.assertNotEqual(result[0], "ok")
                expect.check(self, f"bad {bad!r}", result)

    def test_canonical_json_hash_and_content_id(self):
        @dataclasses.dataclass
        class Row:
            a: Decimal
            when: dt.datetime

        payloads = [
            {"b": 2, "a": [1, (2, 3)], "c": {"z": None, "y": "é"}},
            {"amount": Decimal("10.500"), "naive": dt.datetime(2026, 10, 1, 12), "enum": rc.EventType.DRIFT_DETECTED},
            {"aware": dt.datetime(2026, 10, 1, 14, tzinfo=dt.timezone(dt.timedelta(hours=2))), "row": Row(Decimal("3"), dt.datetime(2026, 1, 1))},
            [],
            "text",
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(payload=repr(payload)[:60]):
                expect.check(self, f"payload {index}", [rc.canonical_json(payload), rc.sha256_hex(payload), rc.content_id("RLE", payload)])


class DataclassRoundTripTests(unittest.TestCase):
    def test_every_stored_ledger_entry_round_trips_byte_for_byte(self):
        seen = set()
        count = 0
        for name, line in _ledger_lines():
            raw = json.loads(line)
            seen.add(raw["event_type"])
            entry = rc.RealizationLedgerEntry(**{**raw, "event_type": rc.EventType(raw["event_type"])})
            entry.validate()
            self.assertEqual(rc.canonical_json(entry.to_dict()), line, name)
            count += 1
        self.assertGreater(count, 300)
        self.assertGreaterEqual(len(seen), 15, sorted(seen))

    def test_every_stored_package_and_snapshot_round_trips_byte_for_byte(self):
        count = 0
        for path in _package_files():
            text = path.read_text(encoding="utf-8")
            doc = json.loads(text)
            package = rc.RealizationPackage(**doc)
            package.validate()
            self.assertEqual(json.dumps(package.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n", text, path)
            count += 1
        self.assertGreater(count, 41)

    def test_dataclasses_are_frozen_with_the_single_file_fields(self):
        for name in ("RealizationLedgerEntry", "RealizationPackage"):
            new = getattr(rc, name)
            expect.check(self, name, [(f.name, str(f.type)) for f in dataclasses.fields(new)])
            self.assertTrue(new.__dataclass_params__.frozen)


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        name, line = next(_ledger_lines())
        cls.raw_entry = json.loads(line)
        cls.raw_package = json.loads(next(_package_files()).read_text(encoding="utf-8"))

    def _entry_error(self, **changes):
        raw = {**self.raw_entry, **changes}
        return _error(rc.RealizationLedgerEntry(**{**raw, "event_type": rc.EventType(raw["event_type"])}).validate)

    def _package_error(self, **changes):
        raw = {**self.raw_package, **changes}
        return _error(rc.RealizationPackage(**raw).validate)

    def test_ledger_entry_validation_messages(self):
        hex24 = "A" * 24
        cases = [
            {}, {"ledger_entry_id": None}, {"ledger_entry_id": "RLE-" + hex24.lower()}, {"ledger_entry_id": "RLE-ABC"},
            {"ledger_entry_id": "APP-" + hex24}, {"realization_package_id": "RPKG-" + "G" * 24},
            {"recommendation_package_id": "XPKG-1"}, {"ledger_contract_version": "2.0.0"},
            {"effective_at_utc": "yesterday"}, {"recorded_at_utc": "2026-13-01T00:00:00Z"},
            {"application_episode_id": "APP-1"}, {"validation_episode_id": "VAL-" + hex24}, {"measurement_id": "MEAS-x"},
            {"caused_by_entry_id": "RLE-" + hex24}, {"corrects_entry_id": "MEAS-" + hex24},
        ]
        failing = 0
        for change in cases:
            with self.subTest(change=change):
                new = self._entry_error(**change)
                expect.check(self, json.dumps(change, sort_keys=True), new)
                failing += new is not None
        self.assertEqual(failing, len(cases) - 3)

    def test_package_validation_messages(self):
        cases = [
            {}, {"contract_version": "1.0.0"}, {"realization_package_id": "RPKG-1"}, {"snapshot_id": "RSNAP-" + "a" * 24},
            {"source_recommendation": {}}, {"warehouse": {"workspace_id": "1"}}, {"warehouse": {"warehouse_id": "w"}},
            {"as_of_utc": "not-a-time"}, {"history": {"ledger_checkpoint_hash": "MD5:x"}}, {"history": {}},
        ]
        for change in cases:
            with self.subTest(change=change):
                new = self._package_error(**change)
                expect.check(self, json.dumps(change, sort_keys=True), new)
                self.assertEqual(new is None, change == {})


class IdBuilderTests(unittest.TestCase):
    """IDs built from the frozen PKG fixture and its demo observation equal the stored values and the stored outputs'."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = canonical.load_scenario("example-exact")["package_doc"]
        cls.observations = [
            demo_observation(cls.pkg),
            demo_observation(cls.pkg, modified=True),
            {**demo_observation(cls.pkg), "window": {"start_utc": "2026-10-02T00:00:00Z"}, "source": "S",
             "correction": {"version": 2}, "performance": {"reliability_pass": False}},
            {"observed_at_utc": "2026-10-09T12:00:00Z"},
        ]
        cls.policies = [RealizerPolicy(), RealizerPolicy.from_mapping({"minimum_days": 3})]

    def test_rpkg_id_and_stored_output(self):
        pkg_id = self.pkg["recommendation_package_id"]
        self.assertEqual(rc.make_rpkg_id(pkg_id), "RPKG-976E6C1C6E88CE76321758ED")

    def test_evidence_id_and_stored_output(self):
        ids = []
        for obs in self.observations:
            payload = {"config": observed_config(obs, self.pkg), "observed_at_utc": obs["observed_at_utc"]}
            ids.append(rc.make_evidence_id("OBSERVED_CONFIGURATION", self.pkg, obs, payload))
        expect.check(self, "ids", ids)
        obs = canonical.load_scenario("example-exact")["runs"][0]["observation"]
        observed_at = utc_iso(obs["observed_at_utc"])
        payload = {"config": observed_config(obs, self.pkg), "observed_at_utc": observed_at,
                   "source": obs.get("source", "NORMALIZED_OBSERVATION")}
        revid = rc.make_evidence_id("OBSERVED_CONFIGURATION", self.pkg, obs, payload)
        self.assertEqual(revid, "REVID-37F7F275591908182339F44B")
        self.assertTrue((canonical.EXPECTED / "example-exact" / "realization" / "evidence" / f"{revid}.json").is_file())

    def test_episode_counterfactual_and_measurement_ids(self):
        rpkg = rc.make_rpkg_id(self.pkg["recommendation_package_id"])
        for i, obs in enumerate(self.observations):
            ids = {}
            for policy in self.policies:
                app = rc.make_app_id(rpkg, "SCN-X", i, "SHA256:A", "SHA256:B", "REVID-" + "C" * 24)
                cf = make_cf_id(app, obs, self.pkg, policy, counterfactual_eligible(obs)[1])
                ids[policy.snapshot_id] = {"app": app, "val": rc.make_val_id(app, obs, policy), "cf": cf,
                                           "measurements": [rc.make_measurement_id(app, obs, cf, basis) for basis in ("ECONOMIC_COST", "LIST_PRICE")]}
            expect.check(self, f"observation {i}", ids)

    def test_stored_ledgers_carry_ids_the_builders_reproduce(self):
        """Every APP / VAL / MEAS id in the example-exact ledger is one the moved builders produce from its inputs."""
        obs = canonical.load_scenario("example-exact")["runs"][0]["observation"]
        lines = [json.loads(line) for name, line in _ledger_lines() if name == "example-exact"]
        app_ids = {e["application_episode_id"] for e in lines if e["application_episode_id"]}
        val_ids = {e["validation_episode_id"] for e in lines if e["validation_episode_id"]}
        meas = [e for e in lines if e["event_type"] == "ECONOMIC_MEASUREMENT_RECORDED"]
        self.assertEqual(len(app_ids), 1)
        app = app_ids.pop()
        policy = RealizerPolicy()
        self.assertEqual(val_ids, {rc.make_val_id(app, obs, policy)})
        method = next(e["payload"]["method"] for e in lines if e["event_type"] == "COUNTERFACTUAL_RECORDED")
        cf = make_cf_id(app, obs, self.pkg, policy, method)
        self.assertEqual({e["payload"]["counterfactual_id"] for e in lines if e["event_type"] == "COUNTERFACTUAL_RECORDED"}, {cf})
        self.assertEqual([e["measurement_id"] for e in meas],
                         [rc.make_measurement_id(app, obs, cf, e["payload"]["economics"]["economic_basis"]) for e in meas])


if __name__ == "__main__":
    unittest.main()
