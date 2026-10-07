"""Realizer ledger (src/realizer/packager/ledger.py): the read side (path layout, loading, the latest-event lookup) and
the write side (entry identity, append with duplicate suppression, the checkpoint, the regeneration record).

Each function returns, and raises, what the stored expectations (``tests/fixtures/realizer/expectations/``) hold: every
ledger of the compatibility baseline loads to the same entries and answers the latest-event lookups the same way, every
stored entry is rebuilt with its stored RLE id, a malformed input raises the same exception, and appending (each entry
twice) leaves the same file and logs the same lines. The mutation check proves the comparison notices a dropped duplicate
suppression.
"""
import copy
import hashlib
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import Opaque, canonical, compact_tree, fixture_package, outcome, tree  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.contracts.realization import EventType, canonical_json  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.packager import ledger, packager  # noqa: E402


class RegenerationFileTests(unittest.TestCase):
    """A request already in the ledger keeps the evidence and window of the run that first asked, in its entry and its
    RERUN file, even when the file has to be written again."""

    def request(self, evidence_id, start):
        from realizer.contracts.reconciliation import RegenerationRequest
        payload = {"regeneration_request_id": "RERUN-" + "A" * 24, "reasons": ["CONFIGURATION_DRIFT"], "evidence_id": evidence_id,
                   "observation_window": {"start_utc": start, "end_utc": "2026-10-20T00:00:00Z"}, "status": "REQUESTED"}
        return RegenerationRequest(regeneration_request_id=payload["regeneration_request_id"], application_episode_id=None, payload=payload)

    def test_a_missing_file_is_restored_from_the_first_request(self):
        package = fixture_package()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            ledger.append_regeneration(package, out, self.request("REVID-" + "1" * 24, "2026-10-01T00:00:00Z"), "2026-10-20T00:00:00Z", "2026-10-20T00:00:00Z")
            path = ledger.regeneration_path(out, "RERUN-" + "A" * 24)
            path.unlink()
            ledger.append_regeneration(package, out, self.request("REVID-" + "2" * 24, "2026-10-08T00:00:00Z"), "2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z")
            written = json.loads(path.read_text())
        self.assertEqual((written["evidence_id"], written["observation_window"]["start_utc"]), ("REVID-" + "1" * 24, "2026-10-01T00:00:00Z"))


class LedgerReadTests(unittest.TestCase):
    def test_path_layout(self):
        out = Path("/tmp/out")
        self.assertEqual(ledger.ledger_path(out, "RPKG-X"), out / "realization" / "ledger" / "RPKG-X.jsonl")

    def test_every_stored_ledger_loads_and_answers_the_latest_event_lookups(self):
        types = [("APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED"), ("ECONOMIC_MEASUREMENT_RECORDED",), ("DRIFT_DETECTED",), ("NOPE_TYPE",)]
        loaded = {}
        for name in canonical.scenario_names():
            for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
                entries = ledger.load_ledger(path)
                latest = []
                for names in types:
                    found = ledger.latest_event(entries, *[EventType(n) for n in names if n != "NOPE_TYPE"])
                    latest.append(None if found is None else found.ledger_entry_id)
                loaded[f"{name}/{path.stem}"] = {"entries": [e.to_dict() for e in entries], "latest": latest}
        self.assertGreater(len(loaded), 30)
        self.assertEqual(ledger.latest_event([]), None)
        expect.check(self, "ledgers", loaded, spot={"ledgers": len(loaded), "entries": sum(len(v["entries"]) for v in loaded.values()),
                                                    "first": next(iter(loaded)), "latest_of_first": next(iter(loaded.values()))["latest"]})

    def test_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ledger.load_ledger(Path(tmp) / "none.jsonl"), [])

    def test_malformed_ledger_raises(self):
        good = next((canonical.EXPECTED / "GR-024" / "realization" / "ledger").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
        first = json.loads(good[0])
        cases = {
            "not json": "not json\n",
            "unknown event type": json.dumps({**first, "event_type": "NOPE"}) + "\n",
            "missing event type": json.dumps({k: v for k, v in first.items() if k != "event_type"}) + "\n",
            "unexpected field": json.dumps({**first, "extra": 1}) + "\n",
            "missing field": json.dumps({k: v for k, v in first.items() if k != "payload"}) + "\n",
            "invalid id": json.dumps({**first, "ledger_entry_id": "BAD"}) + "\n",
            "bad second line": good[0] + "\n[1]\n",
            "blank lines only": "\n  \n",
        }
        for label, text in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "l.jsonl"
                path.write_text(text, encoding="utf-8")
                result = outcome(lambda: [e.to_dict() for e in ledger.load_ledger(path)])
                if label != "blank lines only":
                    self.assertEqual(result[0], "LedgerError")  # an unreadable line is a refusal that names the file and line
                    self.assertRegex(result[1], r"^ledger l\.jsonl line [12] is not a valid ledger entry \(\w+\)$")
                expect.check(self, label, result)
        with tempfile.TemporaryDirectory() as tmp:
            expect.check(self, "a directory", outcome(ledger.load_ledger, Path(tmp))[0])


def stored_ledgers():
    """``(scenario, source package, stored ledger lines)`` for every ledger of the compatibility baseline."""
    for name in canonical.scenario_names():
        package = canonical.load_scenario(name)["package_doc"]
        for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
            yield name, package, path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def entry_args(package, line):
    """The ``make_ledger_entry`` arguments that rebuild a stored ledger line."""
    return dict(
        package=copy.deepcopy(package), rpkg_id=line["realization_package_id"], event_type=EventType(line["event_type"]),
        effective_at_utc=line["effective_at_utc"], recorded_at_utc=line["recorded_at_utc"], payload=copy.deepcopy(line["payload"]),
        evidence_refs=line["evidence_refs"], application_episode_id=line["application_episode_id"],
        validation_episode_id=line["validation_episode_id"], measurement_id=line["measurement_id"],
        caused_by_entry_id=line["caused_by_entry_id"], corrects_entry_id=line["corrects_entry_id"],
    )


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")


class capture_logs:
    """The ``realizer`` logger's lines (level and message) while the block runs."""
    def __enter__(self):
        self.logger, self.handler = logging.getLogger("realizer"), _Logs()
        self.level = self.logger.level
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        return self.handler.lines

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)


