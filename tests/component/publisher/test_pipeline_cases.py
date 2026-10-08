from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher import publisher as p  # noqa: E402
from publisher.analyzer.collector.demo import demo_evidence  # noqa: E402
from publisher.common.hashing import canonical_json  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class PipelineCases(PublisherCase):
    def test_analysis_time_makes_decision_semantics_repeatable(self):
        first = p.evaluate_warehouses(demo_evidence(), self.t)[0]
        second = p.evaluate_warehouses(demo_evidence(), self.t)[0]
        self.assertEqual(canonical_json(first.candidates), canonical_json(second.candidates))
        self.assertEqual(canonical_json(first.scenarios), canonical_json(second.scenarios))

    def test_same_evidence_produces_identical_locked_json_and_workbook_projection(self):
        from publisher.contracts import workbook_contract as wc
        ea = demo_evidence(); eb = demo_evidence()
        pa = p.build_recommendation_packages(ea, self.t)
        pb = p.build_recommendation_packages(eb, self.t)
        self.assertEqual([x.to_dict() for x in pa], [x.to_dict() for x in pb])
        self.assertEqual(wc.project_workbook(pa), wc.project_workbook(pb))
