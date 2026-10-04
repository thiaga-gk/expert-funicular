"""M9 Stage 1 (Reporter Contract 1.2.0 / Workbook Contract 1.3.0): re-evaluation window and observed change."""
import copy
import dataclasses
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p
from contracts import reporter_contract as rc
from contracts import reporter_schema
from contracts import workbook_contract as wc

GOLDEN = ROOT / "tests" / "golden"


def golden_evidence(name, *, current_start=None, cost_history_days=None, analysis_time=None):
    payload = json.loads((GOLDEN / f"{name}.json").read_text())["evidence"][0]
    if analysis_time:
        payload["analysis_time"] = analysis_time
    if cost_history_days is not None:
        payload["cost_history_days"] = cost_history_days
    if current_start:
        for ep in payload["epochs"]:
            if ep["is_current"]:
                ep["epoch_start"] = current_start
        prior = [e for e in payload["epochs"] if not e["is_current"]]
        for ep in prior:  # keep the prior epoch strictly before the new current start
            ep["epoch_end"] = current_start
    return [p.evidence_from_dict(payload)]


def build(evidence):
    return p.build_recommendation_packages(evidence, p.Thresholds())[0]


class ContractConstantsTests(unittest.TestCase):
    def test_versions_and_gate_constants_match_policy(self):
        self.assertEqual(rc.CONTRACT_VERSION, "1.2.0")
        self.assertEqual(wc.WORKBOOK_CONTRACT_VERSION, "1.3.0")
        t = p.Thresholds()
        # The validator embeds these gates; changing the policy thresholds therefore requires a contract bump.
        self.assertEqual(rc.REEVALUATION_EPOCH_MATURITY_DAYS, t.current_config_stability_days)
        self.assertEqual(rc.REEVALUATION_COST_HISTORY_DAYS, t.min_economics_cost_history_days)

    def test_earliest_reevaluation_arithmetic(self):
        start = "2026-10-01T15:00:00Z"
        both = ["COST_HISTORY_INSUFFICIENT", "CURRENT_CONFIG_EPOCH_TOO_NEW"]
        self.assertEqual(rc.expected_earliest_reevaluation_time(start, both), "2026-10-08T15:00:00Z")
        self.assertEqual(rc.expected_earliest_reevaluation_time(start, ["CURRENT_CONFIG_EPOCH_TOO_NEW"]), "2026-10-08T15:00:00Z")
        self.assertEqual(rc.expected_earliest_reevaluation_time(start, ["COST_HISTORY_INSUFFICIENT"]), "2026-10-08T00:00:00Z")

    def test_reevaluation_window_validation(self):
        ok = rc.ReevaluationWindow(rc.ReevaluationStatus.WAITING_FOR_EVIDENCE, "2026-10-08T15:00:00Z", ["CURRENT_CONFIG_EPOCH_TOO_NEW"], "m")
        ok.validate()
        for bad in (
            dataclasses.replace(ok, blocking_codes=[]),
            dataclasses.replace(ok, blocking_codes=["NOT_A_GATE"]),
            dataclasses.replace(ok, blocking_codes=["CURRENT_CONFIG_EPOCH_TOO_NEW", "COST_HISTORY_INSUFFICIENT"]),  # unsorted
            dataclasses.replace(ok, blocking_codes=["COST_HISTORY_INSUFFICIENT", "COST_HISTORY_INSUFFICIENT"]),
            dataclasses.replace(ok, earliest_reevaluation_time="tomorrow"),
            dataclasses.replace(ok, method=""),
        ):
            with self.assertRaises(ValueError):
                bad.validate()

    def test_observed_change_validation(self):
        change = rc.ConfigChange(rc.TunableKnob.WAREHOUSE_SIZE, "LARGE", "MEDIUM")
        ok = rc.ObservedConfigChange("EPOCH-" + "A" * 24, "2026-01-15T00:00:00Z", "2026-09-29T00:00:00Z", [change], rc.MeasurementStatus.NOT_MEASURED)
        ok.validate()
        for bad in (
            dataclasses.replace(ok, changes=[]),
            dataclasses.replace(ok, changes=[rc.ConfigChange(rc.TunableKnob.WAREHOUSE_SIZE, "LARGE", "LARGE")]),
            dataclasses.replace(ok, changes=[change, change]),
            dataclasses.replace(ok, changes=[rc.ConfigChange(rc.TunableKnob.WAREHOUSE_SIZE, "A", "B"), rc.ConfigChange(rc.TunableKnob.MAX_CLUSTERS, 2, 3)]),  # unsorted
            dataclasses.replace(ok, previous_epoch_id="EPOCH-bad"),
        ):
            with self.assertRaises(ValueError):
                bad.validate()


