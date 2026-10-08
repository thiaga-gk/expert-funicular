from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.contracts.analysis import AnalysisResult  # noqa: E402
from publisher.contracts.candidate import CandidateRecommendation, OptimizationDecision  # noqa: E402
from publisher.contracts.review import WarehouseOptimization  # noqa: E402
from publisher.contracts.scenario import PlanStep, Scenario  # noqa: E402
from publisher.contracts.source import EpochEvidence, PhotonObservation, QuerySourceProfile, WarehouseAttribution, WarehouseConfig  # noqa: E402
from tests.publisher_case import PublisherCase  # noqa: E402


class ContractsCases(PublisherCase):
    def test_normalized_domain_contracts_are_dataclasses(self):
        from dataclasses import is_dataclass
        for cls in (WarehouseConfig,EpochEvidence,PhotonObservation,QuerySourceProfile,WarehouseAttribution,AnalysisResult,OptimizationDecision,CandidateRecommendation,PlanStep,Scenario,WarehouseOptimization):
            self.assertTrue(is_dataclass(cls),cls.__name__)
