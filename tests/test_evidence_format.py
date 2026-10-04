"""Replay evidence file format (schema/evidence.schema.json, docs/contracts/evidence-format.md)."""
import copy
import dataclasses
import glob
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

SCHEMA = json.loads((ROOT / "schema" / "evidence.schema.json").read_text())


def envelope(items):
    return {"evidence_format_version": p.EVIDENCE_FORMAT_VERSION, "publisher_version": p.PUBLISHER_VERSION, "warehouses": items}


class EvidenceFormatTests(unittest.TestCase):
    def setUp(self):
        self.evidence = p.demo_evidence()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_saved_file_is_a_versioned_envelope_that_round_trips(self):
        path = self.dir / "evidence.json"
        p.save_evidence(path, self.evidence)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["evidence_format_version"], "1.0.0")
        self.assertEqual(payload["publisher_version"], p.PUBLISHER_VERSION)
        self.assertEqual(len(payload["warehouses"]), len(self.evidence))
        back = p.load_evidence_files([str(path)])
        self.assertEqual(p.canonical_json(back), p.canonical_json(self.evidence))

    def test_saved_bytes_are_deterministic(self):
        a, b = self.dir / "a.json", self.dir / "b.json"
        p.save_evidence(a, self.evidence)
        p.save_evidence(b, p.demo_evidence())
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_a_bare_list_is_rejected(self):
        path = self.dir / "old.json"
        path.write_text(json.dumps(p.as_serializable(self.evidence)))
        with self.assertRaises(SystemExit) as ctx:
            p.load_evidence_files([str(path)])
        self.assertIn("evidence_format_version", str(ctx.exception))

    def test_a_missing_or_other_version_is_rejected(self):
        for version in ("0.9.0", "1.0.1", "2.0.0", None):
            with self.subTest(version=version):
                payload = envelope(p.as_serializable(self.evidence))
                if version is None:
                    del payload["evidence_format_version"]
                else:
                    payload["evidence_format_version"] = version
                path = self.dir / "v.json"
                path.write_text(json.dumps(payload))
                with self.assertRaises(SystemExit) as ctx:
                    p.load_evidence_files([str(path)])
                self.assertIn("unsupported evidence_format_version", str(ctx.exception))

    def test_invalid_json_is_rejected_with_the_file_name(self):
        path = self.dir / "broken.json"
        path.write_text("{not json")
        with self.assertRaises(SystemExit) as ctx:
            p.load_evidence_files([str(path)])
        self.assertIn("broken.json", str(ctx.exception))

    def test_a_missing_forecast_status_is_rejected_on_load(self):
        payload = envelope(p.as_serializable(self.evidence))
        del payload["warehouses"][0]["forecast_metadata"]["dbu_forecast_status"]
        path = self.dir / "nostatus.json"
        path.write_text(json.dumps(payload))
        with self.assertRaises(SystemExit) as ctx:
            p.load_evidence_files([str(path)])
        self.assertIn("warehouses/0/forecast_metadata", str(ctx.exception))
        self.assertIn("dbu_forecast_status", str(ctx.exception))

    def test_unknown_fields_are_rejected(self):
        payload = envelope(p.as_serializable(self.evidence))
        payload["warehouses"][0]["surprise"] = 1
        with self.assertRaises(ValueError):
            p.validate_evidence_payload(payload)

    def test_error_messages_never_echo_values(self):
        payload = envelope(p.as_serializable(self.evidence))
        payload["warehouses"][0]["cost_history_days"] = "owner@secret.example"
        with self.assertRaises(ValueError) as ctx:
            p.validate_evidence_payload(payload)
        self.assertNotIn("owner@secret.example", str(ctx.exception))
        self.assertIn("cost_history_days", str(ctx.exception))

    def test_save_fails_closed_and_writes_nothing_for_invalid_evidence(self):
        bad = copy.deepcopy(self.evidence)
        bad[0].forecast_metadata.pop("dbu_forecast_status")
        path = self.dir / "evidence.json"
        with self.assertRaises(ValueError):
            p.save_evidence(path, bad)
        self.assertFalse(path.exists())

    def test_every_golden_fixture_item_and_synthetic_fleet_matches_the_schema(self):
        items = []
        for name in sorted(glob.glob(str(ROOT / "tests" / "golden" / "*.json"))):
            items += json.loads(Path(name).read_text()).get("evidence") or []
        self.assertGreaterEqual(len(items), 16)
        p.validate_evidence_payload(envelope(items))
        p.validate_evidence_payload(envelope(p.as_serializable(p.synthetic_demo_evidence(15))))

    def test_schema_properties_match_the_dataclasses(self):
        defs = SCHEMA["$defs"]
        pairs = {
            "warehouse": p.WarehouseEvidence, "epoch": p.EpochEvidence, "config": p.WarehouseConfig,
            "photonObservation": p.PhotonObservation, "attribution": p.WarehouseAttribution,
        }
        for key, cls in pairs.items():
            with self.subTest(schema=key):
                names = {f.name for f in dataclasses.fields(cls)}
                self.assertEqual(set(defs[key]["properties"]), names)
                self.assertEqual(set(defs[key]["required"]), names)
        query_source = defs["attribution"]["properties"]["query_source"]
        self.assertEqual(set(query_source["properties"]), {f.name for f in dataclasses.fields(p.QuerySourceProfile)})
        self.assertEqual(set(query_source["required"]), set(query_source["properties"]))


if __name__ == "__main__":
    unittest.main()