class ReevaluationFromPublisherTests(unittest.TestCase):
    def test_young_epoch_waits_with_exact_earliest_time_and_observed_change(self):
        pkg = build(golden_evidence("newly-changed-config"))  # epoch 2026-09-29T00:00Z, analysis 2026-10-01
        re = pkg.recommended_plan.reevaluation
        self.assertIsNotNone(re)
        self.assertEqual(re.blocking_codes, ["CURRENT_CONFIG_EPOCH_TOO_NEW"])
        self.assertEqual(re.earliest_reevaluation_time, "2026-10-06T00:00:00Z")
        self.assertEqual(pkg.recommended_plan.next_action, "WAIT_FOR_REEVALUATION")
        self.assertEqual(pkg.recommended_plan.selection_reason_code, "CURRENT_EPOCH_IMMATURE")
        oc = pkg.observed_change
        self.assertEqual(oc.changed_at, pkg.warehouse.current_epoch.start_time)
        self.assertEqual([(c.knob.value, c.previous_value, c.current_value) for c in oc.changes], [("warehouse_size", "MEDIUM", "LARGE")])
        self.assertEqual(oc.measurement_status, rc.MeasurementStatus.NOT_MEASURED)

    def test_mature_epoch_has_no_reevaluation_but_keeps_the_observed_change(self):
        pkg = build(golden_evidence("spill-constrained"))
        self.assertIsNone(pkg.recommended_plan.reevaluation)
        self.assertNotEqual(pkg.recommended_plan.next_action, "WAIT_FOR_REEVALUATION")
        self.assertIsNotNone(pkg.observed_change)
        self.assertEqual(pkg.observed_change.changed_at, "2026-01-15T00:00:00Z")

    def test_short_cost_history_alone_waits_for_the_billing_day(self):
        pkg = build(golden_evidence("spill-constrained", cost_history_days=3))
        re = pkg.recommended_plan.reevaluation
        self.assertEqual(re.blocking_codes, ["COST_HISTORY_INSUFFICIENT"])
        self.assertEqual(re.earliest_reevaluation_time, "2026-01-22T00:00:00Z")  # 00:00Z of epoch_start_date + 7d

    def test_ready_step_while_billing_accrues_keeps_its_next_action(self):
        # Mature configuration, three billed days: the ready step can proceed while the cost history fills in.
        pkg = build(golden_evidence("spill-constrained", cost_history_days=3))
        self.assertIsNotNone(pkg.recommended_plan.reevaluation)
        self.assertNotEqual(pkg.recommended_plan.next_action, "WAIT_FOR_REEVALUATION")

    def test_queue_pressure_on_a_young_epoch_waits_for_reevaluation(self):
        # A max-cluster increase needs a mature epoch like every other change, so the plan waits.
        pkg = build(golden_evidence("concurrency-constrained", current_start="2026-09-29T00:00:00+00:00"))
        self.assertIsNotNone(pkg.recommended_plan.reevaluation)
        self.assertEqual(pkg.recommended_plan.next_action, "WAIT_FOR_REEVALUATION")

    def test_both_gates_report_sorted_codes_and_the_later_time(self):
        pkg = build(golden_evidence("newly-changed-config", cost_history_days=2))
        re = pkg.recommended_plan.reevaluation
        self.assertEqual(re.blocking_codes, ["COST_HISTORY_INSUFFICIENT", "CURRENT_CONFIG_EPOCH_TOO_NEW"])
        self.assertEqual(re.earliest_reevaluation_time, "2026-10-06T00:00:00Z")

    def test_first_epoch_has_no_observed_change(self):
        ev = golden_evidence("spill-constrained")
        ev[0].epochs = [e for e in ev[0].epochs if e.is_current]
        self.assertIsNone(build(ev).observed_change)

    def test_identical_config_in_prior_epoch_is_not_a_change(self):
        ev = golden_evidence("spill-constrained")
        cur = ev[0].current_epoch
        prior = next(e for e in ev[0].epochs if not e.is_current)
        prior.config = cur.config
        self.assertIsNone(build(ev).observed_change)

    def test_multi_knob_change_is_sorted_by_knob(self):
        ev = golden_evidence("concurrency-constrained")  # prior MEDIUM/2/4/120 -> LARGE/1/2/45
        oc = build(ev).observed_change
        self.assertEqual([c.knob.value for c in oc.changes], sorted(c.knob.value for c in oc.changes))
        self.assertEqual({c.knob.value for c in oc.changes}, {"warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes"})

    def test_package_is_deterministic_and_realization_stays_not_applied(self):
        a, b = build(golden_evidence("newly-changed-config")), build(golden_evidence("newly-changed-config"))
        self.assertEqual(json.dumps(a.to_dict(), sort_keys=True), json.dumps(b.to_dict(), sort_keys=True))
        self.assertEqual(a.realization.status, rc.RealizationStatus.NOT_APPLIED)
        self.assertIsNone(a.carried_forward_plan)  # Stage 2 block is reserved and null


