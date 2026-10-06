"""``publisher publish``: one publisher run from command-line arguments.

The CLI only parses and validates its own inputs, builds the PublisherRequest, configures logging, calls
``publisher.publish`` and prints a short summary. It never reaches into a capability package.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Optional, Sequence, Tuple

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER, setup_logging
from publisher.config.cost_profiles import COST_PROFILES, parse_cost_profile_flags
from publisher.config.output_layout import partitioned_run_dir
from publisher.config.settings import EXECUTION_MODES, REPO_ROOT
from publisher.config.versions import ANALYZER_VERSION, BUNDLE_MODEL_VERSION, ESTIMATOR_VERSION, OPTIMIZER_VERSION, POLICY_VERSION, PUBLISHER_VERSION
from publisher.contracts import reporter_contract as rc
from publisher.contracts.report import ReportContract
from publisher.contracts.request import MODE_DEMO, MODE_LIVE, MODE_REPLAY, PublisherRequest
from publisher.publisher import demo_fleet_size, publish


def _resolve_execution_args(args: argparse.Namespace) -> Tuple[str,str]:
    """The live collector's SQL execution target as (kind, id) from --execution-kind/--execution-id or exactly one
    convenience flag (serverless/sql warehouse ID, all-purpose cluster ID). Raises PublisherError when none, several or
    an inconsistent combination is given."""
    supplied=[("warehouse",args.serverless_warehouse_id),("warehouse",args.sql_warehouse_id),("cluster",args.all_purpose_cluster_id)]
    supplied=[x for x in supplied if x[1]]
    if args.execution_id:
        if supplied: raise PublisherError("Use either --execution-id/--execution-kind or one convenience execution-ID flag, not both.")
        if not args.execution_kind: raise PublisherError("--execution-kind is required with --execution-id")
        return args.execution_kind,str(args.execution_id)
    if len(supplied)>1: raise PublisherError("Supply exactly one execution target: serverless/sql warehouse ID or all-purpose cluster ID.")
    if supplied: return supplied[0][0],str(supplied[0][1])
    raise PublisherError("Execution target required: --serverless-warehouse-id, --sql-warehouse-id, --all-purpose-cluster-id, or --execution-kind/--execution-id")


def run_output_dir(args: argparse.Namespace, now: Optional[dt.datetime] = None) -> Path:
    """Where this run writes: a new ``run_date=/run_ts=`` folder under --output-dir (UTC start time), or --output-dir
    itself with --flat-output."""
    root = Path(args.output_dir)
    if args.flat_output:
        return root
    return partitioned_run_dir(root, now or dt.datetime.now(dt.timezone.utc))


def request_from_args(args: argparse.Namespace, output_dir: Optional[Path] = None) -> PublisherRequest:
    """The run request: demo when --demo, replay when --from-evidence, otherwise live with a resolved execution target.

    ``output_dir`` is the resolved run folder (``--output-dir`` itself when None). Raises PublisherError for invalid
    --cost-profile flags before anything is collected. The run-scope flags are passed on as raw strings; the config
    layer validates and resolves them.
    """
    parse_cost_profile_flags(args.cost_profile)
    common = dict(output_dir=output_dir or Path(args.output_dir), batch_size=args.batch_size, generate_workbook=not args.no_workbook,
                  print_json=args.print_json, source_region=args.source_region,
                  execution_mode=args.mode, cost_profiles=tuple(args.cost_profile), scope_specs=tuple(args.scope),
                  scope_file=args.scope_file, warehouses=tuple(args.warehouse))
    if args.demo:
        return PublisherRequest(mode=MODE_DEMO, demo_count=args.demo_count, **common)
    if args.from_evidence:
        return PublisherRequest(mode=MODE_REPLAY, evidence_files=tuple(args.from_evidence), **common)
    kind, eid = _resolve_execution_args(args)
    return PublisherRequest(
        mode=MODE_LIVE, profile=args.profile, execution_kind=kind, execution_id=eid, config_file=args.config_file,
        workspace_id=args.workspace_id, aws_price_registry=args.aws_price_registry,
        compatibility_api_mode=args.compatibility_api_mode, billing_ingested_through=args.billing_ingested_through,
        allow_missing_aws_price=args.allow_missing_aws_price, **common,
    )


def parse_args(argv:Optional[Sequence[str]]=None)->argparse.Namespace:
    """Parse the ``publish`` options (defaults come from the documented environment variables). Argument errors exit
    through argparse with status 2. Credentials are never accepted here: OAuth comes from the Databricks profile named by
    --profile."""
    from publisher.contracts import workbook_contract as wc
    fleet_size=demo_fleet_size()
    p=argparse.ArgumentParser(description=f"Deterministic Databricks SQL Warehouse optimizer v{PUBLISHER_VERSION} (Reporter Contract v{rc.CONTRACT_VERSION} + Workbook Contract v{wc.WORKBOOK_CONTRACT_VERSION})")
    p.add_argument("--output-dir",default="./warehouse_optimizer_out")
    p.add_argument("--demo",action="store_true")
    p.add_argument("--from-evidence",action="append",default=[])
    p.add_argument("--profile",default=os.getenv("DATABRICKS_CONFIG_PROFILE","DEFAULT"))
    p.add_argument("--execution-kind",choices=("warehouse","cluster"),default=None,help="Collector SQL execution target kind")
    p.add_argument("--execution-id",default=None,help="SQL warehouse ID or all-purpose cluster ID")
    p.add_argument("--sql-warehouse-id",default=os.getenv("DATABRICKS_SQL_WAREHOUSE_ID"),help="Convenience alias for a SQL warehouse execution target")
    p.add_argument("--serverless-warehouse-id",default=os.getenv("DATABRICKS_SERVERLESS_WAREHOUSE_ID"),help="Convenience alias for a Serverless SQL warehouse execution target")
    p.add_argument("--all-purpose-cluster-id",default=os.getenv("DATABRICKS_ALL_PURPOSE_CLUSTER_ID"),help="Convenience alias for an all-purpose compute execution target")
    p.add_argument("--workspace-id",default=os.getenv("DATABRICKS_WORKSPACE_ID"),help="Workspace/org ID; optional for warehouse, used to construct all-purpose cluster HTTP path")
    p.add_argument("--config-file",default=os.getenv("DATABRICKS_CONFIG_FILE"))
    p.add_argument("--source-region",default=os.getenv("DATABRICKS_REGION","unknown"),help="AWS region of the workspace; Pro/Classic AWS cost uses its registry prices")
    p.add_argument("--allow-missing-aws-price",action="store_true",help="Live mode: publish even when --source-region is missing from the AWS price registry (Pro/Classic AWS cost unavailable)")
    p.add_argument("--billing-ingested-through",default=None,help="YYYY-MM-DD: only billing records ingested on or before this date (reproduces an earlier collection)")
    default_registry=str(REPO_ROOT / "pricing" / "aws_ec2_on_demand.json")
    p.add_argument("--aws-price-registry",default=os.getenv("AWS_PRICE_REGISTRY",default_registry))
    p.add_argument("--batch-size",type=int,default=int(os.getenv("WAREHOUSE_BATCH_SIZE","100")),help="Downstream analysis/report batch size; collector SQL is set-based and aggregated in Databricks")
    p.add_argument("--compatibility-api-mode",choices=("auto","off"),default=os.getenv("COMPATIBILITY_API_MODE","auto"),help="auto=B read-only workspace compatibility API when available, else A HITL fallback")
    p.add_argument("--log-level",choices=("DEBUG","INFO","WARNING","ERROR"),default=os.getenv("PUBLISHER_LOG_LEVEL","INFO"))
    p.add_argument("--demo-count",type=int,default=fleet_size,help=f"Synthetic collector warehouse count for --demo (1..{fleet_size}, one per golden scenario)")
    p.add_argument("--no-workbook",action="store_true",help="Skip workbook.xlsx generation; locked JSON packages are still emitted")
    p.add_argument("--print-json",action="store_true")
    p.add_argument("--mode",choices=EXECUTION_MODES,default=os.getenv("PUBLISHER_EXECUTION_MODE","local"),help="Where the run executes (only local today)")
    p.add_argument("--cost-profile",action="append",default=[],metavar="PROFILE[=WH_ID,...]",
                   help=f"Cost profile ({', '.join(COST_PROFILES)}) for every warehouse, or with =WH_ID[,WH_ID...] (warehouse_id or workspace_id/warehouse_id) for the listed ones; repeatable. Default: aggressive for nonprod-tagged warehouses, balanced otherwise")
    p.add_argument("--flat-output",action="store_true",help="Write directly into --output-dir; by default each run writes to its own run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/ folder under it")
    p.add_argument("--scope",action="append",default=[],metavar="SCENARIO[:TECHNIQUE,...]",help="Recommend only these scenarios/techniques (repeatable): current[:size,max-clusters,min-clusters,auto-stop], migration[:warehouse-type]. Default: everything")
    p.add_argument("--scope-file",default=None,help="JSON run-scope file (schema/run-scope.schema.json) with scenario/technique toggles and warehouses; not combined with --scope/--warehouse")
    p.add_argument("--warehouse",action="append",default=[],metavar="all|WAREHOUSE_ID|WORKSPACE_ID/WAREHOUSE_ID",help="Report only these warehouses (repeatable). Default: all")
    return p.parse_args(argv)


def print_summary(report: ReportContract, log_path: Path, *, print_json: bool = False) -> None:
    """CLI output on stdout: the single package as JSON (--print-json), or the run summary and the written files.

    Raises PublisherError when --print-json is used with anything other than exactly one package.
    """
    if print_json:
        if len(report.packages) != 1:
            raise PublisherError("--print-json requires exactly one warehouse package; use the packages/ directory for multi-warehouse runs.")
        print(json.dumps(report.packages[0].to_dict(),indent=2,sort_keys=True))
    else:
        print(f"Analyzed {report.analyzed_count} warehouse(s); emitted {len(report.packages)} locked Reporter Contract v{rc.CONTRACT_VERSION} package(s), {report.candidate_count} candidates, and {report.scenario_count} scenarios; excluded {report.excluded_count} unsupported warehouse(s) (see manifest).")
        print(f"run folder: {log_path.parent}")
        for k,v in report.files.items(): print(f"{k}: {v}")
        print(f"log: {log_path}")


def _log_settings(args: argparse.Namespace, request: PublisherRequest) -> None:
    """Log the resolved run settings once. Only non-sensitive values: the profile NAME (never tokens), the execution
    kind, counts instead of file lists, and no execution IDs, config-file contents or credentials."""
    LOGGER.info(
        "cli.settings run_mode=%s execution_mode=%s output_dir=%s log_level=%s workbook=%s print_json=%s batch_size=%d",
        request.mode, args.mode, request.output_dir, args.log_level, not args.no_workbook, args.print_json, args.batch_size,
    )
    selection = parse_cost_profile_flags(request.cost_profiles)
    LOGGER.info("cli.settings.cost_profile all_warehouses=%s warehouse_overrides=%d profiles=%s",
                selection.all_profile or "TAG_DEFAULT", len(selection.by_warehouse),
                ",".join(sorted({p for _, p in selection.by_warehouse} | ({selection.all_profile} if selection.all_profile else set()))) or "NONE")
    LOGGER.info("cli.settings.scope scope_flags=%d scope_file=%s warehouse_flags=%d",
                len(request.scope_specs), request.scope_file is not None, len(request.warehouses))
    if request.mode == MODE_LIVE:
        LOGGER.info("cli.settings.live profile=%s execution_kind=%s source_region=%s compatibility_api_mode=%s",
                    request.profile, request.execution_kind, request.source_region, request.compatibility_api_mode)
    elif request.mode == MODE_REPLAY:
        LOGGER.info("cli.settings.replay evidence_files=%d", len(request.evidence_files))
    else:
        LOGGER.info("cli.settings.demo demo_count=%d", request.demo_count)


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Run ``publisher publish``: parse, configure logging, build the request, publish, print the summary; returns 0.

    Creates the run folder (see ``run_output_dir``) and writes the run log to ``<run folder>/publisher.log`` before
    anything else can fail. Raises PublisherError for a refused run (``cli.main.main`` maps it to exit status 2).
    """
    args = parse_args(argv)
    output_dir = run_output_dir(args)
    log_path = setup_logging(output_dir, args.log_level)
    LOGGER.info("policy.versions policy=%s analyzer=%s optimizer=%s estimator=%s bundle=%s",
                POLICY_VERSION, ANALYZER_VERSION, OPTIMIZER_VERSION, ESTIMATOR_VERSION, BUNDLE_MODEL_VERSION)
    request = request_from_args(args, output_dir)
    _log_settings(args, request)
    report = publish(request)
    LOGGER.info("publisher.done warehouses=%d excluded=%d packages=%d candidates=%d scenarios=%d log=%s",
                report.analyzed_count, report.excluded_count, len(report.packages), report.candidate_count, report.scenario_count, log_path)
    print_summary(report, log_path, print_json=args.print_json)
    return 0
