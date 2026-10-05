"""Replay evidence file format (schema/evidence.schema.json, docs/contracts/evidence-format.md)."""
import copy
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector.demo import demo_evidence, synthetic_demo_evidence  # noqa: E402
from publisher.analyzer.collector.replay import load_evidence_files, save_evidence, validate_evidence_payload  # noqa: E402
from publisher.common.hashing import canonical_json  # noqa: E402
from publisher.common.utils import as_serializable  # noqa: E402
from publisher.config.versions import EVIDENCE_FORMAT_VERSION, PUBLISHER_VERSION  # noqa: E402
from publisher.contracts.source import EpochEvidence, PhotonObservation, QuerySourceProfile, WarehouseAttribution, WarehouseConfig, WarehouseEvidence  # noqa: E402
from tests.golden import case_names, load_case  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402

SCHEMA = json.loads((ROOT / "schema" / "evidence.schema.json").read_text())


def envelope(items):
    return {"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": PUBLISHER_VERSION, "warehouses": items}


class EvidenceFormatTests(unittest.TestCase):
    def setUp(self):
        self.evidence = demo_evidence()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_saved_file_is_a_versioned_envelope_that_round_trips(self):
        path = self.dir / "evidence.json"
        save_evidence(path, self.evidence)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["evidence_format_version"], "1.0.0")
        self.assertEqual(payload["publisher_version"], PUBLISHER_VERSION)
        self.assertEqual(len(payload["warehouses"]), len(self.evidence))
        back = load_evidence_files([str(path)])
        self.assertEqual(canonical_json(back), canonical_json(self.evidence))

    def test_saved_bytes_are_deterministic(self):
        a, b = self.dir / "a.json", self.dir / "b.json"
        save_evidence(a, self.evidence)
        save_evidence(b, demo_evidence())
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_a_bare_list_is_rejected(self):
        path = self.dir / "old.json"
        path.write_text(json.dumps(as_serializable(self.evidence)))
        with self.assertRaises(PublisherError) as ctx:
            load_evidence_files([str(path)])
        self.assertIn("evidence_format_version", str(ctx.exception))

    def test_a_missing_or_other_version_is_rejected(self):
        for version in ("0.9.0", "1.0.1", "2.0.0", None):
            with self.subTest(version=version):
                payload = envelope(as_serializable(self.evidence))
                if version is None:
                    del payload["evidence_format_version"]
                else:
                    payload["evidence_format_version"] = version
                path = self.dir / "v.json"
                path.write_text(json.dumps(payload))
                with self.assertRaises(PublisherError) as ctx:
                    load_evidence_files([str(path)])
                self.assertIn("unsupported evidence_format_version", str(ctx.exception))

    def test_invalid_json_is_rejected_with_the_file_name(self):
        path = self.dir / "broken.json"
        path.write_text("{not json")
        with self.assertRaises(PublisherError) as ctx:
            load_evidence_files([str(path)])
        self.assertIn("broken.json", str(ctx.exception))

    def test_a_missing_forecast_status_is_rejected_on_load(self):
        payload = envelope(as_serializable(self.evidence))
        del payload["warehouses"][0]["forecast_metadata"]["dbu_forecast_status"]
        path = self.dir / "nostatus.json"
        path.write_text(json.dumps(payload))
        with self.assertRaises(PublisherError) as ctx:
            load_evidence_files([str(path)])
        self.assertIn("warehouses/0/forecast_metadata", str(ctx.exception))
        self.assertIn("dbu_forecast_status", str(ctx.exception))

    def test_unknown_fields_are_rejected(self):
        payload = envelope(as_serializable(self.evidence))
        payload["warehouses"][0]["surprise"] = 1
        with self.assertRaises(ValueError):
            validate_evidence_payload(payload)

    def test_error_messages_never_echo_values(self):
        payload = envelope(as_serializable(self.evidence))
        payload["warehouses"][0]["cost_history_days"] = "owner@secret.example"
        with self.assertRaises(ValueError) as ctx:
            validate_evidence_payload(payload)
        self.assertNotIn("owner@secret.example", str(ctx.exception))
        self.assertIn("cost_history_days", str(ctx.exception))

    def test_save_fails_closed_and_writes_nothing_for_invalid_evidence(self):
        bad = copy.deepcopy(self.evidence)
        bad[0].forecast_metadata.pop("dbu_forecast_status")
        path = self.dir / "evidence.json"
        with self.assertRaises(ValueError):
            save_evidence(path, bad)
        self.assertFalse(path.exists())

    def test_every_golden_fixture_item_and_synthetic_fleet_matches_the_schema(self):
        items = []
        for name in case_names():
            items += load_case(name).get("evidence") or []
        self.assertGreaterEqual(len(items), 16)
        validate_evidence_payload(envelope(items))
        validate_evidence_payload(envelope(as_serializable(synthetic_demo_evidence())))

    def test_schema_properties_match_the_dataclasses(self):
        defs = SCHEMA["$defs"]
        pairs = {
            "warehouse": WarehouseEvidence, "epoch": EpochEvidence, "config": WarehouseConfig,
            "photonObservation": PhotonObservation, "attribution": WarehouseAttribution,
        }
        for key, cls in pairs.items():
            with self.subTest(schema=key):
                names = {f.name for f in dataclasses.fields(cls)}
                self.assertEqual(set(defs[key]["properties"]), names)
                self.assertEqual(set(defs[key]["required"]), names)
        query_source = defs["attribution"]["properties"]["query_source"]
        self.assertEqual(set(query_source["properties"]), {f.name for f in dataclasses.fields(QuerySourceProfile)})
        self.assertEqual(set(query_source["required"]), set(query_source["properties"]))


if __name__ == "__main__":
    unittest.main()