class PackageValidationAndIdentityTests(unittest.TestCase):
    def setUp(self):
        self.pkg = build(golden_evidence("newly-changed-config"))

    def reid(self, pkg):
        pkg = dataclasses.replace(pkg, recommendation_package_id=pkg.expected_package_id())
        return dataclasses.replace(pkg, provenance=dataclasses.replace(pkg.provenance, package_hash_full_sha256=pkg.full_package_hash()))

    def with_plan(self, **changes):
        return self.reid(dataclasses.replace(self.pkg, recommended_plan=dataclasses.replace(self.pkg.recommended_plan, **changes)))

    def test_new_blocks_change_the_package_identity(self):
        base = self.pkg.expected_package_id()
        other_re = dataclasses.replace(self.pkg.recommended_plan.reevaluation, earliest_reevaluation_time="2026-10-07T00:00:00Z")
        self.assertNotEqual(base, dataclasses.replace(self.pkg, recommended_plan=dataclasses.replace(self.pkg.recommended_plan, reevaluation=other_re)).expected_package_id())
        other_oc = dataclasses.replace(self.pkg.observed_change, changes=[dataclasses.replace(self.pkg.observed_change.changes[0], previous_value="SMALL")])
        self.assertNotEqual(base, dataclasses.replace(self.pkg, observed_change=other_oc).expected_package_id())
        # prose must not alter identity
        self.assertEqual(base, self.with_plan(decision_summary="different prose").expected_package_id())
        method_changed = dataclasses.replace(self.pkg.recommended_plan.reevaluation, method="reworded")
        self.assertEqual(base, dataclasses.replace(self.pkg, recommended_plan=dataclasses.replace(self.pkg.recommended_plan, reevaluation=method_changed)).expected_package_id())

    def test_tampered_reevaluation_is_rejected(self):
        re = self.pkg.recommended_plan.reevaluation
        for bad in (
            dataclasses.replace(re, earliest_reevaluation_time="2026-10-07T00:00:00Z"),       # wrong arithmetic
            dataclasses.replace(re, blocking_codes=["COST_HISTORY_INSUFFICIENT"]),             # gate not actually active
            dataclasses.replace(re, earliest_reevaluation_time="2026-09-01T00:00:00Z"),       # before epoch start
        ):
            with self.assertRaises(ValueError):
                self.with_plan(reevaluation=bad).validate()

    def test_missing_reevaluation_on_a_young_epoch_is_rejected(self):
        with self.assertRaises(ValueError):
            self.with_plan(reevaluation=None).validate()

    def test_reevaluation_on_a_mature_epoch_is_rejected(self):
        mature = build(golden_evidence("spill-constrained"))
        fake = dataclasses.replace(self.pkg.recommended_plan.reevaluation, earliest_reevaluation_time="2026-01-22T00:00:00Z")
        bad = dataclasses.replace(mature, recommended_plan=dataclasses.replace(mature.recommended_plan, reevaluation=fake))
        bad = self.reid(bad)
        with self.assertRaises(ValueError):
            bad.validate()

    def test_wait_action_requires_a_reevaluation_window_and_no_ready_step(self):
        mature = build(golden_evidence("spill-constrained"))
        bad = self.reid(dataclasses.replace(mature, recommended_plan=dataclasses.replace(mature.recommended_plan, next_action="WAIT_FOR_REEVALUATION")))
        with self.assertRaises(ValueError):
            bad.validate()

    def test_tampered_observed_change_is_rejected(self):
        oc = self.pkg.observed_change
        for bad in (
            dataclasses.replace(oc, changed_at="2026-09-30T00:00:00Z"),
            dataclasses.replace(oc, previous_epoch_id=self.pkg.warehouse.current_epoch.epoch_id),
        ):
            with self.assertRaises(ValueError):
                self.reid(dataclasses.replace(self.pkg, observed_change=bad)).validate()

    def test_planstep_may_not_use_the_carried_forward_pricing_basis(self):
        ranges = rc.ComponentRanges(None, None, rc.Range3(1, 2, 3))
        step = next(s for sc in build(golden_evidence("concurrency-constrained")).scenarios for s in sc.execution_plan if s.step_type == rc.PlanStepType.CANDIDATE)
        bad = dataclasses.replace(step, projected_economics=rc.StepProjection(rc.PricingBasis.CONTINGENT_CARRIED_FORWARD, ranges))
        with self.assertRaises(ValueError):
            bad.validate(candidate_ids=[step.candidate_id])


