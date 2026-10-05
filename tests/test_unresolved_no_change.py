"""A decision that leaves a problem unresolved names it with a reason code instead of an empty blocker list.

Queueing while the warehouse rarely reaches its ceiling (scale-up lag on short bursts), queueing whose peak demand fits
the ceiling, a ceiling increase capped by the cluster limit below peak demand, and a warehouse already at the smallest
size each carry a code, and the rationale gives the numbers.
"""
import copy
import dataclasses
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402


class UnresolvedNoChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        cls.t = p.Thresholds()
        fleet = p.synthetic_demo_evidence()
        cls.by = {e.current_epoch.tags["golden_scenario"]: e for e in fleet}
        cls.cands = {}
        for w in p.evaluate_warehouses(fleet, cls.t):
            e = next(x for x in fleet if x.warehouse_id == w.warehouse_id)
            cls.cands[e.current_epoch.tags["golden_scenario"]] = {c.knob: c for c in w.candidates}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_c46_queueing_below_the_ceiling_is_named_with_its_numbers(self):
        # Hourly bursts of ~70 concurrent queries for 8 minutes: 47% of queries queue, yet the warehouse is at its
        # ceiling of 4 only 4.9% of the time, because clusters arrive minutes after each burst starts.
        c = self.cands["C46"]["max_clusters"]
        self.assertEqual(c.status, "NO_CHANGE")
        self.assertEqual(c.blockers, ["QUEUEING_BELOW_CEILING"])
        rationale = c.metadata["rationale"]
        for text in ("47.5% of queries queued", "4.9% of the time", "70 concurrent queries", "7 clusters", "scale-up lag"):
            self.assertIn(text, rationale)

    def test_c47_smallest_size_is_named(self):
        c = self.cands["C47"]["warehouse_size"]
        self.assertEqual((c.status, c.blockers), ("NO_CHANGE", ["ALREADY_AT_SMALLEST_SIZE"]))
        self.assertIn("smallest size", c.metadata["rationale"])

    def test_c42_ceiling_capped_by_the_cluster_limit_below_peak_demand(self):
        c = self.cands["C42"]["max_clusters"]
        self.assertEqual((c.status, c.recommended_config["max_clusters"]), ("ACTIONABLE", 40))
        self.assertEqual(c.blockers, ["PEAK_DEMAND_ABOVE_CLUSTER_LIMIT"])
        self.assertIn("108 clusters", c.metadata["rationale"])
        size = self.cands["C42"]["warehouse_size"]  # no spill: queueing alone never upsizes
        self.assertEqual(size.status, "NO_CHANGE")

    def test_queueing_with_demand_inside_the_ceiling_is_named(self):
        e = copy.deepcopy(self.by["C08"])  # saturated and queueing at its ceiling
        e.current_epoch.config = dataclasses.replace(e.current_epoch.config, max_clusters=30)
        c = next(c for c in p.evaluate_warehouses([e], self.t)[0].candidates if c.knob == "max_clusters")
        self.assertEqual((c.status, c.blockers), ("NO_CHANGE", ["QUEUEING_WITH_DEMAND_WITHIN_CEILING"]))

    def test_quiet_warehouse_keeps_an_empty_blocker_list(self):
        c = self.cands["C06"]["min_clusters"]  # nothing unresolved: no code invented
        self.assertEqual(c.blockers, [])



class UnresolvedNoChangeMissingDemandTests(unittest.TestCase):
    def test_rationale_never_prints_a_missing_demand_figure(self):
        logging.disable(logging.CRITICAL)
        try:
            t = p.Thresholds()
            e = copy.deepcopy(next(x for x in p.synthetic_demo_evidence() if x.current_epoch.tags["golden_scenario"] == "C46"))
            e.current_epoch.query["concurrency_histogram"]["BUSY_START"] = {}
            c = next(c for c in p.evaluate_warehouses([e], t)[0].candidates if c.knob == "max_clusters")
            self.assertEqual(c.blockers, ["QUEUEING_BELOW_CEILING"])
            self.assertNotIn("None", c.metadata["rationale"])
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
