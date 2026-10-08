"""Trailing-365-day actual AWS cost: modeled from billed DBU per epoch, never guessed.

Billing records DBU, not hours. Each Pro/Classic billing day's cluster-hours are its DBU over the published DBU rate of
the size its epoch ran, priced at that size's on-demand cluster-hour (driver + workers × regional EC2 price). Serverless
days add 0. Anything that cannot be priced makes the whole figure null, so the actual total is unknown rather than low.
"""
import copy
import dataclasses
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.analyzer.collector.pricing import cluster_hourly, model_aws_actual_cost  # noqa: E402
from publisher.contracts import reporter_contract as rc  # noqa: E402
from publisher.packager.adapter import _actual_cost_breakdown  # noqa: E402

US_EAST_1 = {"i3.2xlarge": 0.624, "i3.4xlarge": 1.248, "i3.8xlarge": 2.496, "i3.16xlarge": 4.992}


class AwsActualCostTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.e = copy.deepcopy(demo_evidence()[0])  # PRO: MEDIUM epoch from 2025-10-01, LARGE from 2026-01-15

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def with_type(self, epoch_index, warehouse_type):
        ep = self.e.epochs[epoch_index]
        self.e.epochs[epoch_index] = dataclasses.replace(ep, config=dataclasses.replace(ep.config, warehouse_type=warehouse_type))

    def hand_computed(self):
        """Σ dbu / DBU-rate(size) × cluster $/h(size) over the window, MEDIUM = 24 DBU/h, LARGE = 40 DBU/h."""
        rate = {"MEDIUM": (24.0, 2.496 + 8 * 0.624), "LARGE": (40.0, 2.496 + 16 * 0.624)}
        size = {ep.epoch_start: ep.config.warehouse_size for ep in self.e.epochs}
        types = {ep.epoch_start: ep.config.warehouse_type for ep in self.e.epochs}
        total = 0.0
        for r in self.e.daily_cost_365:
            if "2025-10-01" <= r["usage_date"] <= "2026-09-29" and types[r["epoch_start"]] != "SERVERLESS":
                dbu_rate, hourly = rate[size[r["epoch_start"]]]
                total += r["dbu_quantity"] / dbu_rate * hourly
        return round(total, 2)

    def test_cluster_hour_is_driver_plus_workers(self):
        self.assertAlmostEqual(cluster_hourly("MEDIUM", US_EAST_1), 2.496 + 8 * 0.624)
        self.assertAlmostEqual(cluster_hourly("LARGE", US_EAST_1), 2.496 + 16 * 0.624)
        self.assertIsNone(cluster_hourly("MEDIUM", {"i3.2xlarge": 0.624}))
        self.assertIsNone(cluster_hourly("NOT_A_SIZE", US_EAST_1))

    def test_pro_across_a_size_change_prices_each_epoch_at_its_own_size(self):
        cost, model = model_aws_actual_cost(self.e)
        self.assertEqual(model["status"], "MODELED")
        self.assertEqual(cost, self.hand_computed())
        actual = _actual_cost_breakdown(self.e)
        self.assertEqual(actual.aws_cost, cost)
        self.assertEqual(actual.total_cost, round(actual.dbu_cost + cost, 2))
        self.assertEqual(actual.availability, rc.CostAvailability.COMPLETE)
        self.assertEqual(actual.aws_basis, rc.CostBasis.MODELED_AWS_LIST_PRICE)

    def test_classic_is_modeled_like_pro(self):
        pro, _ = model_aws_actual_cost(self.e)
        self.with_type(0, "CLASSIC"); self.with_type(1, "CLASSIC")
        self.assertEqual(model_aws_actual_cost(self.e)[0], pro)

    def test_serverless_days_add_zero_and_older_pro_days_are_modeled(self):
        self.with_type(1, "SERVERLESS")
        cost, model = model_aws_actual_cost(self.e)
        self.assertEqual(cost, self.hand_computed())
        self.assertGreater(cost, 0)
        self.assertGreater(model["serverless_rows"], 0)
        self.assertEqual(_actual_cost_breakdown(self.e).aws_basis, rc.CostBasis.MODELED_AWS_LIST_PRICE)

    def test_serverless_throughout_is_zero_not_null(self):
        self.with_type(0, "SERVERLESS"); self.with_type(1, "SERVERLESS")
        cost, model = model_aws_actual_cost(self.e)
        self.assertEqual((cost, model["status"]), (0.0, "NOT_CUSTOMER_BILLED"))
        self.assertEqual(_actual_cost_breakdown(self.e).aws_basis, rc.CostBasis.MODELED_CURRENT_EPOCH)

    def test_missing_regional_prices_are_null_never_zero(self):
        self.e.aws_cost_model = {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_REGION_NOT_IN_REGISTRY"}
        cost, model = model_aws_actual_cost(self.e)
        self.assertIsNone(cost)
        self.assertEqual(model["blocker"], "AWS_PRICE_REGION_NOT_IN_REGISTRY")
        actual = _actual_cost_breakdown(self.e)
        self.assertIsNone(actual.total_cost)
        self.assertEqual((actual.availability, actual.aws_basis), (rc.CostAvailability.PARTIAL, rc.CostBasis.UNAVAILABLE))

    def test_falsified_topology_is_null(self):
        self.e.aws_cost_model = {**self.e.aws_cost_model, "status": "UNAVAILABLE", "blocker": "AWS_NODE_TYPE_MISMATCH"}
        self.assertEqual(model_aws_actual_cost(self.e)[1]["blocker"], "AWS_NODE_TYPE_MISMATCH")

    def test_price_currency_other_than_billing_currency_is_null(self):
        self.e.currency = "EUR"
        cost, model = model_aws_actual_cost(self.e)
        self.assertIsNone(cost)
        self.assertEqual(model["blocker"], "AWS_PRICE_CURRENCY_MISMATCH")

    def test_billing_row_of_unknown_epoch_is_null(self):
        self.e.daily_cost_365[-1] = {**self.e.daily_cost_365[-2], "epoch_start": "2024-01-01T00:00:00+00:00"}
        self.assertEqual(model_aws_actual_cost(self.e)[1]["blocker"], "BILLING_ROW_EPOCH_UNKNOWN")

    def test_size_without_published_rate_is_null(self):
        ep = self.e.epochs[1]
        self.e.epochs[1] = dataclasses.replace(ep, config=dataclasses.replace(ep.config, warehouse_size="5X_LARGE"))
        self.assertIsNone(model_aws_actual_cost(self.e)[0])

    def test_days_outside_the_settled_window_do_not_count(self):
        before = model_aws_actual_cost(self.e)[0]
        late = {**self.e.daily_cost_365[-1], "usage_date": "2026-09-30", "dbu_quantity": 10000.0}
        self.e.daily_cost_365.append(late)  # after billing_complete_through
        self.assertEqual(model_aws_actual_cost(self.e)[0], before)

    def test_deterministic(self):
        self.assertEqual(model_aws_actual_cost(self.e), model_aws_actual_cost(copy.deepcopy(self.e)))


if __name__ == "__main__":
    unittest.main()