class CarriedForwardReservedBlockTests(unittest.TestCase):
    """Stage 2 is not produced yet; the reserved block must still be validated if present."""

    def plan(self, **kw):
        step = rc.CarriedForwardStep(1, rc.TunableKnob.WAREHOUSE_SIZE, "WH-SIZE-REDUCE-001", "LARGE", "MEDIUM", rc.CarriedForwardDisposition.COMPLETED, None, None)
        pend = rc.CarriedForwardStep(2, rc.TunableKnob.MAX_CLUSTERS, "WH-MAX-CLUSTER-ASSESS-001", 4, 3, rc.CarriedForwardDisposition.PENDING_REEVALUATION, None,
                                     rc.ComponentRanges(None, None, rc.Range3(5.0, 9.0, 12.0)))
        base = dict(
            source_epoch_id="EPOCH-" + "B" * 24, as_of="2026-09-29T00:00:00Z", match_policy=rc.MatchPolicy.EXACT_SINGLE_KNOB,
            match_outcome=rc.MatchOutcome.MATCHED, match_reason_code=None, replay_fingerprint="SHA256:" + "C" * 64,
            steps=[step, pend], remaining_upside_365d=rc.ComponentRanges(None, None, rc.Range3(5.0, 9.0, 12.0)),
            pricing_basis=rc.PricingBasis.CONTINGENT_CARRIED_FORWARD,
        )
        base.update(kw)
        return rc.CarriedForwardPlan(**base)

    def test_valid_block(self):
        self.plan().validate()

    def test_structural_rules(self):
        invalid = rc.CarriedForwardDisposition.INVALIDATED
        completed_with_savings = rc.CarriedForwardStep(1, rc.TunableKnob.WAREHOUSE_SIZE, "T", "A", "B", rc.CarriedForwardDisposition.COMPLETED, None,
                                                       rc.ComponentRanges(None, None, rc.Range3(1, 2, 3)))
        cases = [
            self.plan(pricing_basis=rc.PricingBasis.AUTHORITATIVE),
            self.plan(match_outcome=rc.MatchOutcome.MISMATCH),  # needs a reason and invalidated steps
            self.plan(match_outcome=rc.MatchOutcome.NOT_REPLAYABLE, match_reason_code="PRIOR_EVIDENCE_UNAVAILABLE"),  # must have no steps
            self.plan(steps=[completed_with_savings]),
            self.plan(replay_fingerprint="sha"),
            self.plan(remaining_upside_365d=rc.ComponentRanges(None, None, rc.Range3(1.0, 2.0, 3.0))),  # != last pending
            self.plan(steps=[rc.CarriedForwardStep(2, rc.TunableKnob.WAREHOUSE_SIZE, "T", "A", "B", invalid, "X", None)]),  # order not contiguous
        ]
        for bad in cases:
            with self.assertRaises(ValueError):
                bad.validate()
        # INVALIDATED needs a reason code
        with self.assertRaises(ValueError):
            rc.CarriedForwardStep(1, rc.TunableKnob.WAREHOUSE_SIZE, "T", "A", "B", invalid, None, None).validate()

    def test_not_replayable_is_valid_with_no_steps_and_null_upside(self):
        self.plan(match_outcome=rc.MatchOutcome.NOT_REPLAYABLE, match_reason_code="PRIOR_EVIDENCE_UNAVAILABLE", steps=[], remaining_upside_365d=None).validate()