class WriteSideTests(unittest.TestCase):
    def test_business_key_for_every_stored_entry(self):
        keys = []
        for _, _, _, lines in stored_ledgers():
            for line in lines:
                payload = {**line["payload"], "application_episode_id": line["application_episode_id"],
                           "validation_episode_id": line["validation_episode_id"], "measurement_id": line["measurement_id"]}
                keys.append(ledger.ledger_business_key(EventType(line["event_type"]), payload))
        self.assertGreater(len(keys), 300)
        expect.check(self, "keys", keys, spot={"count": len(keys), "first": keys[0], "last": keys[-1]})
        for bad in (None, 5, [("a", 1)]):
            expect.check(self, f"bad payload {bad!r}", outcome(ledger.ledger_business_key, EventType.DRIFT_DETECTED, bad))

    def test_every_stored_entry_is_rebuilt_with_its_id(self):
        count = 0
        for name, package, _, lines in stored_ledgers():
            for line in lines:
                with self.subTest(scenario=name, entry=line["ledger_entry_id"]):
                    entry = ledger.make_ledger_entry(**entry_args(package, line))
                    self.assertEqual(entry.ledger_entry_id, line["ledger_entry_id"])
                    self.assertEqual(json.loads(canonical_json(entry.to_dict())), line)
                    count += 1
        self.assertGreater(count, 300)

    def test_malformed_input_raises(self):
        package = fixture_package()
        _, _, _, lines = next(stored_ledgers())
        line = next(x for x in lines if x["event_type"] == "VALIDATION_COMPLETED") if any(x["event_type"] == "VALIDATION_COMPLETED" for x in lines) else lines[-1]
        no_warehouse = {k: v for k, v in package.items() if k != "warehouse"}
        no_workspace = {**package, "warehouse": {k: v for k, v in package["warehouse"].items() if k != "workspace_id"}}
        no_package_id = {k: v for k, v in package.items() if k != "recommendation_package_id"}
        cases = {
            "package without warehouse": dict(package=no_warehouse),
            "warehouse without workspace": dict(package=no_workspace),
            "package without id": dict(package=no_package_id),
            "event type is text": dict(event_type="DRIFT_DETECTED"),
            "effective time not a time": dict(effective_at_utc="not a time"),
            "effective time missing": dict(effective_at_utc=None),
            "recorded time a number": dict(recorded_at_utc=5),
            "payload missing": dict(payload=None),
            "payload a list": dict(payload=[1]),
            "payload not serializable": dict(payload={"x": Opaque()}),
            "realization package id malformed": dict(rpkg_id="BAD"),
            "application episode malformed": dict(application_episode_id="BAD"),
            "validation episode malformed": dict(validation_episode_id="BAD"),
            "measurement malformed": dict(measurement_id="BAD"),
            "caused-by malformed": dict(caused_by_entry_id="BAD"),
            "corrects entry malformed": dict(corrects_entry_id="BAD"),
            "evidence refs not iterable": dict(evidence_refs=5),
        }
        for label, patch in cases.items():
            with self.subTest(case=label):
                def build():
                    args = entry_args(package, line)
                    args.update(copy.deepcopy(patch))
                    return ledger.make_ledger_entry(**args).to_dict()
                result = outcome(build)
                self.assertNotEqual(result[0], "ok", label)
                expect.check(self, label, result)

    def test_an_entry_the_schema_refuses_is_a_defect_and_is_never_built(self):
        """``make_ledger_entry`` checks the entry against ``ledger_entry_schema`` after the dataclass validation; an entry that
        passes the id and time checks but not the schema (a non-text evidence reference, a payload that is not a mapping
        once serialized, an empty workspace id) raises ``jsonschema.ValidationError``, not a ``RealizerError``."""
        import jsonschema
        from realizer.common.errors import RealizerError
        package = fixture_package()
        _, _, _, lines = next(stored_ledgers())
        cases = {
            "evidence reference not text": dict(evidence_refs=[1]),
            "evidence reference null": dict(evidence_refs=[None]),
            "empty workspace id": dict(package={**package, "warehouse": {**package["warehouse"], "workspace_id": ""}}),
        }
        for label, patch in cases.items():
            with self.subTest(case=label):
                args = entry_args(package, lines[0])
                args.update(copy.deepcopy(patch))
                with self.assertRaises(jsonschema.ValidationError) as caught:
                    ledger.make_ledger_entry(**args)
                self.assertNotIsInstance(caught.exception, RealizerError)

    def test_an_entry_with_an_invalid_event_payload_type_is_refused_by_the_schema_check(self):
        import jsonschema
        from realizer.contracts.realization_schema import validate_ledger_entry
        package = fixture_package()
        _, _, _, lines = next(stored_ledgers())
        good = ledger.make_ledger_entry(**entry_args(package, lines[0])).to_dict()
        self.assertIsNone(validate_ledger_entry(good))
        for key, value in (("payload", [1]), ("payload", "text"), ("event_type", "NOPE"), ("evidence_refs", "RLE-X"), ("provenance", None)):
            with self.subTest(key=key, value=value), self.assertRaises(jsonschema.ValidationError):
                validate_ledger_entry({**good, key: value})

    def test_every_stored_ledger_entry_validates_against_the_schema_and_carries_the_publisher_release(self):
        from publisher.config.versions import PUBLISHER_VERSION
        from realizer.contracts.realization_schema import validate_ledger_entry
        count = 0
        for _, _, _, lines in stored_ledgers():
            for line in lines:
                validate_ledger_entry(line)
                self.assertEqual(line["provenance"]["product_version"], PUBLISHER_VERSION)
                count += 1
        self.assertGreater(count, 100)

    def test_checkpoint_for_every_ledger_prefix_and_order(self):
        checkpoints = []
        for name, _, path, _ in stored_ledgers():
            entries = ledger.load_ledger(path)
            for k in range(len(entries) + 1):
                for order in ("forward", "reversed"):
                    cut = entries[:k] if order == "forward" else entries[:k][::-1]
                    checkpoints.append([name, path.stem, k, order, ledger.ledger_checkpoint(cut)])
        self.assertGreater(len(checkpoints), 600)
        expect.check(self, "checkpoints", checkpoints, spot={"count": len(checkpoints), "first": checkpoints[0], "last": checkpoints[-1]})
        for bad in (5, [None], [{}]):
            expect.check(self, f"bad entries {bad!r}", outcome(ledger.ledger_checkpoint, bad))


