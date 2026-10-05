"""Configuration loading: turn parsed CLI arguments into a PublisherRequest."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional, Sequence, Tuple

from publisher.config.settings import REPO_ROOT
from publisher.config.versions import PUBLISHER_VERSION
from publisher.contracts import reporter_contract as rc
from publisher.contracts.request import MODE_DEMO, MODE_LIVE, MODE_REPLAY, PublisherRequest


def _resolve_execution_args(args: argparse.Namespace) -> Tuple[str,str]:
    supplied=[("warehouse",args.serverless_warehouse_id),("warehouse",args.sql_warehouse_id),("cluster",args.all_purpose_cluster_id)]
    supplied=[x for x in supplied if x[1]]
    if args.execution_id:
        if supplied: raise SystemExit("Use either --execution-id/--execution-kind or one convenience execution-ID flag, not both.")
        if not args.execution_kind: raise SystemExit("--execution-kind is required with --execution-id")
        return args.execution_kind,str(args.execution_id)
    if len(supplied)>1: raise SystemExit("Supply exactly one execution target: serverless/sql warehouse ID or all-purpose cluster ID.")
    if supplied: return supplied[0][0],str(supplied[0][1])
    raise SystemExit("Execution target required: --serverless-warehouse-id, --sql-warehouse-id, --all-purpose-cluster-id, or --execution-kind/--execution-id")


def request_from_args(args: argparse.Namespace) -> PublisherRequest:
    """The run request: demo when --demo, replay when --from-evidence, otherwise live with a resolved execution target."""
    common = dict(output_dir=Path(args.output_dir), batch_size=args.batch_size, generate_workbook=not args.no_workbook,
                  print_json=args.print_json, source_region=args.source_region)
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
    from publisher.analyzer.collector.demo import DEMO_FLEET
    from publisher.contracts import workbook_contract as wc
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
    p.add_argument("--demo-count",type=int,default=len(DEMO_FLEET),help=f"Synthetic collector warehouse count for --demo (1..{len(DEMO_FLEET)}, one per golden scenario)")
    p.add_argument("--no-workbook",action="store_true",help="Skip workbook.xlsx generation; locked JSON packages are still emitted")
    p.add_argument("--print-json",action="store_true")
    return p.parse_args(argv)