class ReviewFindingTests(unittest.TestCase):
    """Fixes for the independent review of the Stage 1 contract change."""

    def reid(self, pkg):
        pkg = dataclasses.replace(pkg, recommendation_package_id=pkg.expected_package_id())
        return dataclasses.replace(pkg, provenance=dataclasses.replace(pkg.provenance, package_hash_full_sha256=pkg.full_package_hash()))

    def carried(self, pkg, **kw):
        base = dict(
            source_epoch_id=pkg.observed_change.previous_epoch_id, as_of="2026-09-29T00:00:00Z", match_policy=rc.MatchPolicy.EXACT_SINGLE_KNOB,
            match_outcome=rc.MatchOutcome.NOT_REPLAYABLE, match_reason_code="PRIOR_EVIDENCE_UNAVAILABLE", replay_fingerprint="SHA256:" + "A" * 64,
            steps=[], remaining_upside_365d=None, pricing_basis=rc.PricingBasis.CONTINGENT_CARRIED_FORWARD,
        )
        base.update(kw)
        return rc.CarriedForwardPlan(**base)

    def test_billing_only_wait_on_a_mature_epoch_is_not_called_immature(self):
        # Epoch is months old; only the billing-history gate is active (days still waiting to be ingested).
        pkg = build(golden_evidence("sparse-evidence", cost_history_days=6))  # no ready step in the recommended scenario
        self.assertEqual(pkg.recommended_plan.reevaluation.blocking_codes, ["COST_HISTORY_INSUFFICIENT"])
        self.assertEqual(pkg.recommended_plan.next_action, "WAIT_FOR_REEVALUATION")
        self.assertEqual(pkg.recommended_plan.selection_reason_code, "BILLING_HISTORY_ACCRUING")
        self.assertNotIn("too new", pkg.recommended_plan.decision_summary)
        young = build(golden_evidence("newly-changed-config"))
        self.assertEqual(young.recommended_plan.selection_reason_code, "CURRENT_EPOCH_IMMATURE")

    def test_earliest_time_rounds_up_to_the_next_second(self):
        self.assertEqual(rc.expected_earliest_reevaluation_time("2026-09-24T12:00:00.600000Z", ["CURRENT_CONFIG_EPOCH_TOO_NEW"]), "2026-10-01T12:00:01Z")
        self.assertEqual(rc.expected_earliest_reevaluation_time("2026-09-24T12:00:00Z", ["CURRENT_CONFIG_EPOCH_TOO_NEW"]), "2026-10-01T12:00:00Z")

    def test_carried_forward_requires_a_young_epoch_not_just_any_window(self):
        mature_cost_only = build(golden_evidence("spill-constrained", cost_history_days=3))
        bad = self.reid(dataclasses.replace(mature_cost_only, carried_forward_plan=self.carried(mature_cost_only)))
        with self.assertRaises(ValueError):
            bad.validate()
        young = build(golden_evidence("newly-changed-config"))
        self.reid(dataclasses.replace(young, carried_forward_plan=self.carried(young))).validate()

    def test_validator_is_as_strict_as_the_schema_for_the_reserved_block(self):
        young = build(golden_evidence("newly-changed-config"))
        for kw in ({"replay_fingerprint": "SHA256:" + "z" * 64}, {"replay_fingerprint": "SHA256:" + "a" * 64}, {"match_reason_code": "lower case"}):
            with self.assertRaises(ValueError):
                self.carried(young, **kw).validate()
        step = rc.CarriedForwardStep(1, rc.TunableKnob.WAREHOUSE_SIZE, "T", "A", "B", rc.CarriedForwardDisposition.INVALIDATED, "bad code", None)
        with self.assertRaises(ValueError):
            step.validate()

    def test_package_identity_binds_the_whole_carried_forward_block(self):
        young = build(golden_evidence("newly-changed-config"))
        a = dataclasses.replace(young, carried_forward_plan=self.carried(young))
        b = dataclasses.replace(young, carried_forward_plan=self.carried(young, as_of="2026-09-30T00:00:00Z"))  # same replay_fingerprint
        c = dataclasses.replace(young, carried_forward_plan=self.carried(young, match_reason_code="PRIOR_EVIDENCE_STALE"))
        self.assertNotEqual(a.expected_package_id(), b.expected_package_id())
        self.assertNotEqual(a.expected_package_id(), c.expected_package_id())
        self.assertNotEqual(a.expected_package_id(), young.expected_package_id())