def append_all(entries, path):
    """Append every entry twice (the repeat is a duplicate); the results and the logged lines."""
    results = []
    with capture_logs() as logs:
        for entry in entries + entries:
            results.append(ledger.append_if_absent(path, entry))
    return results, logs


class AppendTests(unittest.TestCase):
    def test_every_stored_ledger_appends_and_suppresses_duplicates(self):
        count = 0
        appended = {}
        for name, _, source, _ in stored_ledgers():
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "deep" / "l.jsonl"
                entries = ledger.load_ledger(source)
                results, logs = append_all(entries, path)
                self.assertEqual(results, [True] * len(entries) + [False] * len(entries))
                self.assertEqual(sum("ledger.append" in line for line in logs), len(entries))
                self.assertEqual(sum("ledger.duplicate_suppressed" in line for line in logs), len(entries))
                appended[f"{name}/{source.stem}"] = {"results": results, "logs": logs, "file": hashlib.sha256(path.read_bytes()).hexdigest()}
                count += len(entries)
        self.assertGreater(count, 300)
        expect.check(self, "appended", appended, spot={"ledgers": len(appended), "entries": count, "first": next(iter(appended))})

    def test_a_corrupt_ledger_fails_before_anything_is_appended(self):
        _, _, source, _ = next(stored_ledgers())
        entry = ledger.load_ledger(source)[0]
        for label, text in (("not json", "not json\n"), ("a list", "[1]\n"), ("unknown event type", '{"event_type": "NOPE"}\n')):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "l.jsonl"
                path.write_text(text, encoding="utf-8")
                with capture_logs() as logs:
                    result = (outcome(ledger.append_if_absent, path, entry), logs, path.read_text(encoding="utf-8"))
                self.assertEqual(result[0][0], "LedgerError")
                self.assertEqual(result[2], text)
                expect.check(self, label, result)

    def test_an_entry_that_cannot_be_serialized_leaves_the_file_alone(self):
        _, _, source, _ = next(stored_ledgers())
        entry = ledger.load_ledger(source)[0]
        object.__setattr__(entry, "payload", {"x": Opaque()})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l.jsonl"
            with capture_logs() as logs:
                result = (outcome(ledger.append_if_absent, path, entry), logs, path.exists() and path.read_text(encoding="utf-8"))
        self.assertEqual(result[0][0], "TypeError")
        expect.check(self, "result", result)


