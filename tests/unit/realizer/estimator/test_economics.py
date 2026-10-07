"""Estimator economics (src/realizer/estimator/economics.py): the functions and the measurement facts.

``make_cf_id``, ``expected_annual_for_exact_step`` and ``calculate_economics`` give the results (and raise the
exceptions on malformed input) stored in ``tests/fixtures/realizer/expectations/`` over every application the
41 scenarios recorded and over grids of economics, readiness, verdicts and policies. The realization classes,
denominators, period, AWS components, trailing-twelve-month savings, unavailable economics, guardrail failure and the
counterfactual that cannot support a claim are also pinned to explicit values. ``counterfactual_record``,
``measurement_facts`` and ``correction_facts`` build the ledger payloads (compared entry for entry in
``test_estimator``); their fallbacks are pinned here.
"""
import copy
import itertools
import json
import unittest
from decimal import Decimal
from pathlib import Path

from tests import realizer_expect as expect
from tests.unit.realizer.helpers import stage_policy, Opaque, Unprintable, canonical, fixture_package, outcome  # noqa: E402
from realizer.analyzer.analysis import candidate_steps  # noqa: E402
from realizer.governor.policy import authority_eligible, counterfactual_eligible  # noqa: E402
from realizer.analyzer.collector.collector import demo_observation  # noqa: E402
from realizer.config.settings import MODELER_GATEWAY_VERSION  # noqa: E402
from realizer.contracts.realization import make_measurement_id, make_rpkg_id  # noqa: E402
from realizer.contracts.realization import EventType  # noqa: E402
from realizer.contracts.request import RealizationRequest  # noqa: E402
from realizer.estimator import economics  # noqa: E402
from realizer.governor.policy import RealizerPolicy  # noqa: E402
from realizer.packager.ledger import make_ledger_entry  # noqa: E402
from realizer.realizer import build_context  # noqa: E402
from realizer.analyzer import analyzer  # noqa: E402
from realizer.governor import governor  # noqa: E402
from realizer.reconciler import reconciler  # noqa: E402

APPLICATION_EVENTS = {"APPLICATION_OBSERVED", "MODIFIED_APPLICATION_OBSERVED", "INDEPENDENT_CHANGE_OBSERVED"}
POLICIES = [None, {"realized_ratio_min": "0.95", "under_realized_ratio_min": "0.7", "minimum_days": 1, "require_representative_regime": False}]


def policy(mapping):
    return stage_policy(mapping)


