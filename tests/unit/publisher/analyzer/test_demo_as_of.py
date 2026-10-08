"""--demo-as-of: the demo fleet analyzed at a later time, with its configuration history unchanged.

The default is the fixed demo analysis time, so spelling it out changes nothing; a later time moves the analysis time,
the billing watermark and the packages' generated_at; an earlier one is refused (the fleet's epochs start just before
the default). The Realizer's demo world measures a moved package like any other.
"""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import DEMO_ANALYSIS_TIME, resolve_demo_analysis_time, synthetic_demo_evidence  # noqa: E402
from publisher.common.utils import as_serializable  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.common.errors import PublisherError  # noqa: E402

LATER = "2026-11-01T12:00:00+00:00"


class DemoAsOfTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)

    def test_resolution_normalizes_and_refuses_earlier_times(self):
        self.assertEqual(resolve_demo_analysis_time(None), DEMO_ANALYSIS_TIME)
        self.assertEqual(resolve_demo_analysis_time("2026-10-01T12:00:00Z"), DEMO_ANALYSIS_TIME)
        self.assertEqual(resolve_demo_analysis_time("2026-11-01T13:00:00+01:00"), LATER)
        for value in ("2026-10-01T11:59:59Z", "not a time"):
            with self.subTest(value=value), self.assertRaises(PublisherError):
                resolve_demo_analysis_time(value)

    def test_the_default_spelled_out_is_byte_identical(self):
        default = as_serializable(synthetic_demo_evidence(2))
        spelled = as_serializable(synthetic_demo_evidence(2, resolve_demo_analysis_time("2026-10-01T12:00:00Z")))
        self.assertEqual(default, spelled)

    def test_a_later_time_moves_the_analysis_and_the_packages(self):
        moved = synthetic_demo_evidence(2, LATER)
        self.assertEqual({e.analysis_time for e in moved}, {LATER})
        self.assertEqual({str(e.forecast_metadata.get("billing_watermark"))[:10] for e in moved}, {"2026-11-01"})
        self.assertEqual(as_serializable(moved), as_serializable(synthetic_demo_evidence(2, LATER)))
        packages = p.build_recommendation_packages(moved, Thresholds())
        self.assertEqual({x.provenance.generated_at for x in packages}, {"2026-11-01T12:00:00Z"})

    def test_the_realizer_demo_world_measures_a_moved_package(self):
        from realizer.analyzer.collector import demo_world
        from realizer.analyzer.projector import project_observation
        document = p.build_recommendation_packages(synthetic_demo_evidence(1, LATER), Thresholds())[0].to_dict()
        observation = project_observation(document, demo_world.simulated_evidence(document), None)
        self.assertGreater(observation["applied_at_utc"], "2026-11-01T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
