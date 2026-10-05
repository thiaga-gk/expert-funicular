"""Packager (src/publisher/packager): planning and assembly are deterministic and run() is plan + assemble."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.analyzer.collector.demo import synthetic_demo_evidence  # noqa: E402
from publisher.config.thresholds import Thresholds  # noqa: E402
from publisher.contracts.analysis import FleetAnalysis  # noqa: E402
from publisher.contracts.request import MODE_REPLAY, PublisherRequest, RunContext  # noqa: E402
from publisher.governor import governor  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402
from publisher.packager import packager  # noqa: E402
from publisher.reviewer import reviewer  # noqa: E402


def reviewed_fleet(n=6):
    t = Thresholds()
    fleet = synthetic_demo_evidence(n)
    analysis = FleetAnalysis(fleet, analyzer.analyze(fleet, t))
    context = RunContext(PublisherRequest(mode=MODE_REPLAY, output_dir=Path(".")), t)
    governance = governor.run(context, analysis)
    optimization = optimizer.run(context, analysis, governance)
    return context, analysis, governance, reviewer.review_candidates(context, analysis, governance, optimization)


class PackagerTests(unittest.TestCase):
    def test_run_is_plan_then_assemble_and_deterministic(self):
        context, analysis, governance, reviewed = reviewed_fleet()
        first = packager.run(context, analysis, governance, copy.deepcopy(reviewed))
        second = packager.run(context, analysis, governance, copy.deepcopy(reviewed))
        self.assertEqual([p.to_dict() for p in first.packages], [p.to_dict() for p in second.packages])
        planned = packager.plan(copy.deepcopy(reviewed), context.thresholds)
        assembled = packager.assemble(analysis, planned, context.thresholds)
        self.assertEqual([p.recommendation_package_id for p in assembled], [p.recommendation_package_id for p in first.packages])

    def test_every_planned_warehouse_has_a_plan_a(self):
        context, _, _, reviewed = reviewed_fleet()
        for w in packager.plan(reviewed, context.thresholds):
            self.assertTrue(w.scenarios)
            self.assertTrue(any(s.name.startswith("Plan A") for s in w.scenarios), [s.name for s in w.scenarios])


if __name__ == "__main__":
    unittest.main()