def stored_applications():
    """``(package, application payload)`` for every distinct application the stored scenario outputs recorded."""
    seen = set()
    for name in canonical.scenario_names():
        package = canonical.load_scenario(name)["package_doc"]
        for path in sorted((canonical.EXPECTED / name / "realization" / "ledger").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["event_type"] in APPLICATION_EVENTS:
                    key = (package["recommendation_package_id"], json.dumps(entry["payload"], sort_keys=True))
                    if key not in seen:
                        seen.add(key)
                        yield package, entry["payload"]


def demo_application(package, observation):
    context = build_context(RealizationRequest(package, observation, Path("/nonexistent-out")))
    analysis = analyzer.run(context)
    return reconciler.run(context, analysis, governor.run(context, analysis)).active.payload


class CounterfactualIdTests(unittest.TestCase):
    def test_cf_id_for_every_application_observation_and_policy(self):
        observations = [run["observation"] for name in canonical.scenario_names() for run in canonical.load_scenario(name)["runs"] if run.get("observation")]
        results = []
        for (package, app), mapping in itertools.product(list(stored_applications())[:12], POLICIES):
            for observation in observations[::4]:
                method = counterfactual_eligible(observation)[1]
                results.append(economics.make_cf_id(app["application_episode_id"], observation, package, policy(mapping), method))
        self.assertGreater(len(results), 100)
        self.assertTrue(all(r.startswith("CF-") for r in results))
        expect.check(self, "ids", results, spot={"count": len(results), "distinct": len(set(results)), "first": results[0]})

    def test_cf_id_failures(self):
        package = fixture_package()
        base = demo_observation(package)
        broken_package = copy.deepcopy(package)
        broken_package["current_config"] = 5
        cases = [
            ("evidence", {**base, "economics": {**base["economics"], "dbu_realized": Opaque()}}, package, None),
            ("window", {**base, "window_end_utc": Opaque()}, package, None),
            ("observation", 5, package, None),
            ("package", base, broken_package, None),
            ("policy", base, package, {"realized_ratio_min": "NaN"}),
        ]
        for label, observation, pkg, mapping in cases:
            with self.subTest(case=label):
                new = outcome(economics.make_cf_id, "APP-X", observation, pkg, policy(mapping), "MATCHED_HISTORICAL")
                self.assertNotEqual(new[0], "ok")
                expect.check(self, label, new)


class CounterfactualIdentityTests(unittest.TestCase):
    """The CF id names what the evidence states about the counterfactual: the method, the window (its start falling back to
    the applied time, its end to the observed time), the replay target, the measurement's assumptions and a fingerprint
    of the economics and measurement. The caller's ``workload`` is informational and enters no identity."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)
        cls.policy = RealizerPolicy()

    def cf(self, observation, method="MATCHED_HISTORICAL"):
        return economics.make_cf_id("APP-X", observation, self.pkg, self.policy, method)

    def test_two_collections_of_one_window_with_different_costs_get_different_ids(self):
        other = {**self.base, "economics": {**self.base["economics"], "counterfactual_old_cost": "10500.00"}}
        self.assertNotEqual(self.cf(self.base), self.cf(other))
        components = {**self.base, "economics": {**self.base["economics"], "dbu_realized": "2613.12"}}
        self.assertNotEqual(self.cf(self.base), self.cf(components))

    def test_the_workload_enters_no_identity(self):
        self.assertEqual(self.cf(self.base), self.cf({**self.base, "workload": {"queries": 12345, "mix": ["etl"]}}))

    def test_the_method_is_hashed(self):
        self.assertNotEqual(self.cf(self.base, "MATCHED_HISTORICAL"), self.cf(self.base, "NO_DEFENSIBLE_COUNTERFACTUAL"))

    def test_a_window_without_a_start_hashes_the_applied_time(self):
        without = {**self.base, "window_start_utc": None}
        at_applied = {**self.base, "window_start_utc": self.base["applied_at_utc"]}
        self.assertEqual(self.cf(without), self.cf(at_applied))
        self.assertNotEqual(self.cf(without), self.cf(self.base))
        # and a window without an end hashes the observed time
        self.assertEqual(self.cf({**self.base, "window_end_utc": None}), self.cf({**self.base, "window_end_utc": self.base["observed_at_utc"]}))

    def test_replay_inputs_are_hashed(self):
        measured = {**self.base, "measurement": {"source_value": 30, "applied_value": 10, "cost_basis": "DBU_AND_MODELED_AWS",
                                                 "replay": {"replayed_target_minutes": 30, "restart_cost_cluster_minutes": "4"}}}
        changed = copy.deepcopy(measured)
        changed["measurement"]["replay"]["restart_cost_cluster_minutes"] = "8"
        self.assertNotEqual(self.cf(measured), self.cf(changed))
        self.assertNotEqual(self.cf(measured), self.cf(self.base))

    def test_the_payload_states_what_the_id_hashes(self):
        measured = {**self.base, "window_start_utc": None,
                    "measurement": {"source_value": 30, "applied_value": 10, "cost_basis": "DBU_ONLY",
                                    "replay": {"replayed_target_minutes": 30, "restart_cost_cluster_minutes": "4"}}}
        record = economics.counterfactual_record("APP-X", measured, self.pkg, self.policy, True, "CALIBRATED_PRIOR_STATE")
        self.assertEqual(record.counterfactual_id, self.cf(measured, "CALIBRATED_PRIOR_STATE"))
        payload = record.payload
        self.assertEqual(payload["window"], {"start_utc": self.base["applied_at_utc"], "end_utc": self.base["window_end_utc"]})
        self.assertEqual(payload["replay_target"], 30)
        self.assertEqual(payload["assumptions"], {"source_value": 30, "applied_value": 10, "cost_basis": "DBU_ONLY", "restart_cost_cluster_minutes": "4"})
        self.assertRegex(payload["evidence_fingerprint"], r"^[0-9A-F]{64}$")
        caller = economics.counterfactual_record("APP-X", self.base, self.pkg, self.policy, True, "MATCHED_HISTORICAL").payload
        self.assertEqual((caller["replay_target"], caller["assumptions"]), (None, None))
        self.assertNotIn("post_change_workload", caller)


class ExpectedAnnualTests(unittest.TestCase):
    def test_every_plan_step_of_every_fixture_package(self):
        results = {}
        for path in sorted(canonical.PACKAGES.glob("*.json")):
            package = json.loads(path.read_text(encoding="utf-8"))
            for scenario in package["scenarios"]:
                for step in scenario.get("execution_plan", []):
                    results[f"{path.stem}/{scenario['scenario_id']}/{step.get('step')}"] = economics.expected_annual_for_exact_step(package, step)
        self.assertGreater(len(results), 10)
        expect.check(self, "steps", results)

    def test_candidate_economics_shapes(self):
        package = fixture_package()
        step = {"candidate_id": "CAND-T"}
        shapes = {
            "total": {"savings_365d": {"total": {"base": "120.5"}, "dbu": {"base": "7"}}},
            "dbu fallback": {"savings_365d": {"total": None, "dbu": {"base": "7"}}},
            "no base": {"savings_365d": {"total": {"low": "1"}}},
            "non-mapping total": {"savings_365d": {"total": "120"}},
            "no savings": {"savings_365d": None},
            "no economics": None,
            "malformed base": {"savings_365d": {"total": {"base": "abc"}}},
            "non-finite base": {"savings_365d": {"total": {"base": "Infinity"}}},
            "non-mapping savings": {"savings_365d": 5},
        }
        for label, econ in shapes.items():
            with self.subTest(shape=label):
                pkg = copy.deepcopy(package)
                pkg["candidates"] = [{"candidate_id": "CAND-T", "independent_economics": econ}]
                expect.check(self, label, outcome(economics.expected_annual_for_exact_step, pkg, step))
        pkg = copy.deepcopy(package)
        pkg["candidates"] = [{"candidate_id": "CAND-T", "independent_economics": shapes["total"]}]
        self.assertEqual(economics.expected_annual_for_exact_step(pkg, step), (Decimal("120.5"), "PUBLISHER_CANDIDATE"))
        pkg["candidates"][0]["independent_economics"] = shapes["dbu fallback"]
        # A missing total is unavailable; the DBU range is used only when the DBU component is asked for.
        self.assertEqual(economics.expected_annual_for_exact_step(pkg, step), (None, "UNAVAILABLE"))
        self.assertEqual(economics.expected_annual_for_exact_step(pkg, step, "dbu"), (Decimal("7"), "PUBLISHER_CANDIDATE"))
        self.assertEqual(economics.expected_annual_for_exact_step(pkg, {"candidate_id": "CAND-NONE"}), (None, "UNAVAILABLE"))
        self.assertEqual(economics.expected_annual_for_exact_step(pkg, {}), (None, "UNAVAILABLE"))


class CalculateEconomicsGridTests(unittest.TestCase):
    """``calculate_economics`` over applications x economics x readiness x verdicts x policies."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)
        cls.app = demo_application(cls.pkg, cls.base)

    def apps(self):
        yield from stored_applications()
        for conformance, scope, step in itertools.product(("EXACT", "MODIFIED", "CONTRARY", "UNRELATED"), (None, "SCENARIO"), (1, 2, 99)):
            app = {**self.app, "implementation_conformance": conformance, "step": step}
            if scope:
                app["application_scope"] = scope
            yield self.pkg, app

    def economics_variants(self):
        econ = self.base["economics"]
        return [
            econ,
            {**econ, "counterfactual_old_cost": None},
            {**econ, "observed_new_cost": None},
            {**econ, "counterfactual_old_cost": "9000", "observed_new_cost": "10000"},
            {**econ, "observed_new_cost": "10000"},
            {**econ, "period_days": None},
            {**econ, "period_days": 0},
            {**econ, "period_days": "365"},
            {**econ, "expected_annual_savings_actual_scope": "12000"},
            {**econ, "expected_annual_savings_actual_scope": "20000"},
            {**econ, "expected_annual_savings_actual_scope": "30000"},
            {**econ, "expected_annual_savings_actual_scope": "0"},
            {**econ, "expected_annual_savings_actual_scope": "-5"},
            {**econ, "currency": None, "economic_basis": None, "mature_ttm_realized_savings": "4321.005",
             "realized_savings_since_application": "2500", "dbu_realized": "400", "aws_economic_realized": "350.555",
             "aws_cash_realized": 0, "aws_commitment_freed": "12", "aws_evidence_type": "CUR"},
        ]

    def test_grid(self):
        observations = [self.base, {**self.base, "elapsed_days": 1}, {**self.base, "elapsed_days": None, "query_count": 5000}]
        results = []
        classes = set()
        for (package, app), econ, obs, passed, eligible, mapping in itertools.product(
                list(self.apps()), self.economics_variants(), observations, (True, False), (True, False), POLICIES):
            observation = {**obs, "economics": econ}
            new = outcome(economics.calculate_economics, package, app, observation, policy(mapping), passed, eligible)
            results.append(new)
            if new[0] == "ok":
                classes.add((new[1].get("realization_state"), new[1]["classification_detail"], new[1]["denominator_source"]))
        self.assertGreater(len(results), 10000)
        states = {c[0] for c in classes}
        details = {c[1] for c in classes}
        sources = {c[2] for c in classes}
        self.assertEqual(states, {None, "REALIZED", "UNDER_REALIZED", "VALIDATION_FAILED", "BLOCKED", "MONITORING"})
        self.assertIn("UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED", details)
        self.assertEqual(sources, {"UNAVAILABLE", "PUBLISHER_CANDIDATE", "PUBLISHER_SCENARIO", "REESTIMATED_APPLIED_SCOPE", "NOT_APPLICABLE"})
        expect.check(self, "results", results, spot={"count": len(results), "failures": sum(1 for r in results if r[0] != "ok"),
                                                     "classes": sorted(map(list, ((str(a), b, c) for a, b, c in classes)))})

    def test_malformed_input_raises_the_stored_exception(self):
        econ = self.base["economics"]
        bad_plan = copy.deepcopy(self.pkg)
        bad_plan["scenarios"][0]["execution_plan"][0]["step"] = "abc"
        cases = {
            "economics not a mapping": ({"economics": 5}, self.app, self.pkg),
            "cost not a number": ({"economics": {**econ, "observed_new_cost": "abc"}}, self.app, self.pkg),
            "cost not finite": ({"economics": {**econ, "counterfactual_old_cost": "NaN"}}, self.app, self.pkg),
            "period not a number": ({"economics": {**econ, "period_days": "abc"}}, self.app, self.pkg),
            "period absent": ({"economics": {k: v for k, v in econ.items() if k != "period_days"}}, self.app, self.pkg),
            "currency absent": ({"economics": {k: v for k, v in econ.items() if k != "currency"}}, self.app, self.pkg),
            "expected not a number": ({"economics": {**econ, "expected_annual_savings_actual_scope": "abc"}}, self.app, self.pkg),
            "ttm not a number": ({"economics": {**econ, "mature_ttm_realized_savings": "abc"}}, self.app, self.pkg),
            "aws not finite": ({"economics": {**econ, "aws_cash_realized": "Infinity"}}, self.app, self.pkg),
            "currency unprintable": ({"economics": {**econ, "currency": Unprintable()}}, self.app, self.pkg),
            "application step": ({}, {**self.app, "step": "x"}, self.pkg),
            "plan step": ({}, self.app, bad_plan),
            "negative period": ({"economics": {**econ, "period_days": "-30"}}, self.app, self.pkg),
            "readiness": ({"economics": econ, "query_count": "abc"}, self.app, self.pkg),
        }
        for label, (patch, app, package) in cases.items():
            with self.subTest(case=label):
                observation = {**self.base, **patch}
                new = outcome(economics.calculate_economics, package, app, observation, policy(None), True, True)
                expect.check(self, label, new)
        # a negative period is not an error: it only suppresses annualization
        negative = economics.calculate_economics(self.pkg, self.app, {**self.base, "economics": {**econ, "period_days": "-30"}}, RealizerPolicy(), True, True)
        self.assertIsNone(negative["annualized_realized_savings"])


class EconomicsCaseTests(unittest.TestCase):
    """Explicit economics, pinned to values."""

    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)
        cls.app = demo_application(cls.pkg, cls.base)
        # The demo observation realizes, over its 30 days, the applied step's published savings pro rata.
        econ = cls.base["economics"]
        cls.realized = Decimal(econ["counterfactual_old_cost"]) - Decimal(econ["observed_new_cost"])
        cls.annualized = (cls.realized * 365 / 30).quantize(Decimal("0.01"))
        step = next(s for s in candidate_steps(cls.pkg) if s.get("step") == cls.app["step"])
        cls.published = economics.expected_annual_for_exact_step(cls.pkg, step)[0]

    def econ(self, app=None, passed=True, eligible=True, policy=None, observation=None, **economics_patch):
        observation = {**(observation or self.base)}
        observation["economics"] = {**self.base["economics"], **economics_patch}
        return economics.calculate_economics(self.pkg, app or self.app, observation, policy or RealizerPolicy(), passed, eligible)

    def test_ratio_classes(self):
        # The annualized realized savings against a re-estimate that is 0.99x (realized), 1.64x (under) and 2.47x (far under) as large.
        cases = {"0.99": ("REALIZED", "REALIZED", False), "1.64": ("UNDER_REALIZED", "UNDER_REALIZED", False),
                 "2.47": ("UNDER_REALIZED", "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED", True)}
        for factor, (state, detail, reoptimize) in cases.items():
            expected = str((self.annualized * Decimal(factor)).quantize(Decimal("0.01")))
            with self.subTest(expected=expected):
                result = self.econ(expected_annual_savings_actual_scope=expected)
                self.assertEqual((result["realization_state"], result["classification_detail"], result["reoptimization_recommended"]), (state, detail, reoptimize))
                self.assertEqual(result["annualized_realized_savings"], str(self.annualized))
                self.assertEqual(result["successful_realized_savings"], str(self.realized))
                self.assertEqual(Decimal(result["realization_ratio"]), self.realized * 365 / 30 / Decimal(expected))
        near = str((self.annualized * Decimal("1.0277")).quantize(Decimal("0.01")))   # ratio 0.973: realized by default, under at a 0.99 minimum
        self.assertEqual(self.econ(expected_annual_savings_actual_scope=near)["realization_state"], "REALIZED")
        self.assertEqual(self.econ(expected_annual_savings_actual_scope=near, policy=stage_policy({"realized_ratio_min": "0.99"}))["realization_state"], "UNDER_REALIZED")
        for expected in ("0", "-5"):
            result = self.econ(expected_annual_savings_actual_scope=expected)
            self.assertEqual((result["realization_state"], result["classification_detail"], result["realization_ratio"]), ("MONITORING", "VALIDATED_VALUE_UNCLASSIFIED", None))

    def test_denominators(self):
        self.assertEqual(self.econ()["denominator_source"], "PUBLISHER_CANDIDATE")
        self.assertEqual(self.econ()["expected_same_scope_annual_savings"], str(self.published))
        self.assertEqual(self.econ(expected_annual_savings_actual_scope="15000")["denominator_source"], "REESTIMATED_APPLIED_SCOPE")
        modified = {**self.app, "implementation_conformance": "MODIFIED"}
        self.assertEqual(self.econ(app=modified)["denominator_source"], "UNAVAILABLE")
        self.assertEqual(self.econ(app=modified, expected_annual_savings_actual_scope="15000")["denominator_source"], "REESTIMATED_APPLIED_SCOPE")
        scenario = {**self.app, "application_scope": "SCENARIO"}
        # A scenario-scope application is measured against the steps it applied: none named, no denominator.
        self.assertEqual(self.econ(app=scenario)["denominator_source"], "UNAVAILABLE")
        eligible = [st["step"] for st in candidate_steps(self.pkg, self.app["scenario_id"]) if authority_eligible(st)]
        refs = [{"scenario_id": self.app["scenario_id"], "step": n, "conformance": "EXACT"} for n in eligible[:2]]
        self.assertEqual(self.econ(app={**scenario, "applied_step_refs": refs})["denominator_source"], "PUBLISHER_SCENARIO")
        self.assertEqual(self.econ(app={**scenario, "implementation_conformance": "MODIFIED"})["denominator_source"], "UNAVAILABLE")
        self.assertEqual(self.econ(app={**self.app, "step": 99})["denominator_source"], "UNAVAILABLE")
        self.assertEqual(self.econ(app={**self.app, "implementation_conformance": "CONTRARY"}, expected_annual_savings_actual_scope="1")["denominator_source"], "UNAVAILABLE")

    def test_period_and_annualization(self):
        self.assertEqual(self.econ()["period_days"], "30")
        year = self.econ(counterfactual_old_cost="150000", observed_new_cost="120000", period_days=365, expected_annual_savings_actual_scope="30000")
        self.assertEqual((year["annualized_realized_savings"], year["realization_ratio"], year["realization_state"]), ("30000.00", "1", "REALIZED"))
        # The measurement period is required; it is never replaced by the observation's elapsed days.
        with self.assertRaises(TypeError):
            self.econ(period_days=None)
        econ = {k: v for k, v in self.base["economics"].items() if k != "period_days"}
        with self.assertRaises(KeyError):
            economics.calculate_economics(self.pkg, self.app, {**self.base, "economics": econ}, RealizerPolicy(), True, True)
        zero = self.econ(period_days=0)
        self.assertEqual((zero["period_days"], zero["annualized_realized_savings"], zero["realization_state"]), ("0", None, "MONITORING"))
        missing_currency = {k: v for k, v in self.base["economics"].items() if k != "currency"}
        with self.assertRaises(KeyError):
            economics.calculate_economics(self.pkg, self.app, {**self.base, "economics": missing_currency}, RealizerPolicy(), True, True)
        unrepresentative = self.econ(observation={**self.base, "representative_regime": False})
        self.assertIsNone(unrepresentative["annualized_realized_savings"])
        self.assertEqual((unrepresentative["realization_state"], unrepresentative["realization_ratio"]), ("MONITORING", None))

    def test_cost_components_stay_separate_and_unknown_stays_null(self):
        result = self.econ(dbu_realized="400", aws_economic_realized="350.555", aws_cash_realized=0, aws_commitment_freed="12", aws_evidence_type="CUR")
        self.assertEqual({k: result[k] for k in ("dbu_realized", "aws_economic_realized", "aws_cash_realized", "aws_commitment_freed", "aws_evidence_type")},
                         {"dbu_realized": "400.00", "aws_economic_realized": "350.56", "aws_cash_realized": "0.00", "aws_commitment_freed": "12.00", "aws_evidence_type": "CUR"})
        bare = self.econ()
        for key in ("dbu_realized", "aws_economic_realized", "aws_cash_realized", "aws_commitment_freed", "aws_evidence_type", "mature_ttm_realized_savings"):
            self.assertIsNone(bare[key], key)

    def test_trailing_twelve_months_and_since_application(self):
        result = self.econ(mature_ttm_realized_savings="4321.005", realized_savings_since_application="2500")
        self.assertEqual((result["mature_ttm_realized_savings"], result["realized_savings_since_application"]), ("4321.01", "2500.00"))
        self.assertEqual(self.econ()["realized_savings_since_application"], str(self.realized))

    def test_cost_increase(self):
        result = self.econ(counterfactual_old_cost="9000", observed_new_cost="10000")
        self.assertEqual((result["observed_economic_delta"], result["successful_realized_savings"]), ("-1000.00", "-1000.00"))
        self.assertTrue(Decimal(result["realization_ratio"]) < 0)
        self.assertEqual(result["classification_detail"], "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED")

    def test_unavailable_economics(self):
        for patch in ({"counterfactual_old_cost": None}, {"observed_new_cost": None}):
            with self.subTest(patch=patch):
                result = self.econ(currency="EUR", **patch)
                self.assertEqual(result, {
                    "available": False, "currency": "EUR", "observed_economic_delta": None, "successful_realized_savings": None,
                    "annualized_realized_savings": None, "expected_same_scope_annual_savings": None, "denominator_source": "UNAVAILABLE",
                    "realization_ratio": None, "classification_detail": "ECONOMICS_UNAVAILABLE",
                })

    def test_guardrail_failure_and_counterfactual_without_claim(self):
        failed = self.econ(passed=False)
        self.assertEqual((failed["realization_state"], failed["classification_detail"], failed["successful_realized_savings"], failed["observed_economic_delta"]),
                         ("VALIDATION_FAILED", "HARD_GUARDRAIL_FAILED", "0.00", str(self.realized)))
        blocked = self.econ(eligible=False)
        self.assertEqual((blocked["realization_state"], blocked["classification_detail"], blocked["successful_realized_savings"], blocked["observed_economic_delta"]),
                         ("BLOCKED", "COUNTERFACTUAL_NOT_CLAIM_ELIGIBLE", "0.00", str(self.realized)))
        self.assertEqual(self.econ(passed=False, eligible=False)["realization_state"], "VALIDATION_FAILED")
        # A failed or blocked measurement keeps its observed delta but publishes no run-rate and no ratio.
        for result in (failed, blocked):
            self.assertIsNone(result["annualized_realized_savings"])
            self.assertIsNone(result["realization_ratio"])
        for result in (self.econ(), self.econ(expected_annual_savings_actual_scope="1000000")):
            self.assertIsNotNone(result["annualized_realized_savings"])
            self.assertIsNotNone(result["realization_ratio"])
        monitoring = self.econ(expected_annual_savings_actual_scope="0")
        self.assertEqual(monitoring["realization_state"], "MONITORING")
        self.assertIsNotNone(monitoring["annualized_realized_savings"])


class MeasurementFactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = fixture_package()
        cls.base = demo_observation(cls.pkg)

    def test_counterfactual_record_with_and_without_the_counterfactual_cost(self):
        policy = RealizerPolicy()
        record = economics.counterfactual_record("APP-X", self.base, self.pkg, policy, True, "MATCHED_HISTORICAL")
        cf_id = economics.make_cf_id("APP-X", self.base, self.pkg, policy, "MATCHED_HISTORICAL")
        basis = economics.counterfactual_basis(self.base)
        self.assertEqual(record.payload, {"counterfactual_id": cf_id, "method": "MATCHED_HISTORICAL", "claim_eligible": True,
                                          "workload_basis": "ACTUAL_POST_CHANGE_WORKLOAD", "counterfactual_old_cost": "10000.00",
                                          "modeler_version": MODELER_GATEWAY_VERSION,
                                          "window": {"start_utc": self.base["window_start_utc"], "end_utc": self.base["window_end_utc"]},
                                          "replay_target": None, "assumptions": None, "evidence_fingerprint": basis["evidence_fingerprint"]})
        without = {**self.base, "economics": {**self.base["economics"], "counterfactual_old_cost": None}}
        record = economics.counterfactual_record("APP-X", without, self.pkg, policy, False, "NO_DEFENSIBLE_COUNTERFACTUAL")
        self.assertEqual((record.counterfactual_id, record.payload, record.claim_eligible), (economics.make_cf_id("APP-X", without, self.pkg, policy, "NO_DEFENSIBLE_COUNTERFACTUAL"), None, False))

    def test_measurement_window_falls_back_to_the_observation_times(self):
        record = economics.counterfactual_record("APP-X", self.base, self.pkg, RealizerPolicy(), True, "M")
        facts = economics.measurement_facts("APP-X", {**self.base, "window_start_utc": None, "window_end_utc": ""}, record, {"realization_state": "REALIZED"}, {},
                                            effective="2026-10-01T12:00:00Z", observed_at="2026-10-09T12:00:00Z")
        self.assertEqual((facts.window_start_utc, facts.window_end_utc), ("2026-10-01T12:00:00Z", "2026-10-09T12:00:00Z"))
        self.assertEqual(facts.measurement_id, make_measurement_id("APP-X", {**self.base, "window_start_utc": None, "window_end_utc": ""}, record.counterfactual_id, "ECONOMIC_COST"))
        self.assertEqual((facts.realization_state, facts.mature_ttm_realized_savings, facts.correction_status), ("REALIZED", None, "ORIGINAL"))

    def test_correction_facts(self):
        """The correction of a measurement against the ledger history: a window measured earlier with other economics is
        corrected (with or without a correction object), the same economics are not; a new window is a correction only with
        a correction object, which falls back to the latest earlier measurement; a key and version already recorded is
        not recorded again."""
        from realizer.contracts.realization import MeasurementFacts
        rpkg = make_rpkg_id(self.pkg["recommendation_package_id"])

        def measured(mid, start="2026-10-02T12:00:00Z", end="2026-10-09T12:00:00Z", economics_=None, app="APP-" + "A" * 24):
            return MeasurementFacts(measurement_id=mid, window_start_utc=start, window_end_utc=end, counterfactual_id="CF-X",
                                    counterfactual_method="M", economics=economics_ or {"successful_realized_savings": "1.00"},
                                    attribution={}, realization_state="REALIZED", mature_ttm_realized_savings=None, correction_status="ORIGINAL")

        def entry(m, app="APP-" + "A" * 24, event=EventType.ECONOMIC_MEASUREMENT_RECORDED, payload=None):
            return make_ledger_entry(package=self.pkg, rpkg_id=rpkg, event_type=event, effective_at_utc="2026-10-09T12:00:00Z",
                                     recorded_at_utc="2026-10-09T12:00:00Z", application_episode_id=app,
                                     measurement_id=m.measurement_id if event == EventType.ECONOMIC_MEASUREMENT_RECORDED else None,
                                     payload=payload or {"window_start_utc": m.window_start_utc, "window_end_utc": m.window_end_utc, "economics": m.economics,
                                                        "attribution": m.attribution, "realization_state": m.realization_state,
                                                        "mature_ttm_realized_savings": m.mature_ttm_realized_savings})

        app = "APP-" + "A" * 24
        first = measured("MEAS-" + "1" * 24)
        prior = entry(first)
        restated = measured("MEAS-" + "2" * 24, economics_={"successful_realized_savings": "0.80"})
        # a silent restatement of the same window
        facts = economics.correction_facts(self.base, restated, app, [prior])
        self.assertEqual(facts.payload, {"correction_key": restated.measurement_id, "type": "RESTATEMENT", "version": None,
                                         "reason": economics.REMEASURED_REASON, "measurement_id": restated.measurement_id,
                                         "corrects_entry_id": prior.ledger_entry_id})
        # a supplied correction keeps its key, type, version and reason, and names the measurement of the window
        facts = economics.correction_facts({**self.base, "correction": {"key": "k", "type": "retraction", "version": 3, "reason": "r", "corrects_entry_id": None}}, restated, app, [prior])
        self.assertEqual((facts.payload["correction_key"], facts.payload["type"], facts.payload["version"], facts.payload["reason"], facts.corrects_entry_id),
                         ("k", "RETRACTION", 3, "r", prior.ledger_entry_id))
        # the same economics, another application episode, or reprocessing: no correction of that window
        self.assertIsNone(economics.correction_facts(self.base, measured("MEAS-" + "3" * 24), app, [prior]))
        self.assertIsNone(economics.correction_facts(self.base, restated, "APP-" + "B" * 24, [prior]))
        self.assertTrue(economics.repeats_earlier_measurement(measured("MEAS-" + "3" * 24), app, [prior]))
        self.assertFalse(economics.repeats_earlier_measurement(restated, app, [prior]))
        self.assertIsNone(economics.correction_facts(self.base, first, app, [prior]))
        # a new window: only a correction object, falling back to the latest earlier measurement
        other = measured("MEAS-" + "4" * 24, start="2026-10-03T12:00:00Z", end="2026-10-10T12:00:00Z")
        self.assertIsNone(economics.correction_facts(self.base, other, app, [prior]))
        self.assertIsNone(economics.correction_facts({**self.base, "correction": {}}, other, app, [prior]))
        facts = economics.correction_facts({**self.base, "correction": {"version": 3}}, other, app, [prior])
        self.assertEqual(facts.payload, {"correction_key": other.measurement_id, "type": "RESTATEMENT", "version": 3, "reason": None,
                                         "measurement_id": other.measurement_id, "corrects_entry_id": prior.ledger_entry_id})
        explicit = economics.correction_facts({**self.base, "correction": {"key": "k", "type": "credit", "reason": "r", "corrects_entry_id": "RLE-X"}}, other, app, [])
        self.assertEqual((explicit.payload["correction_key"], explicit.payload["type"], explicit.payload["reason"], explicit.corrects_entry_id), ("k", "CREDIT", "r", "RLE-X"))
        self.assertIsNone(economics.correction_facts({**self.base, "correction": {"type": "x"}}, other, app, []).corrects_entry_id)
        # one correction per key and version
        recorded = entry(first, event=EventType.BILLING_CORRECTION_RECORDED, payload={"correction_key": "k", "version": 3})
        self.assertIsNone(economics.correction_facts({**self.base, "correction": {"key": "k", "version": 3}}, other, app, [prior, recorded]))
        self.assertIsNotNone(economics.correction_facts({**self.base, "correction": {"key": "k", "version": 4}}, other, app, [prior, recorded]))
        self.assertEqual(outcome(economics.correction_facts, {"correction": {"type": Unprintable()}}, other, app, []), ("ValueError", "unprintable observation value"))

if __name__ == "__main__":
    unittest.main()
