"""The cost components of an observation's economics (src/realizer/config/loader.py ``validate_observation``, and the
compared component in src/realizer/estimator/economics.py ``calculate_economics``).

When an observation states both costs, its realized total is ``counterfactual_old_cost - observed_new_cost``, and the
components it gives must add up to it within the Reporter contract's 0.03 tolerance: ``dbu_realized`` alone is the
total; ``dbu_realized + aws_economic_realized`` is the total. An AWS component needs a DBU component and an evidence
type, and an AWS cash or freed-commitment amount needs ``CUR_ACTUAL`` evidence (modeled AWS is never cash). A value
that is not a finite number is refused naming the field. An observation that gives only a DBU component, without a
measurement cost basis, is compared with the published DBU range. The live projector's observations pass these checks.
"""
import copy
import unittest
from decimal import Decimal

from tests import realizer_world as world
from tests.unit.realizer.helpers import ROOT  # noqa: F401  (puts src on the path)
from realizer.analyzer.collector.collector import demo_observation
from realizer.analyzer.projector import project_observation
from realizer.common.errors import InputError
from realizer.config import loader
from realizer.estimator.economics import calculate_economics
from realizer.governor.policy import RealizerPolicy

PRO = world.package("autostop-scoped-pro")


def with_economics(base, **patch):
    """*base* with a realized total of 1,000.00 (10,000.00 counterfactual, 9,000.00 observed) and *patch* applied."""
    observation = copy.deepcopy(base)
    observation["economics"].update({"counterfactual_old_cost": "10000.00", "observed_new_cost": "9000.00", "dbu_realized": None,
                                     "aws_economic_realized": None, "aws_cash_realized": None, "aws_commitment_freed": None,
                                     "aws_evidence_type": None})
    observation["economics"].update(patch)
    return observation


class ComponentValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkg = world.package("inplace-size-canary")
        cls.base = demo_observation(cls.pkg)

    def accepted(self, **patch):
        loader.validate_observation(with_economics(self.base, **patch), self.pkg)

    def refused(self, message, **patch):
        with self.assertRaises(InputError) as caught:
            loader.validate_observation(with_economics(self.base, **patch), self.pkg)
        self.assertIn(message, str(caught.exception))
        for value in patch.values():
            if isinstance(value, str) and value not in ("MODELED", "CUR_ACTUAL"):
                self.assertNotIn(value, str(caught.exception), "the message names the field, never the value")
        return str(caught.exception)

    def test_no_component_is_not_checked(self):
        self.accepted()

    def test_components_must_add_up_to_the_realized_total(self):
        # 5,000 DBU + 400 AWS claims 5,400 of a 1,000 realization.
        self.refused("dbu_realized + aws_economic_realized", dbu_realized="5000", aws_economic_realized="400", aws_evidence_type="MODELED")
        self.accepted(dbu_realized="600", aws_economic_realized="400", aws_evidence_type="MODELED")
        self.accepted(dbu_realized="600.01", aws_economic_realized="400.02", aws_evidence_type="MODELED")  # 1,000.03: at the tolerance
        self.refused("dbu_realized + aws_economic_realized", dbu_realized="600.02", aws_economic_realized="400.02", aws_evidence_type="MODELED")

    def test_a_dbu_component_alone_is_the_total(self):
        self.accepted(dbu_realized="1000")
        self.accepted(dbu_realized="1000.02")
        self.accepted(dbu_realized=999.98)
        self.refused("dbu_realized", dbu_realized="1000.04")
        self.refused("dbu_realized", dbu_realized="600")

    def test_an_aws_component_needs_a_dbu_component_and_an_evidence_type(self):
        self.refused("AWS component without a DBU component", aws_economic_realized="1000", aws_evidence_type="MODELED")
        self.refused("AWS component without an evidence type", dbu_realized="600", aws_economic_realized="400")

    def test_modeled_aws_is_never_cash(self):
        for field in ("aws_cash_realized", "aws_commitment_freed"):
            with self.subTest(field=field):
                self.refused(field, dbu_realized="600", aws_economic_realized="400", aws_evidence_type="MODELED", **{field: "400"})
                self.refused(field, dbu_realized="1000", **{field: "400"})  # no evidence type is not CUR_ACTUAL either
                self.accepted(dbu_realized="600", aws_economic_realized="400", aws_evidence_type="CUR_ACTUAL", **{field: "400"})
        self.refused("aws_cash_realized", aws_cash_realized="900", aws_evidence_type="MODELED")

    def test_a_component_that_is_not_a_finite_number_is_refused_naming_the_field(self):
        for field in ("dbu_realized", "aws_economic_realized", "aws_cash_realized", "aws_commitment_freed"):
            for value in (float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    patch = {"dbu_realized": "600", "aws_economic_realized": "400", "aws_evidence_type": "CUR_ACTUAL", field: value}
                    self.refused(field, **patch)
        self.refused("counterfactual_old_cost", counterfactual_old_cost=float("nan"), dbu_realized="1000")

    def test_without_both_costs_the_components_are_not_checked(self):
        # Without both costs the economics are unavailable and no component is reported.
        self.accepted(observed_new_cost=None, dbu_realized="5000")
        self.accepted(counterfactual_old_cost=None, dbu_realized="5000", aws_economic_realized="400", aws_evidence_type="MODELED")
        self.accepted(observed_new_cost=None, aws_economic_realized="400", aws_cash_realized="400", aws_evidence_type="MODELED")

    def test_without_economics_nothing_is_checked(self):
        loader.validate_observation({**self.base, "economics": None, "window_start_utc": None, "window_end_utc": None}, self.pkg)


class ProjectedObservationTests(unittest.TestCase):
    """The live projector rounds each amount to cents; its components still reconcile with its total."""

    def test_projected_components_pass_the_checks(self):
        cases = {
            "DBU and modeled AWS": (PRO, world.world_spec("C13", {"auto_stop": 10}), 13, {}),
            "DBU only": (PRO, world.world_spec("C13", {"auto_stop": 10}), 13, {"aws_registry": {}}),
            "serverless": (world.package("autostop-scoped-serverless"), world.world_spec("C02", {"auto_stop": 5}), 2, {}),
            "never stop": (world.package("autostop-scoped-never-stop"), world.world_spec("C01", {"auto_stop": 35}), 1, {}),
        }
        for label, (package, spec, index, options) in cases.items():
            with self.subTest(case=label):
                observation = project_observation(package, world.measure(package, spec, index, **options))
                loader.validate_observation(observation, package)
                econ = observation["economics"]
                total = Decimal(econ["counterfactual_old_cost"]) - Decimal(econ["observed_new_cost"])
                components = Decimal(econ["dbu_realized"]) + Decimal(econ["aws_economic_realized"] or 0)
                self.assertLessEqual(abs(components - total), Decimal("0.03"))
                self.assertIsNone(econ["aws_cash_realized"])


class ComparedComponentTests(unittest.TestCase):
    """A caller-supplied observation that gives only a DBU component states DBU costs: it is compared with the DBU range."""

    def setUp(self):
        sid = PRO["recommended_plan"]["scenario_id"]
        self.app = {"application_scope": "CANDIDATE", "scenario_id": sid, "step": 1, "implementation_conformance": "EXACT"}

    def economics(self, **patch):
        observation = with_economics(demo_observation(PRO), expected_annual_savings_actual_scope=None, period_days=30, **patch)
        return calculate_economics(PRO, self.app, observation, RealizerPolicy(), True)

    def test_dbu_only_is_compared_with_the_dbu_range(self):
        dbu_only = self.economics(dbu_realized="1000")
        self.assertEqual((dbu_only["expected_range"]["component"], dbu_only["expected_same_scope_annual_savings"]), ("dbu", "5958.66"))

    def test_with_an_aws_component_or_no_component_it_is_compared_with_the_total(self):
        for patch in ({}, {"dbu_realized": "600", "aws_economic_realized": "400", "aws_evidence_type": "MODELED"}):
            with self.subTest(patch=patch):
                econ = self.economics(**patch)
                self.assertEqual((econ["expected_range"]["component"], econ["expected_same_scope_annual_savings"]), ("total", "9363.51"))

    def test_a_measurement_cost_basis_still_decides(self):
        observation = with_economics(demo_observation(PRO), expected_annual_savings_actual_scope=None, period_days=30, dbu_realized="1000")
        observation["measurement"] = {"cost_basis": "DBU_AND_MODELED_AWS"}
        econ = calculate_economics(PRO, self.app, observation, RealizerPolicy(), True)
        self.assertEqual(econ["expected_range"]["component"], "total")


if __name__ == "__main__":
    unittest.main()