class RegenerationRecordTests(unittest.TestCase):
    """``append_regeneration`` records a regeneration request after its payload is decided: the REGENERATION_REQUESTED
    entry on the application episode, then the RERUN file. ``packager.request_regeneration`` is the two steps in one call."""

    def test_records_the_entry_and_file(self):
        from realizer.reconciler.lifecycle import regeneration_request
        package = fixture_package()
        observation = demo_observation(package)
        app = "APP-" + "0123456789AB" * 2
        recorded = "2026-10-09T12:00:00Z"
        payloads = {"no application payload": {},
                    "full application payload": {"observed_config": {"warehouse_size": "SMALL"}, "application_episode_id": app, "invalidates": ["min_clusters"]}}
        for label, app_payload in payloads.items():
            with self.subTest(app_payload=app_payload):
                results = []
                for write in ("append_regeneration", "request_regeneration"):
                    with tempfile.TemporaryDirectory() as tmp, capture_logs() as logs:
                        out = Path(tmp)
                        if write == "request_regeneration":
                            result = outcome(packager.request_regeneration, copy.deepcopy(package), dict(app_payload), observation, out,
                                             RealizerPolicy(), recorded, recorded, reason="R")
                        else:
                            request = regeneration_request(package, dict(app_payload), observation, RealizerPolicy(), reason="R")
                            result = outcome(ledger.append_regeneration, copy.deepcopy(package), out, request, recorded, recorded)
                        results.append((result, logs, compact_tree(tree(out))))
                self.assertEqual(results[0], results[1])
                self.assertEqual(results[0][0], ("ok", None))
                self.assertTrue(any(name.startswith("realization/regeneration/RERUN-") for name in results[0][2]["files"]))
                expect.check(self, label, results[0])


class DuplicateSuppressionMutationTests(unittest.TestCase):
    """Mutation check: an ``append_if_absent`` that does not reload and suppress is caught by the same comparison."""

    def test_dropping_the_duplicate_suppression_changes_the_ledger(self):
        _, _, source, _ = next(stored_ledgers())
        entries = ledger.load_ledger(source)

        def appends_blindly(path, entry):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(canonical_json(entry.to_dict()) + "\n")
            return True

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            real = append_all(entries, Path(a) / "l.jsonl")
            with mock.patch.object(ledger, "append_if_absent", appends_blindly):
                mutant = append_all(entries, Path(b) / "l.jsonl")
            self.assertNotEqual(real, mutant)
            self.assertNotEqual((Path(a) / "l.jsonl").read_bytes(), (Path(b) / "l.jsonl").read_bytes())


if __name__ == "__main__":
    unittest.main()
