"""Realizer ledger (src/realizer/packager/ledger.py): the read side (path layout, loading, the latest-event lookup) and
the write side (entry identity, append with duplicate suppression, the checkpoint, the regeneration record).

Each function returns, and raises, what the frozen oracle's copy did; every ledger of the compatibility baseline loads
to the same entries, every stored entry is rebuilt with the same RLE id, and appending (each entry twice) leaves the same
file and logs the same lines. The mutation checks prove the comparisons notice a dropped duplicate suppression.
"""
import copy
import inspect
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.unit.realizer.parity import Opaque, canonical, engine, fixture_package, oengine, outcome, tree  # noqa: E402
from realizer.packager import ledger  # noqa: E402


class LedgerReadTests(unittest.TestCase):
    def test_engine_reexports_and_path_layout(self):
        for name in ("ledger_path", "load_ledger", "latest_event"):
            self.assertIs(getattr(engine, name), getattr(ledger, name))
        out = Path("/tmp/out")
        self.assertEqual(ledger.ledger_path(out, "RPKG-X"), oengine.ledger_path(out, "RPKG-X"))
        self.assertEqual(ledger.ledger_path(out, "RPKG-X"), out / "realization" / "ledger" / "RPKG-X.jsonl")

    def test_every_stored_ledger_loads_like_the_oracle_and_latest_event_agrees(self):
        types = [("APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED"), ("ECONOMIC_MEASUREMENT_RECORDED",), ("DRIFT_DETECTED",), ("NOPE_TYPE",)]
        count = 0
        for name in canonical.scenario_names():
            for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
                with self.subTest(scenario=name):
                    new, old = ledger.load_ledger(path), oengine.load_ledger(path)
                    self.assertEqual([e.to_dict() for e in new], [e.to_dict() for e in old])
                    for names in types:
                        found_new = ledger.latest_event(new, *[engine.rc.EventType(n) for n in names if n != "NOPE_TYPE"])
                        found_old = oengine.latest_event(old, *[oengine.rc.EventType(n) for n in names if n != "NOPE_TYPE"])
                        self.assertEqual(None if found_new is None else found_new.ledger_entry_id,
                                         None if found_old is None else found_old.ledger_entry_id)
                    count += 1
        self.assertGreater(count, 30)
        self.assertEqual(ledger.latest_event([]), None)

    def test_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ledger.load_ledger(Path(tmp) / "none.jsonl"), [])
            self.assertEqual(oengine.load_ledger(Path(tmp) / "none.jsonl"), [])

    def test_malformed_ledger_raises_the_oracle_exception(self):
        good = next((canonical.EXPECTED / "GR-024" / "realization" / "ledger").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
        import json
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
                new = outcome(lambda: [e.to_dict() for e in ledger.load_ledger(path)])
                old = outcome(lambda: [e.to_dict() for e in oengine.load_ledger(path)])
                self.assertEqual(new, old)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(outcome(ledger.load_ledger, Path(tmp))[0], outcome(oengine.load_ledger, Path(tmp))[0])


def stored_ledgers():
    """``(scenario, source package, stored ledger lines)`` for every ledger of the compatibility baseline."""
    for name in canonical.scenario_names():
        package = canonical.load_scenario(name)["package_doc"]
        for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
            yield name, package, path, [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def entry_args(module, package, line):
    """The ``make_ledger_entry`` arguments that rebuild a stored ledger line with *module*."""
    return dict(
        package=copy.deepcopy(package), rpkg_id=line["realization_package_id"], event_type=module.rc.EventType(line["event_type"]),
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


class MovedWriteSideTests(unittest.TestCase):
    NAMES = ("ledger_business_key", "make_ledger_entry", "append_if_absent", "ledger_checkpoint")

    def test_engine_reexports(self):
        for name in self.NAMES + ("append_regeneration",):
            self.assertIs(getattr(engine, name), getattr(ledger, name), name)

    def test_source_and_signature_are_the_oracles(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(inspect.getsource(getattr(ledger, name)), inspect.getsource(getattr(oengine, name)))
                self.assertEqual(str(inspect.signature(getattr(ledger, name))), str(inspect.signature(getattr(oengine, name))))

    def test_business_key_equals_the_oracle_for_every_stored_entry(self):
        count = 0
        for _, _, _, lines in stored_ledgers():
            for line in lines:
                payload = {**line["payload"], "application_episode_id": line["application_episode_id"],
                           "validation_episode_id": line["validation_episode_id"], "measurement_id": line["measurement_id"]}
                new = ledger.ledger_business_key(engine.rc.EventType(line["event_type"]), payload)
                self.assertEqual(new, oengine.ledger_business_key(oengine.rc.EventType(line["event_type"]), payload))
                count += 1
        self.assertGreater(count, 300)
        for bad in (None, 5, [("a", 1)]):
            self.assertEqual(outcome(ledger.ledger_business_key, engine.rc.EventType.DRIFT_DETECTED, bad),
                             outcome(oengine.ledger_business_key, oengine.rc.EventType.DRIFT_DETECTED, bad))

    def test_every_stored_entry_is_rebuilt_with_its_id(self):
        count = 0
        for name, package, _, lines in stored_ledgers():
            for line in lines:
                with self.subTest(scenario=name, entry=line["ledger_entry_id"]):
                    new = ledger.make_ledger_entry(**entry_args(engine, package, line))
                    old = oengine.make_ledger_entry(**entry_args(oengine, package, line))
                    self.assertEqual(new.to_dict(), old.to_dict())
                    self.assertEqual(new.ledger_entry_id, line["ledger_entry_id"])
                    self.assertEqual(json.loads(engine.rc.canonical_json(new.to_dict())), line)
                    count += 1
        self.assertGreater(count, 300)

    def test_malformed_input_raises_the_oracle_exception(self):
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
                def build(module):
                    args = entry_args(module, package, line)
                    args.update({k: (getattr(module.rc.EventType, v.name) if k == "event_type" and hasattr(v, "name") else v) for k, v in copy.deepcopy(patch).items()})
                    return module.make_ledger_entry(**args).to_dict()
                old = outcome(build, oengine)
                self.assertNotEqual(old[0], "ok", label)
                self.assertEqual(outcome(build, engine), old)

    def test_checkpoint_equals_the_oracle_for_every_ledger_prefix_and_order(self):
        count = 0
        for name, _, path, _ in stored_ledgers():
            new, old = ledger.load_ledger(path), oengine.load_ledger(path)
            for k in range(len(new) + 1):
                for order in (slice(None), slice(None, None, -1)):
                    self.assertEqual(ledger.ledger_checkpoint(new[:k][order]), oengine.ledger_checkpoint(old[:k][order]), (name, k))
                    count += 1
        self.assertGreater(count, 600)
        for bad in (5, [None], [{}]):
            self.assertEqual(outcome(ledger.ledger_checkpoint, bad), outcome(oengine.ledger_checkpoint, bad))


class AppendTests(unittest.TestCase):
    def append_all(self, module, entries, path):
        results, logs = [], None
        with capture_logs() as logs:
            for entry in entries + entries:  # every entry twice: the repeat is a duplicate
                results.append(module.append_if_absent(path, entry))
        return results, logs

    def test_every_stored_ledger_appends_and_suppresses_duplicates_like_the_oracle(self):
        count = suppressed = 0
        for name, _, source, _ in stored_ledgers():
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                new_path, old_path = Path(a) / "deep" / "l.jsonl", Path(b) / "deep" / "l.jsonl"
                new_entries, old_entries = ledger.load_ledger(source), oengine.load_ledger(source)
                new_results, new_logs = self.append_all(ledger, new_entries, new_path)
                old_results, old_logs = self.append_all(oengine, old_entries, old_path)
                self.assertEqual(new_results, old_results)
                self.assertEqual(new_logs, old_logs)
                self.assertEqual(new_path.read_bytes(), old_path.read_bytes())
                self.assertEqual(new_results, [True] * len(new_entries) + [False] * len(new_entries))
                self.assertEqual(sum("ledger.append" in line for line in new_logs), len(new_entries))
                self.assertEqual(sum("ledger.duplicate_suppressed" in line for line in new_logs), len(new_entries))
                count += len(new_entries)
                suppressed += len(new_entries)
        self.assertGreater(count, 300)

    def test_a_corrupt_ledger_fails_before_anything_is_appended(self):
        _, _, source, _ = next(stored_ledgers())
        entry = ledger.load_ledger(source)[0]
        old_entry = oengine.load_ledger(source)[0]
        for text in ("not json\n", "[1]\n", '{"event_type": "NOPE"}\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                results = []
                for module, e, root in ((ledger, entry, a), (oengine, old_entry, b)):
                    path = Path(root) / "l.jsonl"
                    path.write_text(text, encoding="utf-8")
                    with capture_logs() as logs:
                        results.append((outcome(module.append_if_absent, path, e), logs, path.read_text(encoding="utf-8")))
                self.assertEqual(results[0], results[1])
                self.assertNotEqual(results[0][0][0], "ok")
                self.assertEqual(results[0][2], text)

    def test_an_entry_that_cannot_be_serialized_leaves_the_file_alone(self):
        _, _, source, _ = next(stored_ledgers())
        results = []
        for module in (ledger, oengine):
            entry = module.load_ledger(source)[0]
            object.__setattr__(entry, "payload", {"x": Opaque()})
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "l.jsonl"
                with capture_logs() as logs:
                    results.append((outcome(module.append_if_absent, path, entry), logs, path.exists() and path.read_text(encoding="utf-8")))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0][0], "TypeError")


class RegenerationRecordTests(unittest.TestCase):
    """``append_regeneration`` is the oracle's ``request_regeneration`` after its payload is decided: the REGENERATION_REQUESTED
    entry on the application episode, then the RERUN file."""

    def test_records_the_oracle_entry_and_file(self):
        from realizer.reconciler.lifecycle import regeneration_request
        package = fixture_package()
        observation = oengine.demo_observation(package)
        app = "APP-" + "0123456789AB" * 2
        recorded = "2026-10-09T12:00:00Z"
        for app_payload in ({}, {"observed_config": {"warehouse_size": "SMALL"}, "application_episode_id": app, "invalidates": ["min_clusters"]}):
            with self.subTest(app_payload=app_payload):
                outcomes = []
                for write in ("append_regeneration", "oracle"):
                    with tempfile.TemporaryDirectory() as tmp, capture_logs() as logs:
                        out = Path(tmp)
                        if write == "oracle":
                            result = outcome(oengine.request_regeneration, copy.deepcopy(package), dict(app_payload), observation, out,
                                             oengine.RealizerPolicy(), recorded, recorded, reason="R")
                        else:
                            request = regeneration_request(package, dict(app_payload), observation, engine.RealizerPolicy(), reason="R")
                            result = outcome(ledger.append_regeneration, copy.deepcopy(package), out, request, recorded, recorded)
                        outcomes.append((result, logs, tree(out)))
                self.assertEqual(outcomes[0], outcomes[1])
                self.assertEqual(outcomes[0][0], ("ok", None))
                self.assertTrue(any(name.startswith("realization/regeneration/RERUN-") for name in outcomes[0][2]))

    def test_request_regeneration_is_the_packager_entry_point(self):
        from realizer.packager import packager
        self.assertIs(engine.request_regeneration, packager.request_regeneration)
        self.assertEqual(str(inspect.signature(packager.request_regeneration)), str(inspect.signature(oengine.request_regeneration)))


class DuplicateSuppressionMutationTests(unittest.TestCase):
    """Mutation check: an ``append_if_absent`` that does not reload and suppress is caught by the same comparison."""

    def test_dropping_the_duplicate_suppression_changes_the_ledger(self):
        _, _, source, _ = next(stored_ledgers())
        entries = ledger.load_ledger(source)

        def appends_blindly(path, entry):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(engine.rc.canonical_json(entry.to_dict()) + "\n")
            return True

        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            real = AppendTests().append_all(ledger, entries, Path(a) / "l.jsonl")
            with mock.patch.object(ledger, "append_if_absent", appends_blindly):
                mutant = AppendTests().append_all(ledger, entries, Path(b) / "l.jsonl")
            self.assertNotEqual(real, mutant)
            self.assertNotEqual((Path(a) / "l.jsonl").read_bytes(), (Path(b) / "l.jsonl").read_bytes())


if __name__ == "__main__":
    unittest.main()
