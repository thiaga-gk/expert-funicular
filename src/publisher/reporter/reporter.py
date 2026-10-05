"""Reporter facade: persist the reviewed packages and the run outputs, and print the CLI summary; never recalculates or changes them."""
from __future__ import annotations

import json
from pathlib import Path

from publisher.contracts import reporter_contract as rc
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.report import ReportContract
from publisher.contracts.request import RunContext
from publisher.contracts.review import PackageReviewResult
from publisher.reporter.json_reporter import package_locked_contract


def run(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult, package: PackageReviewResult) -> ReportContract:
    """How the canonical packages are communicated: write them, evidence.json, the workbook and the manifest."""
    packages = package.packages
    files = package_locked_contract(context.request.output_dir, packages, analysis.evidence, generate_workbook=context.request.generate_workbook,
                                    unseen_billed_warehouses=analysis.unseen_billed_warehouses)
    excluded = len(governance.exclusions)
    return ReportContract(files=files, packages=list(packages), analyzed_count=len(analysis.evidence) - excluded, excluded_count=excluded,
                          candidate_count=sum(len(p.candidates) for p in packages), scenario_count=sum(len(p.scenarios) for p in packages))


def print_summary(report: ReportContract, log_path: Path, *, print_json: bool = False) -> None:
    """CLI output: the single package as JSON (--print-json), or the run summary and the written files."""
    if print_json:
        if len(report.packages) != 1:
            raise SystemExit("--print-json requires exactly one warehouse package; use the packages/ directory for multi-warehouse runs.")
        print(json.dumps(report.packages[0].to_dict(),indent=2,sort_keys=True))
    else:
        print(f"Analyzed {report.analyzed_count} warehouse(s); emitted {len(report.packages)} locked Reporter Contract v{rc.CONTRACT_VERSION} package(s), {report.candidate_count} candidates, and {report.scenario_count} scenarios; excluded {report.excluded_count} unsupported warehouse(s) (see manifest).")
        for k,v in report.files.items(): print(f"{k}: {v}")
        print(f"log: {log_path}")