class SchemaAndWorkbookTests(unittest.TestCase):
    def test_package_validates_against_the_schema_with_new_keys(self):
        import jsonschema
        schema = reporter_schema.build_schema()
        pkg = build(golden_evidence("newly-changed-config"))
        payload = json.loads(json.dumps(pkg.to_dict()))
        jsonschema.Draft202012Validator(schema).validate(payload)
        for key in ("observed_change", "carried_forward_plan"):
            self.assertIn(key, schema["required"])
            self.assertIn(key, payload)
        self.assertIn("reevaluation", payload["recommended_plan"])
        self.assertEqual(schema["properties"]["contract_version"], {"const": "1.2.0"})

    def test_schema_rejects_savings_on_observed_change_and_unknown_gate_codes(self):
        import jsonschema
        schema = reporter_schema.build_schema()
        payload = json.loads(json.dumps(build(golden_evidence("newly-changed-config")).to_dict()))
        validator = jsonschema.Draft202012Validator(schema)
        with_cost = copy.deepcopy(payload)
        with_cost["observed_change"]["savings_365d"] = 1.0
        self.assertFalse(validator.is_valid(with_cost))
        bad_code = copy.deepcopy(payload)
        bad_code["recommended_plan"]["reevaluation"]["blocking_codes"] = ["SOMETHING_ELSE"]
        self.assertFalse(validator.is_valid(bad_code))

    def test_committed_schema_matches_the_generator(self):
        committed = (ROOT / "schema" / "recommendation-package.schema.json").read_text()
        self.assertEqual(committed, json.dumps(reporter_schema.build_schema(), indent=2, sort_keys=True) + "\n")

    def test_workbook_portfolio_projects_the_new_columns(self):
        keys = [c.key for c in wc.PORTFOLIO_COLUMNS]
        new = ["earliest_reevaluation_time", "reevaluation_blockers", "observed_change"]
        self.assertEqual(keys[keys.index("requires_hitl") + 1: keys.index("requires_hitl") + 4], new)
        group = next(g for g in wc.PORTFOLIO_GROUPS if g.title == "Recommended plan")
        self.assertEqual(group.end_key, "observed_change")
        young = build(golden_evidence("newly-changed-config"))
        row = wc.project_warehouse_portfolio(young)
        self.assertEqual(row["earliest_reevaluation_time"], "2026-10-06T00:00:00Z")
        self.assertEqual(row["reevaluation_blockers"], "CURRENT_CONFIG_EPOCH_TOO_NEW")
        self.assertEqual(row["observed_change"], "warehouse_size MEDIUM→LARGE (2026-09-29)")
        mature = wc.project_warehouse_portfolio(build(golden_evidence("spill-constrained")))
        self.assertEqual(mature["earliest_reevaluation_time"], wc.DISPLAY_NA)
        self.assertEqual(mature["reevaluation_blockers"], wc.DISPLAY_NA)
        for value in row.values():
            self.assertNotEqual(value, "")  # the workbook never shows a blank semantic value


if __name__ == "__main__":
    unittest.main()
