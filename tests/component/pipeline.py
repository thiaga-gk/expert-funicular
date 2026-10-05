"""One cached demo pipeline run (Analyzer -> Governor -> Optimizer -> Reviewer -> Packager) shared by component tests."""
import functools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer import analyzer  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from publisher.contracts.request import MODE_DEMO, PublisherRequest, RunContext  # noqa: E402
from publisher.governor import governor  # noqa: E402
from publisher.optimizer import optimizer  # noqa: E402
from publisher.packager import packager  # noqa: E402
from publisher.reviewer import reviewer  # noqa: E402


@functools.lru_cache(maxsize=1)
def demo_run():
    context = RunContext(PublisherRequest(mode=MODE_DEMO, output_dir=Path("."), demo_count=41), Thresholds())
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    optimization = optimizer.run(context, analysis, governance)
    reviewed = reviewer.review_candidates(context, analysis, governance, optimization)
    packaging = packager.run(context, analysis, governance, reviewed)
    return context, analysis, governance, reviewed, packaging
