"""Reporter facade: persist the reviewed packages and the run outputs; never recalculates or changes them."""
from __future__ import annotations

from publisher.common.logging import LOGGER
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.report import ReportContract
from publisher.contracts.request import RunContext
from publisher.contracts.review import PackageReviewResult
from publisher.reporter.json_reporter import package_locked_contract


def run(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult, package: PackageReviewResult) -> ReportContract:
    """How the canonical packages are communicated: write them, evidence.json, the workbook and the manifest.

    Returns a ReportContract with the written file paths and the run counts. Counts come straight from the reviewed
    packages and the governor's exclusions; nothing is recalculated. Files are written under
    ``context.request.output_dir`` in the order documented in ``json_reporter.package_locked_contract``.
    """
    packages = package.packages
    files = package_locked_contract(context.request.output_dir, packages, analysis.evidence, generate_workbook=context.request.generate_workbook,
                                    unseen_billed_warehouses=analysis.unseen_billed_warehouses)
    excluded = len(governance.exclusions)
    LOGGER.info("reporter.done packages=%d files=%d analyzed=%d excluded=%d", len(packages), len(files),
                len(analysis.evidence) - excluded, excluded)
    return ReportContract(files=files, packages=list(packages), analyzed_count=len(analysis.evidence) - excluded, excluded_count=excluded,
                          candidate_count=sum(len(p.candidates) for p in packages), scenario_count=sum(len(p.scenarios) for p in packages))
